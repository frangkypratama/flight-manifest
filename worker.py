#!/usr/bin/env python3
"""Worker manifest penerbangan.

Alur: mailbox IMAP -> lampiran PDF -> `claude -p` (Claude membaca PDF langsung
dan membersihkan data) -> pemeriksaan -> POST ke flight.bcbandaaceh.id.

Tidak ada parser regex untuk isi PDF. Kode di sini hanya:
  - menormalkan nilai yang menjadi kunci unik di database,
  - mencocokkan hasil Claude dengan total yang tertulis di dokumen itu sendiri,
  - mengirim dan mencatat status supaya tidak terkirim dua kali.

Pemakaian:
  python3 worker.py --file sample/contoh_manifest.pdf --dry-run   # uji satu PDF
  python3 worker.py --mailbox --dry-run                           # uji baca mailbox
  python3 worker.py --mailbox                                     # jalan sungguhan (cron)
"""
import argparse
import datetime
import email
import fcntl
import hashlib
import imaplib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from email.header import decode_header, make_header

HERE = os.path.dirname(os.path.abspath(__file__))
PROMPT_PATH = os.path.join(HERE, "prompts", "parse_flight_doc.md")
STATE_PATH = os.path.join(HERE, "state.json")
LOCK_PATH = os.path.join(HERE, "worker.lock")
OUT_DIR = os.path.join(HERE, "out")
MAX_ATTEMPTS = 3
MONTHS = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]


def log(level, msg):
    print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} [{level}] {msg}", flush=True)


def load_env(path=os.path.join(HERE, ".env")):
    env = dict(os.environ)
    if os.path.exists(path):
        for line in open(path, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    return env


# ------------------------------------------------------------------- Claude
def run_claude(data: bytes, env: dict) -> str:
    """Claude membaca PDF-nya sendiri lewat tool Read (satu-satunya tool yang diizinkan)."""
    prompt = open(PROMPT_PATH, encoding="utf-8").read()
    timeout = int(env.get("CLAUDE_TIMEOUT", "420"))
    # cwd folder sementara berisi hanya dokumen.pdf, supaya Claude tidak membaca CLAUDE.md milik repo.
    with tempfile.TemporaryDirectory() as work:
        with open(os.path.join(work, "dokumen.pdf"), "wb") as f:
            f.write(data)
        stdin = prompt + "\n\nDokumen: dokumen.pdf (di folder kerja saat ini). Baca semua halamannya dengan tool Read."
        cmd = ["claude", "-p", "--output-format", "text", "--tools", "Read",
               "--allowedTools", "Read", "--no-session-persistence"]
        log("INFO", "Claude mulai membaca PDF (biasanya 1-2 menit)...")
        t0 = time.time()
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, cwd=work, env=env)  # env: termasuk CLAUDE_CODE_OAUTH_TOKEN dari .env
        payload = stdin
        while True:
            try:
                out, err = proc.communicate(input=payload, timeout=15)
                break
            except subprocess.TimeoutExpired:
                payload = None
                waited = int(time.time() - t0)
                if waited >= timeout:
                    proc.kill()
                    proc.communicate()
                    raise RuntimeError(f"claude -p timeout setelah {timeout}s")
                log("INFO", f"Claude masih bekerja... {waited}s")
        r = subprocess.CompletedProcess(cmd, proc.returncode, out, err)
    if r.returncode != 0:
        detail = (r.stderr.strip() or r.stdout.strip() or "(tanpa keluaran)")[:300]
        raise RuntimeError(f"claude exit {r.returncode}: {detail}")
    return r.stdout


def extract_json(raw: str) -> dict:
    raw = raw.strip()
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw)
    start = raw.find("{")
    if start < 0:
        raise ValueError("tidak ada objek JSON di keluaran Claude")
    obj, _ = json.JSONDecoder().raw_decode(raw[start:])
    return obj


# --------------------------------------------------------------- normalisasi
def blank_to_none(v):
    if v is None:
        return None
    v = str(v).strip()
    return None if v in ("", "-", "null", "None") else v


def norm_date(v):
    s = str(v or "").strip().upper()
    s = re.sub(r"/\d{4}$", "", s)  # buang jam: 01OCT26/0735
    m = re.match(r"^(\d{1,2})[\s\-/.]*([A-Z]{3})[A-Z]*[\s\-/.]*(\d{4}|\d{2})$", s)
    if not m or m.group(2) not in MONTHS:
        return None
    day, yy = int(m.group(1)), int(m.group(3)[-2:])
    try:
        datetime.date(2000 + yy, MONTHS.index(m.group(2)) + 1, day)  # tolak tanggal yang tidak ada
    except ValueError:
        return None
    return f"{day:02d}{m.group(2)}{yy:02d}"


