# CLAUDE-VPS.md — Worker Manifest (Sisi VPS)

## Konteks

Worker Python di VPS yang memproses manifest penerbangan dari email, lalu mengirim hasilnya ke API Laravel di `flight.bcbandaaceh.id`.

Email manifest masuk lewat **forward Gmail ke mailbox `manifest@bcbandaaceh.id`** (filter Gmail). Worker membaca mailbox itu lewat IMAP. Tidak ada Gmail API dan tidak ada OAuth Google.

## Alur

1. Login IMAP ke `manifest@bcbandaaceh.id`, ambil email baru (UID lebih besar dari `last_uid` di `state.json`).
2. Ambil semua lampiran PDF, termasuk PDF di dalam lampiran `.eml` (`message/rfc822`).
3. `claude -p` membaca PDF itu langsung (tool Read, tanpa `pdftotext`) dan menghasilkan JSON sesuai `prompts/parse_flight_doc.md`.
4. Worker menormalkan kunci unik (tanggal `DDMMMYY`, nomor penerbangan angka saja, nama dengan pemisah `/`) dan mencocokkan hasil dengan total yang tertulis di dokumen. Kalau tidak cocok, JSON tidak dikirim.
5. POST ke `HOSTING_URL` dengan header `X-API-Key`.
6. Status "selesai" hanya dicatat setelah server membalas 200 atau 201.

## File

```
vps-worker/
├── worker.py                  ← skrip utama
├── prompts/parse_flight_doc.md
├── .env                       ← IMAP_HOST, IMAP_USER, IMAP_PASS, HOSTING_URL, API_KEY, CLAUDE_TIMEOUT
├── .env.example
├── state.json                 ← dibuat otomatis (UID terakhir, lampiran selesai, percobaan ulang)
└── out/                       ← JSON hasil per dokumen (berisi nama penumpang, jangan dikomit)
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
