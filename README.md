# Hermes Discord Bot

Bot Discord untuk menyuruh Hermes lewat tag (`@bot ...`). Pasang di VPS/mesin yang sama dengan Hermes.

Cara pakai (di channel server, bukan DM):

- `@bot ping` cek bot hidup dan lihat latensinya
- `@bot <perintah>` kirim perintah ke Hermes (`hermes -z`) dan tampilkan jawabannya
- `@bot proyek <ide>` jalankan `pipeline.py` langsung (tanpa lewat Hermes), lalu kirim file hasilnya
- `@bot buatkan web ...` / `@bot bikin bot ...` otomatis masuk pipeline juga (lihat `PIPELINE_AUTO`)

Bot hanya merespons kalau di-tag langsung (bukan `@everyone` atau `@role`). Hanya user yang ID-nya ada di `ALLOWED_USER_IDS` yang dilayani. Pesan dari user lain dibalas "Lau siape mpruy".

## Struktur repo

```
hermes-bot/
├── index.js        # bot Discord (discord.js v14), dijalankan lewat pm2
├── pipeline.py     # pipeline pembuat proyek (Python 3, tanpa dependensi pip)
├── package.json    # dependensi Node: discord.js dan dotenv (Node >= 18)
├── skills/         # aturan kualitas (.md) yang ditempel ke prompt pipeline
└── README.md
```

File yang dibuat saat jalan, di samping `pipeline.py`:

| File / folder         | Isi                                                                    |
| --------------------- | ---------------------------------------------------------------------- |
| `projects/<nama>/`    | hasil tiap proyek                                                      |
| `usage.jsonl`         | log pemakaian token dan perkiraan biaya (Rp) tiap panggilan model      |
| `last_code_raw.txt`   | jawaban mentah model saat menulis kode, untuk debug                    |
| `keys_state.json`     | pencatat pemakaian harian dan jeda tiap key Gemini                     |
| `keys.json`           | (opsional, buat sendiri) daftar key Gemini, lihat bagian pipeline      |
| `constraints.md`      | (opsional, buat sendiri) batasan teknis pengganti bawaan               |

## Pasang

```
# Node 20 (sekali saja; minimal Node 18)
curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash -
sudo apt install -y nodejs
sudo npm install -g pm2

git clone https://github.com/eruuamamiya/hermes-bot.git
cd hermes-bot
npm install
cp .env.example .env      # kalau file contoh tidak ada, buat .env sendiri (lihat contoh di bawah)
nano .env                 # isi token, ID Discord lu, dan pengaturan lain (lihat tabel di bawah)
which hermes              # salin path-nya ke HERMES_BIN
pm2 start index.js --name hermes-dc
pm2 save
pm2 logs hermes-dc
```

Kalau log menampilkan `Logged in as ...`, bot sudah online.

Bot ini tidak memakai slash command, jadi tidak perlu `deploy-commands.js`, `CLIENT_ID`, atau `GUILD_ID`. Setelah mengubah `.env`, jalankan `pm2 restart hermes-dc`.

Untuk fitur `proyek`, server juga butuh Python 3, `PIPELINE_PATH` yang benar, dan `VIKEY_API_KEY` (lihat bagian pipeline).

## Ambil token dan ID

1. discord.com/developers/applications -> New Application
2. Tab Bot -> Reset Token -> salin ke `DISCORD_TOKEN`
3. OAuth2 -> URL Generator -> centang scope `bot`, lalu beri izin **View Channels**, **Send Messages**, **Read Message History**, **Embed Links** (progres tampil sebagai embed), dan **Attach Files** (untuk mengirim hasil pipeline). Buka URL-nya untuk mengundang bot ke server.
4. Discord: Settings -> Advanced -> Developer Mode, lalu klik kanan nama lu -> Copy User ID -> `ALLOWED_USER_IDS` (beberapa ID dipisah koma)

Intent **Message Content** tidak perlu diaktifkan, karena Discord tetap mengirim isi pesan yang nge-tag bot. Bot hanya memakai intent `Guilds` dan `GuildMessages`.

## Pengaturan `.env` (bot)