def norm_name(v):
    # sama dengan ManifestImportController::normalizeName
    return re.sub(r"\s+", " ", str(v or "").replace(",", "/")).strip()


def normalize(p: dict):
    """Kembalikan (payload_bersih, daftar_error)."""
    errors = []
    out = {"doc_type": p.get("doc_type")}
    out["airline"] = blank_to_none(p.get("airline"))
    out["airline_code"] = (blank_to_none(p.get("airline_code")) or "").upper()
    fn = re.sub(r"\D", "", str(p.get("flight_no") or ""))
    out["flight_no"] = fn
    route = re.sub(r"[^A-Za-z]", "", str(p.get("route") or "")).upper()
    out["route"] = route or None
    out["flight_date"] = norm_date(p.get("flight_date"))
    out["summary"] = p.get("summary") or {}
    out["notes"] = p.get("notes") or []

    if out["doc_type"] not in ("baggage", "manifest", "enhanced_manifest"):
        errors.append(f"doc_type tidak dikenal: {out['doc_type']!r}")
    if not out["airline"]:
        errors.append("airline kosong")
    if not re.fullmatch(r"[A-Z0-9]{2}", out["airline_code"]):
        errors.append(f"airline_code tidak valid: {out['airline_code']!r}")
    if not fn:
        errors.append("flight_no kosong")
    if out["route"] is not None and len(out["route"]) != 6:
        errors.append(f"route harus 6 huruf: {out['route']!r}")
    if not out["flight_date"]:
        errors.append(f"flight_date tidak terbaca: {p.get('flight_date')!r}")

    pax = []
    for i, q in enumerate(p.get("passengers") or [], 1):
        name = norm_name(q.get("name"))
        if not name:
            errors.append(f"penumpang #{i}: nama kosong")
            continue
        status = str(q.get("status") or "").lower().replace("-", "").replace("_", "").replace(" ", "")
        if status not in ("boarded", "noshow"):
            errors.append(f"penumpang #{i}: status tidak valid {q.get('status')!r}")
        ptype = (blank_to_none(q.get("pax_type")) or "ADT").upper()
        if ptype not in ("ADT", "CHD", "INF"):
            errors.append(f"penumpang #{i}: pax_type tidak valid {ptype!r}")
        bw = q.get("bag_weight")
        if bw is not None:
            try:
                bw = float(bw)
                bw = int(bw) if bw == int(bw) else bw
            except (TypeError, ValueError):
                errors.append(f"penumpang #{i}: bag_weight bukan angka {bw!r}")
                bw = None
        tags = q.get("bag_tags")
        tags = [str(t) for t in tags] if isinstance(tags, list) and tags else None
        pax.append({
            "name": name,
            "pnr": (blank_to_none(q.get("pnr")) or "").upper() or None,
            "seat": (blank_to_none(q.get("seat")) or "").upper() or None,
            "fare_class": (blank_to_none(q.get("fare_class")) or "").upper() or None,
            "gender": blank_to_none(q.get("gender")),
            "pax_type": ptype,
            "bag_weight": bw,
            "bag_tags": tags,
            "status": status,
            "ticket_no": blank_to_none(q.get("ticket_no")),
        })
    out["passengers"] = pax
    if not pax:
        errors.append("daftar penumpang kosong")
    return out, errors


def check_totals(p: dict):
    """Cocokkan hasil Claude dengan total yang tertulis di dokumen. Kembalikan (error, warning)."""
    errors, warns = [], []
    s = p["summary"]
    pax = p["passengers"]
    adult = [q for q in pax if q["pax_type"] != "INF"]
    n_board = sum(1 for q in adult if q["status"] == "boarded")
    n_ns = sum(1 for q in adult if q["status"] == "noshow")
    n_inf = sum(1 for q in pax if q["pax_type"] == "INF")

    def cmp(label, got, want, required=False):
        if want is None:
            if required:
                errors.append(f"total '{label}' tidak ada di summary, hasil tidak bisa diverifikasi")
            return
        if got != want:
            errors.append(f"{label}: terbaca {got}, di dokumen {want}")

    if p["doc_type"] == "manifest":
        cmp("boarded", n_board, s.get("total_boarded"), required=True)
        cmp("no-show", n_ns, s.get("total_no_shows"), required=True)
        cmp("bayi", n_inf, s.get("infant"))
        cmp("manifested", n_board + n_ns, s.get("total_pax"))
    elif p["doc_type"] == "baggage":
        cmp("jumlah penumpang", len(pax), s.get("total_pax"), required=True)
        total_w = sum(q["bag_weight"] or 0 for q in pax)
        cmp("total berat", total_w, s.get("total_weight_kg"))
    else:
        cmp("jumlah penumpang", len(adult), s.get("total_pax"))

    names = [q["name"] for q in pax]
    dup = sorted({n for n in names if names.count(n) > 1})
    if dup:
        warns.append(f"{len(dup)} nama kembar dalam satu dokumen (lihat out/*.json): "
                     "database hanya menyimpan satu baris per nama, jumlah di database bisa kurang dari dokumen")
    for n in p.get("notes") or []:
        warns.append(f"catatan Claude: {n}")
    return errors, warns


