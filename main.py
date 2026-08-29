#!/usr/bin/env python3
"""Script produksi: ambil PDF dari Gmail, parse dengan Claude, kirim ke API Laravel."""

import base64
import json
import pickle
import sys
from datetime import datetime
from pathlib import Path

from google.auth.transport.requests import Request
from googleapiclient.discovery import build

from parse_local import call_claude, extract_json, extract_text, load_config, send_to_api

BASE_DIR = Path(__file__).resolve().parent
CREDENTIALS_PATH = BASE_DIR / "credentials.json"
TOKEN_PATH = BASE_DIR / "token.pickle"
DOWNLOADS_DIR = BASE_DIR / "downloads"
LOGS_DIR = BASE_DIR / "logs"
PROCESSED_IDS_PATH = BASE_DIR / "processed_ids.json"

MAX_PROCESSED_IDS = 500
GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]


def log(message: str) -> None:
    print(message)
    try:
        LOGS_DIR.mkdir(parents=True, exist_ok=True)
        log_file = LOGS_DIR / f"main-{datetime.now():%Y-%m-%d}.log"
        with log_file.open("a", encoding="utf-8") as f:
            f.write(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {message}\n")
    except OSError:
        pass


def load_processed_ids() -> list:
    if not PROCESSED_IDS_PATH.exists():
        return []
    try:
        return json.loads(PROCESSED_IDS_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []


def save_processed_ids(ids: list) -> None:
    trimmed = ids[-MAX_PROCESSED_IDS:]
    PROCESSED_IDS_PATH.write_text(json.dumps(trimmed, indent=2), encoding="utf-8")


def get_gmail_service():
    if not CREDENTIALS_PATH.exists():
        log(f"✗ credentials.json tidak ditemukan di {CREDENTIALS_PATH}")
        log("  Unduh OAuth client credentials (Desktop App) dari Google Cloud Console,")
        log(f"  simpan sebagai {CREDENTIALS_PATH}, lalu jalankan: python3 auth_gmail.py")
        sys.exit(1)

    if not TOKEN_PATH.exists():
        log("✗ token.pickle tidak ditemukan. Jalankan dulu: python3 auth_gmail.py")
        sys.exit(1)

    with TOKEN_PATH.open("rb") as f:
        creds = pickle.load(f)

    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        with TOKEN_PATH.open("wb") as f:
            pickle.dump(creds, f)

    return build("gmail", "v1", credentials=creds)


def get_sender_email(headers: list) -> str:
    raw = next((h["value"] for h in headers if h["name"].lower() == "from"), "")
    if "<" in raw and ">" in raw:
        return raw.split("<", 1)[1].split(">", 1)[0].strip()
    return raw.strip()


def find_pdf_attachments(service, message_id: str) -> tuple:
    message = service.users().messages().get(userId="me", id=message_id, format="full").execute()
    sender_email = get_sender_email(message["payload"].get("headers", []))

    attachments = []

    def walk(part):
        filename = part.get("filename") or ""
        body = part.get("body", {})
        if filename.lower().endswith(".pdf") and body.get("attachmentId"):
            attachments.append((filename, body["attachmentId"]))
        for sub_part in part.get("parts", []) or []:
            walk(sub_part)

    walk(message["payload"])

    return sender_email, attachments


def download_attachment(service, message_id: str, attachment_id: str, filename: str) -> Path:
    attachment = (
        service.users()
        .messages()
        .attachments()
        .get(userId="me", messageId=message_id, id=attachment_id)
        .execute()
    )
    data = base64.urlsafe_b64decode(attachment["data"])

    DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)
    path = DOWNLOADS_DIR / filename
    path.write_bytes(data)
    return path


def process_message(service, config: dict, message_id: str) -> bool:
    """Proses satu email. Return True jika boleh ditandai sebagai processed."""
    try:
        sender_email, attachments = find_pdf_attachments(service, message_id)
    except Exception as exc:
        log(f"✗ Gagal ambil detail email {message_id}: {exc}")
        return False

    if not attachments:
        return True

    all_ok = True

    for filename, attachment_id in attachments:
        pdf_path = None
        try:
            log(f"📄 {filename} (dari {sender_email})")
            pdf_path = download_attachment(service, message_id, attachment_id, filename)

            result = process_document_for_gmail(pdf_path, config, message_id, filename, sender_email)
            if result is None:
                all_ok = False
        except Exception as exc:
            log(f"  ✗ Error: {exc}")
            all_ok = False
        finally:
            if pdf_path and pdf_path.exists():
                pdf_path.unlink()

    return all_ok


def process_document_for_gmail(pdf_path: Path, config: dict, message_id: str, filename: str, sender_email: str):
    text = extract_text(pdf_path)
    if not text.strip():
        log("  ⚠️  Teks kosong, dilewati")
        return None

    log("  🤖 Claude parsing...")
    response_text = call_claude(text)
    payload = extract_json(response_text)

    doc_type = payload.get("doc_type", "?")
    airline_code = payload.get("airline_code", "?")
    flight_no = payload.get("flight_no", "?")
    passengers = payload.get("passengers", [])
    log(f"  📋 {doc_type} | {airline_code}{flight_no} | {len(passengers)} pax")

    log(f"  📤 Sending to {config['HOSTING_URL']}...")
    response = send_to_api(payload, config, {
        "gmail_message_id": message_id,
        "filename": filename,
        "sender_email": sender_email,
    })

    if response.status_code in (200, 201):
        log(f"  ✅ Saved: {airline_code}{flight_no}")
        return True

    log(f"  ✗ Gagal ({response.status_code}): {response.text[:300]}")
    return None


def main() -> None:
    config = load_config()
    service = get_gmail_service()

    processed_ids = load_processed_ids()
    processed_set = set(processed_ids)

    log(f"🔎 Mencari email: {config['GMAIL_QUERY']}")

    response = service.users().messages().list(
        userId="me", q=config["GMAIL_QUERY"], maxResults=50
    ).execute()
    messages = response.get("messages", [])
    new_messages = [m for m in messages if m["id"] not in processed_set]

    log(f"📬 {len(messages)} email ditemukan, {len(new_messages)} baru")

    success = 0
    failed = 0

    for msg in new_messages:
        message_id = msg["id"]
        ok = process_message(service, config, message_id)

        if ok:
            success += 1
            processed_ids.append(message_id)
            save_processed_ids(processed_ids)
        else:
            failed += 1

    log(f"\nSelesai: {success} sukses, {failed} gagal dari {len(new_messages)} email baru")


if __name__ == "__main__":
    main()