| Variabel               | Wajib          | Default   | Fungsi                                                                                                              |
| ---------------------- | -------------- | --------- | ------------------------------------------------------------------------------------------------------------------- |
| `DISCORD_TOKEN`        | ya             | -         | Token bot                                                                                                           |
| `ALLOWED_USER_IDS`     | ya             | -         | ID user yang boleh memakai bot, dipisah koma                                                                        |
| `HERMES_BIN`           | tidak          | `hermes`  | Path lengkap ke perintah `hermes`                                                                                   |
| `HERMES_TIMEOUT_MIN`   | tidak          | `0`       | Batas waktu per perintah Hermes dalam menit. `0` = tanpa batas                                                      |
| `HERMES_CONTEXT`       | tidak          | kosong    | Teks yang dikirim ke Hermes di setiap perintah                                                                      |
| `PIPELINE_PATH`        | untuk `proyek` | -         | Path lengkap ke `pipeline.py`                                                                                       |
| `PIPELINE_PYTHON`      | tidak          | `python3` | Python yang dipakai untuk menjalankan pipeline                                                                      |
| `PIPELINE_KEYWORD`     | tidak          | `proyek`  | Kata kunci pemicu pipeline                                                                                          |
| `PIPELINE_TIMEOUT_MIN` | tidak          | `0`       | Batas waktu pipeline dalam menit. `0` = tanpa batas                                                                 |
| `PIPELINE_AUTO`        | tidak          | `1`       | Permintaan membuat proyek otomatis lewat pipeline (butuh `PIPELINE_PATH`). Isi `0`, `false`, atau `no` untuk mematikan |
| `BOT_NAME`             | tidak          | `Eruu`    | Nama yang tampil di judul embed progres                                                                             |

Semua variabel di `.env` ikut diteruskan ke `pipeline.py`, jadi pengaturan pipeline di bawah juga cukup ditaruh di file yang sama.

Contoh:

```
DISCORD_TOKEN=isi_token_bot
ALLOWED_USER_IDS=123456789012345678
HERMES_BIN=/usr/local/bin/hermes
HERMES_TIMEOUT_MIN=10
HERMES_CONTEXT=Server proyek ada di ssh stb
PIPELINE_PATH=/home/user/hermes-bot/pipeline.py
PIPELINE_PYTHON=python3
PIPELINE_KEYWORD=proyek
PIPELINE_TIMEOUT_MIN=20

# untuk pipeline.py
VIKEY_API_KEY=isi_key_vikey
GEMINI_API_KEYS=key1,key2
```

## Perintah otomatis ke pipeline

Selain kata kunci `proyek`, pesan juga dikirim ke pipeline kalau `PIPELINE_AUTO` aktif dan pesannya:

- diawali `buat`, `buatkan`, `buatin`, `bikin`, `bikinin`, `bangun`, `bangunkan`, atau `rakit` (boleh didahului `tolong`), dan
- memuat salah satu kata: `web`, `website`, `situs`, `bot`, `app`, `aplikasi`, `api`, `script`, `skrip`, `program`, `game`, `landing`, `html`, `node`, `python`, `dashboard`, `portofolio`, `portfolio`, `proyek`.

Pesan lain diteruskan ke Hermes. Kalau cuma menulis `@bot proyek` tanpa ide, bot membalas contoh pemakaian.

## Fitur `proyek` (pipeline)

Kalau pesan diawali kata kunci (default `proyek`) diikuti spasi, bot menjalankan:

```
python3 pipeline.py --name <nama-proyek> -- <ide>
```

Contoh: `@bot proyek buatkan web html statis profil sederhana`

Tahapnya (model default; bisa diubah lewat prompt, lihat bagian berikutnya):

| Tahap | Kerja                                                                                                  | Model default |
| ----- | ------------------------------------------------------------------------------------------------------ | ------------- |
| 1/5   | Menyusun rencana                                                                                       | Gemini        |
| 2/5   | Menambah fitur yang terlewat (maksimal 5)                                                              | DeepSeek      |
| 3/5   | Menulis kode bertahap per kelompok file (html, css, node js, lainnya), sintaks dicek tiap kelompok     | Sonnet        |
| 4/5   | Cek sintaks otomatis lalu periksa kode terhadap checklist, ulang sampai 3 putaran (`--rounds`)         | DeepSeek      |
| 5/5   | Selesai: status, folder, jumlah panggilan model, perkiraan biaya, ringkasan                            | -             |

