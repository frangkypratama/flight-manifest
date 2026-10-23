Kamu menambal nama penumpang yang TERPOTONG pada laporan bagasi, memakai daftar penumpang manifest dari penerbangan dan tanggal yang sama sebagai bukti.

Berkas di folder kerja: `hasil.json` (laporan bagasi hasil bacaan) dan `referensi.json` (daftar `{name, pnr}` penumpang manifest penerbangan yang sama). Baca keduanya dengan tool Read.

Laporan bagasi mencetak nama dengan lebar kolom tetap, sehingga nama sering terpotong (misalnya `PURNAMASARI/` tanpa nama depan, atau nama belakang saja). Database memakai nama sebagai kunci unik, jadi nama bagasi harus sama persis dengan nama di manifest, kalau tidak penumpang yang sama tercatat dua kali.

Keluaranmu dibaca program. Kembalikan HANYA satu objek JSON valid, tanpa markdown.

## Aturan penambalan (semua harus terpenuhi)
Untuk setiap penumpang di `hasil.json`:
1. Kalau `name`-nya sudah sama persis dengan salah satu nama di `referensi.json` yang ber-PNR sama: tidak perlu diubah.
2. Kalau tidak: cari di `referensi.json` entri dengan PNR yang SAMA. Satu PNR bisa dipakai beberapa penumpang (satu pemesanan, banyak orang), jadi ambil semua kandidat ber-PNR sama.
3. Dari kandidat itu, nama bagasi harus merupakan potongan awal (awalan) dari tepat SATU nama referensi, dengan membandingkan huruf tanpa memperhatikan huruf besar/kecil dan spasi ganda. Pemisah nama belakang/depan boleh `/`.
4. Hanya jika tepat satu kandidat yang cocok, buat patch `ubah` untuk `name` dengan nama lengkap dari referensi (persis, tanpa diubah).
5. Selain itu (tidak ada kandidat, lebih dari satu kandidat yang cocok, atau awalan tidak cocok): JANGAN menebak. Jangan buat patch. Masukkan ke `ragu`.

## Format keluaran
{
  "patches": [
    {"op": "ubah", "baris": 7, "field": "name", "baru": "NAMA LENGKAP/DARI REFERENSI", "sumber": "dokumen_lain",
     "bukti": "baris 7: PNR sama dengan referensi, awalan nama cocok, satu kandidat"}
  ],
  "ragu": [{"baris": 9, "alasan": "dua kandidat ber-PNR sama, keduanya cocok awalan"}],
  "alasan": "satu kalimat"
}

Nomor `baris` merujuk ke urutan penumpang di `hasil.json` (mulai dari 1).

## Larangan
- Jangan menulis nama atau PNR di `bukti`, `alasan`, atau `ragu`. Rujuk dengan nomor baris. Nama hanya boleh ada di bidang `baru`.
- Jangan mengubah field selain `name`.
