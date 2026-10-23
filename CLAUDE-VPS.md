# CLAUDE-VPS.md — Worker Manifest (Sisi VPS)

## Konteks

Worker Python di VPS yang memproses manifest penerbangan dari email, lalu mengirim hasilnya ke API Laravel di `flight.bcbandaaceh.id`.

Email manifest masuk lewat **forward Gmail ke mailbox `manifest@bcbandaaceh.id`** (filter Gmail). Worker membaca mailbox itu lewat IMAP. Tidak ada Gmail API dan tidak ada OAuth Google.

## Alur

1. Login IMAP ke `manifest@bcbandaaceh.id`, ambil email baru (UID lebih besar dari `last_uid` di `state.json`).
2. Ambil semua lampiran PDF dan `.txt` (manifest IU dikirim sebagai `.txt`), termasuk yang ada di dalam lampiran `.eml`.
3. **Tahap 1, baca dan bersihkan.** `claude -p` membaca dokumen langsung (tool Read, tanpa `pdftotext`) lalu menghasilkan JSON menurut `prompts/parse_flight_doc.md`. Nilai yang diisi dari sumber lain (nama file, daftar maskapai) dicatat di `koreksi`, lengkap dengan sumber dan buktinya.
4. **Tahap 2, khusus laporan bagasi.** Nama di laporan bagasi sering terpotong. Claude melengkapinya dari manifest penerbangan dan tanggal yang sama (`store/`), hanya jika PNR sama dan awalan nama cocok dengan tepat satu penumpang (`prompts/patch_names.md`). Kalau manifest belum ada, laporan menunggu (maksimal 12 jam).
5. **Tahap 3, pemeriksa independen.** Panggilan `claude -p` terpisah membandingkan hasil dengan dokumen asli baris demi baris (`prompts/verify_flight_doc.md`). Kalau ada selisih, hasil dikoreksi lalu diperiksa ulang sekali.
6. Kode mencocokkan jumlah (boarded, no-show, bayi, bagasi, berat) dengan Totals dokumen, lalu POST ke `HOSTING_URL` dengan header `X-API-Key`.
7. Dokumen yang tidak bisa dipastikan benar masuk **karantina** (`out/review/`), tidak pernah dikirim. Status "selesai" hanya dicatat setelah server membalas 200 atau 201.

Kebijakan: lebih baik lambat dan mahal daripada data yang tidak sesuai. Satu dokumen memakan 2 sampai 4 panggilan Claude.

## File

```
vps-worker/
├── worker.py                  ← skrip utama
├── prompts/parse_flight_doc.md   ← tahap 1
├── prompts/patch_names.md        ← tahap 2 (bagasi)
├── prompts/verify_flight_doc.md  ← tahap 3
├── airlines.json                 ← nama maskapai → kode (hanya yang sudah disetujui)
├── store/ pending/               ← acuan manifest dan dokumen yang menunggu (dibuat otomatis)
├── .env                       ← IMAP_HOST, IMAP_USER, IMAP_PASS, HOSTING_URL, API_KEY, CLAUDE_TIMEOUT
├── .env.example
├── state.json                 ← dibuat otomatis (UID terakhir, lampiran selesai, percobaan ulang)
└── out/ (dan out/review/)     ← JSON hasil dan karantina (berisi nama penumpang, jangan dikomit)
```

## Pemakaian

```bash
python3 worker.py --file sample/contoh.pdf --dry-run   # uji satu PDF, tidak mengirim
python3 worker.py --mailbox --dry-run                  # uji baca mailbox, tidak mengubah state
python3 worker.py --mailbox                            # jalan sungguhan (dipanggil cron)
```

## Aturan penting

- JANGAN hardcode `IMAP_PASS` atau `API_KEY` di kode. Baca dari `.env`.
- JANGAN menulis nama penumpang ke log. Log hanya berisi nama file, jumlah baris, dan hasil pemeriksaan.
- `claude -p` dipanggil dengan hanya tool Read dan dari folder sementara yang hanya berisi `dokumen.pdf`, supaya tidak membaca CLAUDE.md milik repo.
- Setiap panggilan `claude` punya timeout (`CLAUDE_TIMEOUT`), setiap POST ke server 30 detik.
- Lampiran yang gagal tiga kali ditandai `giveup` dan dicatat di log. Perlu dicek manual.
- Satu lampiran dikirim dengan `gmail_message_id` = `<Message-ID>#<hash 8 karakter>`, karena controller melewati kiriman dengan ID yang sama. Nama kolomnya tetap `gmail_message_id` supaya skema tidak berubah.
- Pesan gagal-kirim dari `mailer-daemon` di mailbox dicatat sebagai peringatan. Artinya ada email manifest yang tidak sampai.
- Satu proses pada satu waktu (`flock` pada `worker.lock`).
- Cron: `*/5 * * * * cd /root/flight-manifest && python3 worker.py --mailbox >> logs/worker.log 2>&1`