# -------------------------------------------------------------------- kirim
def post(payload: dict, env: dict):
    url, key = env.get("HOSTING_URL"), env.get("API_KEY")
    if not url or not key:
        raise RuntimeError("HOSTING_URL / API_KEY belum diisi di .env")
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "Content-Type": "application/json", "Accept": "application/json", "X-API-Key": key})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


# ------------------------------------------------------------------- 1 file
def process_document(filename, data, meta, env, dry_run):
    """Kembalikan 'ok' | 'retry' | 'giveup'."""
    sha = hashlib.sha256(data).hexdigest()
    t0 = time.time()
    try:
        raw = run_claude(data, env)
        parsed = extract_json(raw)
    except Exception as e:
        log("ERROR", f"{filename}: Claude gagal: {e}")
        return "retry"
    secs = time.time() - t0

    if parsed.get("error"):
        log("WARN", f"{filename}: bukan dokumen yang didukung ({parsed.get('reason')}), dilewati")
        return "giveup"

    payload, errs = normalize(parsed)
    if not errs:
        e2, warns = check_totals(payload)
        errs += e2
    else:
        warns = []
    n = len(payload["passengers"])
    head = f"{payload['airline_code']}{payload['flight_no']} {payload['flight_date']} {payload['route']} | {payload['doc_type']} | {n} baris | Claude {secs:.0f}s"

    os.makedirs(OUT_DIR, exist_ok=True)
    with open(os.path.join(OUT_DIR, os.path.basename(filename) + ".json"), "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    for w in warns:
        log("WARN", f"{filename}: {w}")
    if errs:
        for e in errs:
            log("ERROR", f"{filename}: {e}")
        log("ERROR", f"{filename}: TIDAK dikirim ({head})")
        return "retry"
    if dry_run:
        log("OK", f"{filename}: lolos pemeriksaan, tidak dikirim (--dry-run) | {head}")
        return "ok"

    send = {k: payload[k] for k in ("doc_type", "airline", "airline_code", "flight_no",
                                    "route", "flight_date", "summary", "passengers")}
    # ID unik per lampiran: controller melewati (skipped) kiriman kedua yang
    # gmail_message_id-nya sama, padahal satu email bisa membawa >1 penerbangan.
    send["gmail_message_id"] = f"{meta['message_id']}#{sha[:8]}"
    send["filename"] = filename
    send["sender_email"] = meta.get("sender")
    try:
        code, body = post(send, env)
    except Exception as e:
        log("ERROR", f"{filename}: gagal menghubungi server: {e}")
        return "retry"
    if code in (200, 201):
        log("OK", f"{filename}: server {code} | {head} | {body[:120]}")
        return "ok"
    if code == 401:
        raise SystemExit("API key ditolak (401). Periksa API_KEY di .env dan MANIFEST_API_KEY di server.")
    log("ERROR", f"{filename}: server {code}: {body[:300]}")
    return "retry"


# ------------------------------------------------------------------ mailbox
def dh(v):
    return str(make_header(decode_header(v or "")))


def iter_pdfs(msg, depth=0):
    """Semua lampiran PDF, termasuk yang ada di dalam lampiran .eml / message/rfc822."""
    n = 0
    for part in msg.walk():
        name = dh(part.get_filename()) if part.get_filename() else ""
        ctype = part.get_content_type()
        if part.is_multipart():
            continue  # wadah; isinya ikut dijelajahi oleh walk()
        if (name.lower().endswith(".eml") or ctype == "message/rfc822") and depth < 3:
            blob = part.get_payload(decode=True)  # .eml yang dikirim sebagai file biasa
            if blob:
                yield from iter_pdfs(email.message_from_bytes(blob), depth + 1)
            continue
        if name.lower().endswith(".pdf") or ctype == "application/pdf":
            data = part.get_payload(decode=True)
            if data:
                n += 1
                yield (name or f"lampiran-{depth}-{n}.pdf"), data


def load_state():
    if os.path.exists(STATE_PATH):
        return json.load(open(STATE_PATH, encoding="utf-8"))
    return {"uidvalidity": None, "last_uid": 0, "done": {}, "attempts": {}}


def save_state(st):
    tmp = STATE_PATH + ".tmp"
    json.dump(st, open(tmp, "w", encoding="utf-8"), indent=1)
    os.replace(tmp, STATE_PATH)


def run_mailbox(env, dry_run, limit):
    lock = open(LOCK_PATH, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log("INFO", "proses lain masih berjalan, keluar")
        return 0

    need = ["IMAP_USER", "IMAP_PASS"] + ([] if dry_run else ["HOSTING_URL", "API_KEY"])
    kosong = [k for k in need if not str(env.get(k, "")).strip()]
    if kosong:
        log("ERROR", f"isi dulu di .env: {', '.join(kosong)} (nilai tidak ditampilkan)")
        return 2

    st = load_state()
    m = imaplib.IMAP4_SSL(env.get("IMAP_HOST", "mail.bcbandaaceh.id"), 993)
    try:
        m.login(env["IMAP_USER"], env["IMAP_PASS"])
    except imaplib.IMAP4.error as e:
        log("ERROR", f"login IMAP ditolak untuk {env['IMAP_USER']}: {e}. Periksa IMAP_USER dan IMAP_PASS di .env")
        return 2
    m.select("INBOX", readonly=True)
    uv = int(m.response("UIDVALIDITY")[1][0])
    if st["uidvalidity"] != uv:
        if st["uidvalidity"] is not None:
            log("WARN", "UIDVALIDITY berubah, mulai ulang dari awal (lampiran yang sudah terkirim tetap dilewati)")
        st.update(uidvalidity=uv, last_uid=0)

    _, data = m.uid("SEARCH", None, "ALL")
    uids = [int(u) for u in data[0].split() if int(u) > st["last_uid"]]
    log("INFO", f"{len(uids)} email baru (UID > {st['last_uid']})")
    if limit:
        uids = uids[:limit]

    advance = True
    failed = 0
    for uid in uids:
        _, d = m.uid("FETCH", str(uid), "(BODY.PEEK[])")
        msg = email.message_from_bytes(d[0][1])
        subj, sender = dh(msg["Subject"]), dh(msg["From"])
        mid = (msg["Message-ID"] or f"uid-{uv}-{uid}").strip()

        if "mailer-daemon" in sender.lower() or subj.startswith("Delivery Status Notification"):
            log("WARN", f"UID {uid}: pesan gagal-kirim dari {sender!r}. Ada email manifest yang TIDAK sampai ke mailbox ini")
        status = "ok"
        for fname, blob in iter_pdfs(msg):
            key = f"{mid}|{fname}|{hashlib.sha256(blob).hexdigest()[:16]}"
            if key in st["done"]:
                continue
            log("INFO", f"UID {uid} | {sender} | {fname}")
            r = process_document(fname, blob, {"message_id": mid, "sender": sender}, env, dry_run)
            if dry_run:
                continue
            if r == "retry":
                n = st["attempts"].get(key, 0) + 1
                st["attempts"][key] = n
                if n >= MAX_ATTEMPTS:
                    log("ERROR", f"{fname}: gagal {n}x, DIABAIKAN. Perlu dicek manual")
                    st["done"][key] = "giveup"
                else:
                    status = "retry"
            else:
                st["done"][key] = r
        if status == "retry":
            advance = False
            failed += 1
        if advance and not dry_run:
            st["last_uid"] = uid
        if not dry_run:
            save_state(st)
    m.logout()
    log("INFO", f"selesai: {len(uids)} email diperiksa, {failed} perlu diulang")
    return 1 if failed else 0


def main():
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--file", help="proses satu PDF lokal")
    g.add_argument("--mailbox", action="store_true", help="proses email baru di mailbox")
    ap.add_argument("--dry-run", action="store_true", help="jangan kirim ke server, jangan ubah state")
    ap.add_argument("--limit", type=int, default=0, help="batasi jumlah email per putaran")
    a = ap.parse_args()
    env = load_env()
    if a.file:
        data = open(a.file, "rb").read()
        sha = hashlib.sha256(data).hexdigest()[:8]
        r = process_document(os.path.basename(a.file), data,
                             {"message_id": f"local-{sha}", "sender": None}, env, a.dry_run)
        sys.exit(0 if r == "ok" else 1)
    sys.exit(run_mailbox(env, a.dry_run, a.limit))


if __name__ == "__main__":
    main()
