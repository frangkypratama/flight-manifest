#!/usr/bin/env python3
"""Simpan lampiran satu email ke sample/uid<N>/ dan, untuk .txt, tampilkan bentuk barisnya
dengan nama penumpang disamarkan (huruf >= 3 berurutan diganti X, kecuali CHD/INF/KUL/BTJ).

Pakai:  python3 ambil_lampiran.py 12          # lampiran email UID 12
        python3 ambil_lampiran.py 12 4        # tampilkan 4 baris penumpang pertama (default 5)
"""
import email
import imaplib
import os
import re
import sys

import worker

uid = sys.argv[1] if len(sys.argv) > 1 else sys.exit(__doc__)
jml = int(sys.argv[2]) if len(sys.argv) > 2 else 5
env = worker.load_env()
m = imaplib.IMAP4_SSL(env.get("IMAP_HOST", "mail.bcbandaaceh.id"), 993)
m.login(env["IMAP_USER"], env["IMAP_PASS"])
m.select("INBOX", readonly=True)
typ, d = m.uid("fetch", uid, "(BODY.PEEK[])")
raw = next((x[1] for x in d if isinstance(x, tuple)), None)
if raw is None:
    sys.exit(f"UID {uid} tidak ditemukan")
msg = email.message_from_bytes(raw)
out = os.path.join(worker.HERE, "sample", f"uid{uid}")
os.makedirs(out, exist_ok=True)
SAFE = {"CHD", "INF", "KUL", "BTJ", "PEN"}
mask = lambda t: re.sub(r"[A-Za-z]{3,}", lambda x: x.group(0) if x.group(0).upper() in SAFE else "X" * len(x.group(0)), t)
for name, data in worker.iter_pdfs(msg):
    path = os.path.join(out, re.sub(r"[^\w.\- ]", "_", name))
    open(path, "wb").write(data)
    print(f"tersimpan: {path} ({len(data)} byte)")
    if name.lower().endswith(".txt"):
        lines = data.decode("utf-8", "replace").splitlines()
        isi = [i for i, l in enumerate(lines) if re.match(r"\s*\d{1,3}\s", l)]
        awal = isi[0] if isi else 0
        print("--- kepala dokumen (apa adanya, tanpa baris penumpang) ---")
        print("\n".join(lines[:awal]))
        print(f"--- {jml} baris penumpang pertama beserta baris lanjutan (nama disamarkan) ---")
        print("\n".join(mask(l) for l in lines[awal:awal + jml * 3]))
        print("--- 12 baris terakhir (Totals) ---")
        print("\n".join(lines[-12:]))