- Progres tampil sebagai satu embed yang di-edit tiap langkah (✅ selesai, ⏳ berjalan, ❌ gagal, ℹ️ catatan), lengkap dengan waktu berjalan dan tiap file yang dibuat atau diubah. Kalau terlalu panjang, langkah lama yang sudah selesai disembunyikan.
- `pipeline.py` mengirim langkah-langkah itu sebagai baris `@@{json}` kalau env `PIPELINE_EVENTS=1` (bot mengisinya otomatis). Tanpa env itu, output di terminal tetap bersih.
- Nama file muncul saat model menulisnya (output di-stream). Kalau penyedia API tidak mendukung stream, pipeline otomatis pakai panggilan biasa dan file muncul sekaligus. File yang kepotong ditulis ulang satu per satu dengan batas token dua kali lipat.
- Ringkasan akhir diambil dari baris `5/5 Selesai` sampai habis (maksimal 900 karakter ditampilkan). Kalau format output itu diubah, ringkasan tidak akan muncul.
- Hasil disimpan di `projects/<nama-proyek>` di samping `pipeline.py`: kode, ditambah `PROJECT.md` (rencana), `DECISIONS.md` (keputusan), `REVIEW.md` (hasil pemeriksaan), `IDEA.txt`, dan `NAME.txt`. Nama folder dibuat dari ide plus tanggal dan jam. Nama dari bot selalu dipakai, jadi `nama: ...` di prompt tidak mengubah nama folder.
- Setelah selesai, file hasil dikirim sebagai lampiran: maksimal 10 file, masing-masing maksimal 500 KB, file kode dulu baru dokumen `.md`/`.txt`. File dan folder berawalan titik serta `node_modules` dilewati. Subfolder ditulis dengan pemisah `__` di nama lampiran.
- Kata kuncinya harus ada tepat di awal pesan dan diikuti spasi, tanpa membedakan huruf besar/kecil. "proyeksi ..." tidak dianggap perintah.

### Atur model lewat prompt

Pembagian kerja model bisa ditulis langsung di pesan, tanpa flag. Peran yang dikenali: `planning`, `html`, `css`, `node js` (backend), `preview` (pemeriksaan), dan `fix bug`. Model yang dikenali: `gemini`, `deepseek`, `sonnet` (`claude` dianggap `sonnet`).

```
@bot proyek buat web portofolio, planning deepseek, html deepseek, css sonnet, node js deepseek, preview gemini, fix bug sonnet
```

- `tanpa preview`, `skip fix bug`, dan sejenisnya melewati tahap itu.
- `tanpa sonnet`, `jangan pakai deepseek`, dan sejenisnya melarang model itu; perannya dialihkan ke model lain.
- Peran yang tidak disebut memakai model default di tabel tahap.

### Pengaturan pipeline (`.env`)

| Variabel             | Wajib | Default                                      | Fungsi                                                                                                   |
| -------------------- | ----- | -------------------------------------------- | -------------------------------------------------------------------------------------------------------- |
| `VIKEY_API_KEY`      | ya    | -                                            | Key API Vikey untuk DeepSeek dan Sonnet. Tanpa ini pipeline langsung berhenti                           |
| `GEMINI_API_KEYS`    | tidak | kosong                                       | Key Gemini dipisah koma. Bisa juga lewat `keys.json` berisi `{"gemini": ["key1", "key2"]}`               |
| `VIKEY_BASE_URL`     | tidak | `https://api.vikey.ai/v1`                    | Alamat API Vikey                                                                                         |
| `GEMINI_BASE_URL`    | tidak | `https://generativelanguage.googleapis.com/v1beta/openai` | Alamat API Gemini (kompatibel OpenAI)                                               |
| `GEMINI_MODEL`       | tidak | `gemini-3.5-flash-lite`                      | Nama model Gemini                                                                                        |
| `DEEPSEEK_MODEL`     | tidak | `deepseek/deepseek-v4.1-flash-1017`          | Nama model DeepSeek                                                                                      |
| `SONNET_MODEL`       | tidak | `anthropic/claude-sonnet-5.5`                | Nama model Sonnet                                                                                        |
| `GEMINI_DAILY_LIMIT` | tidak | `480`                                        | Batas pemakaian harian per key Gemini. Key diputar otomatis, jeda 60 detik kalau kena 429                |
| `HTTP_TIMEOUT`       | tidak | `900`                                        | Batas diam per koneksi (detik), bukan batas total proses                                                 |
| `CODE_MAX_TOKENS`    | tidak | `6000`                                       | Batas output per panggilan menulis kode (per kelompok file)                                              |
| `GROUP_MAX_FILES`    | tidak | `5`                                          | Maksimal file per kelompok penulisan                                                                     |
| `GROUP_MODELS`       | tidak | kosong                                       | Model per kelompok file, contoh `html=gemini,node=deepseek,lainnya=sonnet`. Model di prompt lebih utama  |
| `VIKEY_EXTRA_BODY`   | tidak | kosong                                       | JSON tambahan untuk permintaan ke Vikey, mis. `{"reasoning_effort":"low"}`                               |
| `SKILLS`             | tidak | kosong                                       | Skill yang dimuat, dipisah koma. Dipakai kalau pesan tidak memakai `skill:`                              |
| `SKILLS_MAX_CHARS`   | tidak | `40000`                                      | Batas total karakter isi skill yang ditempel ke prompt                                                   |

