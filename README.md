# Hermes Discord Bot

Bot Discord untuk menyuruh Hermes lewat tag (`@bot ...`). Pasang di VPS yang sama dengan Hermes.

Cara pakai (di channel server, bukan DM):

- `@bot ping` cek bot hidup dan lihat latensinya
- `@bot <perintah>` kirim perintah ke Hermes dan tampilkan jawabannya
- `@bot proyek <ide>` jalankan `pipeline.py` langsung (tanpa lewat Hermes), lalu kirim file hasilnya

Hanya user yang ID-nya ada di `ALLOWED_USER_IDS` yang dilayani. Pesan dari user lain dibalas "Lau siape mpruy".

## Pasang

```bash
# Node 20 (sekali saja)
curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash -
sudo apt install -y nodejs
sudo npm install -g pm2

cd hermes-dc-bot
npm install
cp .env.example .env
nano .env                 # isi token, ID Discord lu, dan pengaturan lain (lihat tabel di bawah)
which hermes              # salin path-nya ke HERMES_BIN
pm2 start index.js --name hermes-dc
pm2 save
pm2 logs hermes-dc
```

Kalau log menampilkan `Logged in as ...`, bot sudah online.

Tidak perlu `deploy-commands.js`, `CLIENT_ID`, atau `GUILD_ID`, karena bot ini tidak memakai slash command. Setelah mengubah `.env`, jalankan `pm2 restart hermes-dc`.

## Ambil token dan ID

1. discord.com/developers/applications -> New Application
2. Tab Bot -> Reset Token -> salin ke `DISCORD_TOKEN`
3. OAuth2 -> URL Generator -> centang scope `bot`, lalu beri izin **View Channels**, **Send Messages**, **Read Message History**, dan **Attach Files** (untuk mengirim hasil pipeline). Buka URL-nya untuk mengundang bot ke server.
4. Discord: Settings -> Advanced -> Developer Mode, lalu klik kanan nama lu -> Copy User ID -> `ALLOWED_USER_IDS` (beberapa ID dipisah koma)

Intent **Message Content** tidak perlu diaktifkan, karena Discord tetap mengirim isi pesan yang nge-tag bot.

## Pengaturan `.env`

| Variabel | Wajib | Default | Fungsi |
|---|---|---|---|
| `DISCORD_TOKEN` | ya | - | Token bot |
| `ALLOWED_USER_IDS` | ya | - | ID user yang boleh memakai bot, dipisah koma |
| `HERMES_BIN` | tidak | `hermes` | Path lengkap ke perintah `hermes` |
| `HERMES_TIMEOUT_MIN` | tidak | `0` | Batas waktu per perintah Hermes dalam menit. `0` = tanpa batas |
| `HERMES_CONTEXT` | tidak | kosong | Teks yang dikirim ke Hermes di setiap perintah |
| `PIPELINE_PATH` | untuk `proyek` | - | Path lengkap ke `pipeline.py` |
| `PIPELINE_PYTHON` | tidak | `python3` | Python yang dipakai untuk menjalankan pipeline |
| `PIPELINE_KEYWORD` | tidak | `proyek` | Kata kunci pemicu pipeline |
| `PIPELINE_TIMEOUT_MIN` | tidak | `0` | Batas waktu pipeline dalam menit. `0` = tanpa batas |
| `PIPELINE_AUTO` | tidak | `1` | Permintaan membuat proyek ("buatkan web ...", "bikin bot ...") otomatis lewat pipeline. Isi `0` untuk mematikan |
| `BOT_NAME` | tidak | `Eruu` | Nama yang tampil di judul embed progres |

Contoh:

```
DISCORD_TOKEN=isi_token_bot
ALLOWED_USER_IDS=123456789012345678
HERMES_BIN=/usr/local/bin/hermes
HERMES_TIMEOUT_MIN=10
HERMES_CONTEXT=Server proyek ada di ssh stb
PIPELINE_PATH=/home/user/pipeline/pipeline.py
PIPELINE_PYTHON=python3
PIPELINE_KEYWORD=proyek
PIPELINE_TIMEOUT_MIN=20
```

## Fitur `proyek` (pipeline)

Kalau pesan diawali kata kunci (default `proyek`) diikuti spasi, bot menjalankan:

```
python3 pipeline.py --name <nama-proyek> -- <ide>
```

