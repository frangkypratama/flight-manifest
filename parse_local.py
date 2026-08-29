#!/usr/bin/env python3
"""Parse file PDF/teks lokal lalu kirim hasilnya ke API Laravel.

Penggunaan:
    python3 parse_local.py test-pdfs/manifest.pdf
    python3 parse_local.py test-pdfs/
"""

import json
import re
import subprocess
import sys
import time
from pathlib import Path

import requests
from dotenv import dotenv_values

BASE_DIR = Path(__file__).resolve().parent
PROMPT_PATH = BASE_DIR / "prompts" / "parse_flight_doc.md"
ENV_PATH = BASE_DIR / ".env"

PDFTOTEXT_TIMEOUT = 30
CLAUDE_TIMEOUT = 300
CLAUDE_MAX_RETRIES = 3
CLAUDE_RETRY_DELAY = 15
HTTP_TIMEOUT = 30

TEXT_EXTENSIONS = {".txt", ""}


def load_config() -> dict:
    values = dotenv_values(ENV_PATH)
    hosting_url = values.get("HOSTING_URL")
    api_key = values.get("API_KEY")
    gmail_query = values.get("GMAIL_QUERY", "")

    if not hosting_url or not api_key:
        print(f"✗ HOSTING_URL dan API_KEY wajib diisi di {ENV_PATH}")
        sys.exit(1)

    return {"HOSTING_URL": hosting_url, "API_KEY": api_key, "GMAIL_QUERY": gmail_query}


def extract_text(path: Path) -> str:
    """Ekstrak teks dari PDF (via pdftotext) atau baca langsung jika file teks."""
    if path.suffix.lower() == ".pdf":
        try:
            result = subprocess.run(
                ["pdftotext", "-layout", str(path), "-"],
                capture_output=True,
                text=True,
                timeout=PDFTOTEXT_TIMEOUT,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(f"pdftotext timeout: {exc}") from exc

        if result.returncode != 0:
            raise RuntimeError(f"pdftotext gagal: {result.stderr.strip()}")

        return result.stdout

    return path.read_text(encoding="utf-8", errors="replace")


def call_claude(document_text: str) -> str:
    """Kirim prompt + teks dokumen ke `claude -p` dan kembalikan output mentahnya."""
    prompt_template = PROMPT_PATH.read_text(encoding="utf-8")
    prompt = f"{prompt_template}\n\n---\n\nDocument text:\n\n{document_text}"

    last_error = None

    for attempt in range(1, CLAUDE_MAX_RETRIES + 1):
        try:
            result = subprocess.run(
                ["claude", "-p", "--output-format", "text", "--allowedTools", "", "--max-turns", "10"],
                input=prompt,
                capture_output=True,
                text=True,
                timeout=CLAUDE_TIMEOUT,
            )
        except subprocess.TimeoutExpired as exc:
            last_error = RuntimeError(f"claude -p timeout setelah {CLAUDE_TIMEOUT}s")
            last_error.__cause__ = exc
        else:
            if result.returncode == 0:
                return result.stdout

            detail = result.stderr.strip() or result.stdout.strip() or f"exit code {result.returncode}"
            last_error = RuntimeError(f"claude -p gagal: {detail}")

        if attempt < CLAUDE_MAX_RETRIES:
            print(f"  ⚠️  Percobaan {attempt} gagal ({last_error}), coba lagi dalam {CLAUDE_RETRY_DELAY}s...")
            time.sleep(CLAUDE_RETRY_DELAY)

    raise last_error


def extract_json(text: str) -> dict:
    """Ambil objek JSON dari response Claude, lepas markdown fence bila ada."""
    text = text.strip()

    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```\s*$", "", text)
        text = text.strip()

    start = text.find("{")
    if start == -1:
        raise ValueError("Tidak ditemukan JSON pada response Claude")

    depth = 0
    in_string = False
    escape = False
    end = None

    for i in range(start, len(text)):
        char = text[i]

        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            continue

        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                end = i
                break

    if end is None:
        raise ValueError("JSON pada response Claude tidak lengkap (kurung kurawal tidak seimbang)")

    return json.loads(text[start:end + 1])


def send_to_api(payload: dict, config: dict, extra_meta: dict | None = None) -> requests.Response:
    body = dict(payload)
    if extra_meta:
        body.update({k: v for k, v in extra_meta.items() if v})

    headers = {
        "Content-Type": "application/json",
        "X-API-Key": config["API_KEY"],
    }

    return requests.post(config["HOSTING_URL"], json=body, headers=headers, timeout=HTTP_TIMEOUT)


def _safe_json(response: requests.Response) -> dict:
    try:
        return response.json()
    except ValueError:
        return {"raw": response.text}


def process_document(path: Path, config: dict, extra_meta: dict | None = None) -> dict | None:
    """Jalankan satu dokumen lewat pipeline extract -> parse -> kirim ke API.

    Return None jika dokumen dilewati (misal teks kosong).
    """
    print(f"📄 {path.name}")

    text = extract_text(path)
    if not text.strip():
        print("  ⚠️  Teks kosong, dilewati")
        return None

    print("  🤖 Claude parsing...")
    response_text = call_claude(text)
    payload = extract_json(response_text)

    doc_type = payload.get("doc_type", "?")
    airline_code = payload.get("airline_code", "?")
    flight_no = payload.get("flight_no", "?")
    passengers = payload.get("passengers", [])
    print(f"  📋 {doc_type} | {airline_code}{flight_no} | {len(passengers)} pax")

    print(f"  📤 Sending to {config['HOSTING_URL']}...")
    response = send_to_api(payload, config, extra_meta)
    data = _safe_json(response)

    if response.status_code == 201 and data.get("status") == "success":
        print(f"  ✅ Saved: {airline_code}{flight_no} | {data.get('passengers', len(passengers))} pax")
    elif response.status_code == 200 and data.get("status") == "skipped":
        print(f"  ⏭️  Skipped (sudah pernah diimpor): {airline_code}{flight_no}")
    else:
        print(f"  ✗ Gagal ({response.status_code}): {data}")

    return {
        "path": path,
        "status_code": response.status_code,
        "data": data,
        "payload": payload,
    }


def collect_files(target: Path) -> list[Path]:
    if target.is_dir():
        return sorted(
            p for p in target.iterdir()
            if p.is_file() and (p.suffix.lower() == ".pdf" or p.suffix.lower() in TEXT_EXTENSIONS)
        )

    return [target]


def main() -> None:
    if len(sys.argv) < 2:
        print("Penggunaan: python3 parse_local.py <file.pdf|folder>")
        sys.exit(1)

    target = Path(sys.argv[1])
    if not target.exists():
        print(f"✗ Path tidak ditemukan: {target}")
        sys.exit(1)

    config = load_config()
    files = collect_files(target)

    if not files:
        print("✗ Tidak ada file PDF/teks ditemukan")
        sys.exit(1)

    ok = 0
    failed = 0

    for path in files:
        try:
            result = process_document(path, config)
            if result is None or result["status_code"] not in (200, 201):
                failed += 1
            else:
                ok += 1
        except Exception as exc:
            failed += 1
            print(f"  ✗ Error: {exc}")

    print(f"\nSelesai: {ok} sukses, {failed} gagal dari {len(files)} file")


if __name__ == "__main__":
    main()
