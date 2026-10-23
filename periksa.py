#!/usr/bin/env python3
"""Ringkas dokumen karantina di out/review/ TANPA menampilkan nama penumpang.

Pakai:  python3 periksa.py              -> semua file karantina
        python3 periksa.py IU169        -> hanya yang namanya memuat 'IU169'
"""
import collections
import glob
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
kata = sys.argv[1].lower() if len(sys.argv) > 1 else ""
files = sorted(glob.glob(os.path.join(HERE, "out", "review", "*.json")))
files = [f for f in files if kata in os.path.basename(f).lower()]
if not files:
    print("tidak ada file karantina yang cocok di out/review/")

for f in files:
    d = json.load(open(f, encoding="utf-8"))
    p, alasan = d.get("payload", {}), d.get("alasan", [])
    pax = p.get("passengers") or []
    print("=" * 70)
    print(os.path.basename(f))
    print("header :", {k: p.get(k) for k in ("doc_type", "airline", "airline_code", "flight_no", "route", "flight_date")})
    print("summary:", p.get("summary"))
    print("penumpang:", len(pax), "| status:", dict(collections.Counter(str(q.get("status")) for q in pax)),
          "| pax_type:", dict(collections.Counter(str(q.get("pax_type")) for q in pax)))
    if pax:
        print("baris 1 :", {k: ("X" if k == "name" else v) for k, v in pax[0].items()})
    print("koreksi :", [(c.get("tahap"), c.get("field"), c.get("sumber")) for c in p.get("koreksi", [])][:10])
    print("notes   :")
    for n in p.get("notes") or []:
        print("  -", n)
    print(f"alasan  : {len(alasan)} total; ringkasan jenis:")
    for a, n in collections.Counter(x.split(":", 1)[-1].strip()[:90] for x in alasan).most_common(8):
        print(f"  {n}x {a}")
