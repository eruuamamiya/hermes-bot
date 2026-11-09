# Hermes Discord Bot

Bot Discord untuk menyuruh Hermes lewat slash command. Pasang di VPS yang sama dengan Hermes.

Command:
- `/ping` cek bot hidup
- `/ask prompt:<teks>` kirim perintah ke Hermes dan tampilkan jawabannya

## Pasang

```bash
# Node 20 (sekali saja)
curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash -
sudo apt install -y nodejs
sudo npm install -g pm2

cd hermes-dc-bot
npm install
cp .env.example .env
nano .env                 # isi token, client id, guild id, ID Discord lu
which hermes              # salin path-nya ke HERMES_BIN
node deploy-commands.js   # daftarkan slash command
pm2 start index.js --name hermes-dc
pm2 save
pm2 logs hermes-dc
```

Kalau log menampilkan `Logged in as ...`, bot sudah online.

## Ambil token dan ID

1. discord.com/developers/applications -> New Application
2. Tab Bot -> Reset Token -> salin ke `DISCORD_TOKEN`
3. Tab General Information -> Application ID -> `CLIENT_ID`
4. OAuth2 -> URL Generator -> centang `bot` dan `applications.commands`, buka URL-nya untuk mengundang bot
5. Discord: Settings -> Advanced -> Developer Mode, lalu klik kanan nama lu -> Copy User ID -> `ALLOWED_USER_IDS`
6. Klik kanan nama server -> Copy Server ID -> `GUILD_ID`

## Catatan

- Hermes dijalankan satu per satu (antrian), batas waktu per perintah diatur `HERMES_TIMEOUT_MIN`.
- Kalau `hermes` tidak ditemukan saat jalan di pm2, isi `HERMES_BIN` dengan path lengkap.
- `HERMES_CONTEXT` dikirim ke Hermes di setiap perintah supaya dia tahu server proyek ada di `ssh stb`.