Contoh: `@bot proyek buatkan web html statis profil sederhana`

- Pipeline berjalan lewat tahap Gemini -> DeepSeek -> Sonnet -> cek -> DeepSeek. Progresnya tampil sebagai satu embed yang di-edit tiap langkah (✅ selesai, ⏳ berjalan, ❌ gagal), termasuk tiap file yang dibuat atau diubah Sonnet, cek sintaks, pemeriksaan DeepSeek, dan putaran perbaikan.
- `pipeline.py` mengirim langkah-langkah itu sebagai baris `@@{json}` kalau env `PIPELINE_EVENTS=1` (bot mengisinya otomatis). Tanpa env itu, output pipeline di terminal tetap bersih.
- Nama file muncul saat Sonnet menulisnya (output model di-stream). Kalau penyedia API tidak mendukung stream, pipeline otomatis pakai panggilan biasa dan file muncul sekaligus.
- Ringkasan akhir diambil dari baris `5/5 Selesai` sampai habis. Kalau format output itu diubah, ringkasan tidak akan muncul.
- Hasil disimpan di folder `projects/<nama-proyek>` di samping `pipeline.py`. Nama folder dibuat dari ide plus tanggal dan jam.
- Setelah selesai, file hasil dikirim sebagai lampiran: maksimal 10 file, masing-masing maksimal 500 KB, file kode dulu baru dokumen `.md`/`.txt`. Subfolder ditulis dengan pemisah `__` di nama lampiran.
- Kata kuncinya harus ada tepat di awal pesan dan diikuti spasi, tanpa membedakan huruf besar/kecil. "proyeksi ..." tidak dianggap perintah.

## Skill (aturan kualitas)

Semua file `.md` di folder `skills/` (di samping `pipeline.py`) dimuat otomatis dan ditempel ke prompt Sonnet saat menulis dan memperbaiki kode, juga ke DeepSeek saat memeriksa. Aturan yang dilanggar dihitung sebagai masalah dan dikirim balik ke Sonnet. Di embed muncul langkah `Memuat skill: ...`.

- Contoh bawaan: `skills/anti-slop.md`.
- Tambah skill baru dengan menaruh file `.md` baru di folder itu. Tidak perlu restart, file dibaca ulang tiap proyek.
- Pilih skill dari pesan Discord dengan `skill:` di akhir pesan atau di baris sendiri, contoh: `@bot buatkan web portofolio html + node js skill: anti-slop, seo`. Bagian `skill: ...` tidak ikut dikirim sebagai ide. Tanpa `skill:`, semua skill di folder dimuat. Nama yang tidak ada ditulis sebagai catatan di embed.
- Untuk pemakaian lewat terminal, isi env `SKILLS=anti-slop,nama-lain`.
- Total isi skill dibatasi `SKILLS_MAX_CHARS` (default 16000 karakter) supaya biaya token terjaga. Skill yang tidak muat dilewati dan muncul sebagai catatan di embed.
- Bagian front matter (`---` ... `---` dengan `name:` dan `description:`) di awal file dibuang otomatis. Nama skill diambil dari nama file.
- Bawaan: `anti-slop` (kode), `anti-slop-ui` (tampilan), `anti-slop-id` (tulisan bahasa Indonesia).

## Catatan

- Perintah biasa ke Hermes juga tampil sebagai embed, tapi hanya dengan langkah antri -> Eruu Bot memproses -> selesai, karena `hermes -z` hanya mengembalikan jawaban akhir.
- Hermes dan pipeline berbagi satu antrian, jadi dijalankan satu per satu. Pesan status memberi tahu kalau ada perintah di depan.
- Waktu tunggu di antrian tidak dihitung ke batas waktu. Batas waktu mulai saat proses benar-benar jalan.
- Kalau batas waktu habis, proses dihentikan. Untuk pipeline, hasil sebagian mungkin ada di folder `projects/<nama-proyek>`.
- Kalau `hermes` tidak ditemukan saat jalan di pm2, isi `HERMES_BIN` dengan path lengkap.
- Kalau Python tidak ditemukan, isi `PIPELINE_PYTHON` dengan path lengkapnya.
- Jawaban panjang dipecah jadi beberapa pesan karena batas Discord 2000 karakter.
- Kalau file bot di server masih bernama `index1.js`, ganti namanya jadi `index.js` atau sesuaikan perintah `pm2 start`.