Kalau tidak ada key Gemini, tahap yang ditugaskan ke Gemini otomatis dialihkan ke DeepSeek. Gemini dihitung gratis; biaya Sonnet dan DeepSeek diperkirakan dalam Rupiah di `usage.jsonl` dan di ringkasan akhir.

Batasan teknis bawaan yang dikirim ke semua model: proyek dijalankan di STB HG680P (Armbian, ARM64, RAM 2 GB) lewat pm2, utamakan Node.js atau Python, rahasia lewat `.env` dengan `.env.example`, serta `package.json` atau `requirements.txt` yang lengkap. Untuk menggantinya, buat `constraints.md` di samping `pipeline.py`.

### Pakai dari terminal

`pipeline.py` juga bisa dijalankan langsung (bot tidak memakai flag ini):

```
export VIKEY_API_KEY="..."
python3 pipeline.py "bot WA yang ..." --name wa-bot
python3 pipeline.py "..." --name x --rounds 3 --approve
python3 pipeline.py --resume nama-proyek
python3 pipeline.py "ide asli" --resume nama-proyek     # kalau IDEA.txt belum ada
python3 pipeline.py "..." --group-model "html=gemini,node=deepseek,lainnya=sonnet"
```

| Flag            | Fungsi                                                                                  |
| --------------- | --------------------------------------------------------------------------------------- |
| `--name`        | Nama folder proyek                                                                      |
| `--rounds`      | Maksimal putaran periksa-perbaiki (default 3)                                           |
| `--approve`     | Minta persetujuan sebelum kode ditulis                                                  |
| `--no-sonnet`   | Jangan pakai Sonnet (larangan di prompt tetap lebih utama)                              |
| `--resume NAMA` | Lanjutkan dari `PROJECT.md` di `projects/NAMA`, tahap rencana dilewati                  |
| `--group-model` | Model per kelompok file                                                                 |
| `--constraints` | Path file batasan teknis                                                                |

## Skill (aturan kualitas)

File `.md` di folder `skills/` (di samping `pipeline.py`) ditempel ke prompt model yang menulis kode, memperbaiki, dan memeriksa. Aturan yang dilanggar dihitung sebagai masalah dan dikirim balik untuk diperbaiki. Di embed muncul langkah `Memuat skill: ...`.

Skill bawaan:

| Skill          | Untuk                    |
| -------------- | ------------------------ |
| `anti-slop`    | kode                     |
| `anti-slop-ui` | tampilan                 |
| `anti-slop-id` | tulisan bahasa Indonesia |

Cara memilih skill, urut dari prioritas tertinggi:

1. `skill:` di pesan Discord (di akhir pesan atau di baris sendiri), contoh: `@bot buatkan web portofolio html + node js skill: anti-slop, seo`. Bagian `skill: ...` tidak ikut dikirim sebagai ide. Di terminal, pakai env `SKILLS=anti-slop,nama-lain`.
2. Kalau tidak ada, nama skill yang kebetulan disebut di teks ide ikut dimuat (tanda strip diabaikan, jadi `antislop` sama dengan `anti-slop`).
3. Kalau tidak ada keduanya, semua skill di folder dimuat.

Catatan lain:

- Tambah skill baru dengan menaruh file `.md` baru di folder `skills/`. Tidak perlu restart, file dibaca ulang tiap proyek.
- Nama skill yang tidak ada ditulis sebagai catatan di embed.
- Total isi skill dibatasi `SKILLS_MAX_CHARS` (default 40000 karakter) supaya biaya token terjaga. Skill yang tidak muat dilewati dan muncul sebagai catatan di embed.
- Bagian front matter (`---` ... `---` dengan `name:` dan `description:`) di awal file dibuang otomatis. Nama skill diambil dari nama file.

## Catatan

- Perintah biasa ke Hermes juga tampil sebagai embed, tapi hanya dengan langkah antri -> Eruu Bot memproses -> selesai, karena `hermes -z` hanya mengembalikan jawaban akhir.
- Hermes dan pipeline berbagi satu antrian, jadi dijalankan satu per satu. Pesan status memberi tahu kalau ada perintah di depan.
- Waktu tunggu di antrian tidak dihitung ke batas waktu. Batas waktu mulai saat proses benar-benar jalan.
- Kalau batas waktu habis, proses dihentikan. Untuk pipeline, hasil sebagian mungkin ada di folder `projects/<nama-proyek>`.
- Kalau `hermes` tidak ditemukan saat jalan di pm2, isi `HERMES_BIN` dengan path lengkap.
- Kalau Python tidak ditemukan, isi `PIPELINE_PYTHON` dengan path lengkapnya.
- Jawaban Hermes yang panjang dipecah jadi beberapa pesan karena batas Discord 2000 karakter. Maksimal 5 pesan, sisanya dipotong dengan catatan.
