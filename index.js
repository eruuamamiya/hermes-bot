// Bot Discord yang meneruskan pesan yang nge-tag bot ke Hermes (jalan di mesin yang sama dengan Hermes).
require('dotenv').config();
const { Client, GatewayIntentBits } = require('discord.js');
const { execFile } = require('node:child_process');

const DISCORD_TOKEN = process.env.DISCORD_TOKEN;
const ALLOWED = (process.env.ALLOWED_USER_IDS || '')
  .split(',')
  .map((s) => s.trim())
  .filter(Boolean);
const HERMES_BIN = process.env.HERMES_BIN || 'hermes';
const TIMEOUT_MIN = Math.min(Math.max(parseInt(process.env.HERMES_TIMEOUT_MIN || '10', 10) || 10, 1), 14);
const TIMEOUT_MS = TIMEOUT_MIN * 60 * 1000;
const CONTEXT = (process.env.HERMES_CONTEXT || '').trim();

const CHUNK = 1900; // batas pesan Discord 2000 karakter
const MAX_CHUNKS = 5;

if (!DISCORD_TOKEN) {
  console.error('DISCORD_TOKEN belum diisi di .env');
  process.exit(1);
}
if (ALLOWED.length === 0) {
  console.error('ALLOWED_USER_IDS kosong. Isi dengan ID Discord lu di .env');
  process.exit(1);
}

// --- Antrian: Hermes dijalankan satu per satu ---
let chain = Promise.resolve();
let pending = 0;
function enqueue(task) {
  pending += 1;
  const run = chain.then(task, task).finally(() => {
    pending -= 1;
  });
  chain = run.catch(() => {});
  return run;
}

// --- Jalankan Hermes tanpa shell (aman dari injeksi perintah) ---
function runHermes(prompt) {
  // Diawali kata biasa supaya tidak pernah dibaca sebagai flag.
  const full = CONTEXT ? `${CONTEXT}\n\nPermintaan:\n${prompt}` : `Permintaan:\n${prompt}`;
  return new Promise((resolve) => {
    execFile(
      HERMES_BIN,
      ['-z', full],
      { timeout: TIMEOUT_MS, maxBuffer: 10 * 1024 * 1024, env: process.env },
      (error, stdout, stderr) => {
        resolve({ error, stdout: stdout || '', stderr: stderr || '' });
      }
    );
  });
}

function splitText(text) {
  const parts = [];
  let rest = text;
  while (rest.length > CHUNK) {
    let cut = rest.lastIndexOf('\n', CHUNK);
    if (cut < CHUNK * 0.5) cut = CHUNK;
    parts.push(rest.slice(0, cut));
    rest = rest.slice(cut).replace(/^\n/, '');
  }
  if (rest.length) parts.push(rest);
  return parts;
}

// status = pesan balasan bot (placeholder) yang diedit jadi jawaban pertama
async function sendLong(status, text) {
  let parts = splitText(text);
  let truncated = false;
  if (parts.length > MAX_CHUNKS) {
    parts = parts.slice(0, MAX_CHUNKS);
    truncated = true;
  }
  if (truncated) parts[parts.length - 1] += '\n\n(jawaban dipotong, terlalu panjang)';
  const opts = { allowedMentions: { parse: [] } };
  await status.edit({ content: parts[0], ...opts });
  for (const p of parts.slice(1)) {
    await status.channel.send({ content: p, ...opts });
  }
}

// Intent MessageContent tidak perlu: Discord tetap mengirim isi pesan yang nge-tag bot.
const client = new Client({
  intents: [GatewayIntentBits.Guilds, GatewayIntentBits.GuildMessages],
});

// 'clientReady' di discord.js terbaru, 'ready' di versi lama. Pakai keduanya, log sekali.
let announced = false;
function onReady() {
  if (announced) return;
  announced = true;
  console.log(`Logged in as ${client.user.tag}`);
}
client.once('clientReady', onReady);
client.once('ready', onReady);

client.on('messageCreate', async (message) => {
  if (message.author.bot) return;
  // Hanya merespons kalau bot di-tag langsung (bukan @everyone / @role)
  if (!message.mentions.has(client.user, { ignoreEveryone: true, ignoreRoles: true })) return;

  const allowedMentions = { parse: [], repliedUser: false };

  if (!ALLOWED.includes(message.author.id)) {
    return message.reply({ content: 'Kamu tidak punya akses ke bot ini.', allowedMentions });
  }

  // Buang tag bot dari teks, sisanya jadi prompt
  const prompt = message.content.replace(new RegExp(`<@!?${client.user.id}>`, 'g'), '').trim();
  if (!prompt) {
    return message.reply({ content: 'Tag aku lalu tulis perintahnya, contoh: @bot cek status server', allowedMentions });
  }

  let status = null;
  try {
    if (prompt.toLowerCase() === 'ping') {
      return message.reply({ content: `Pong! Latensi ${Math.round(client.ws.ping)} ms`, allowedMentions });
    }

    status = await message.reply({
      content: pending > 0 ? `Menunggu giliran (${pending} perintah di depan)...` : 'Hermes sedang bekerja...',
      allowedMentions,
    });
    message.channel.sendTyping().catch(() => {});

    const started = Date.now();
    const { error, stdout, stderr } = await enqueue(() => runHermes(prompt));
    const secs = Math.round((Date.now() - started) / 1000);
    const out = stdout.trim();

    if (error && error.killed) {
      return status.edit(`Hermes melewati batas waktu ${TIMEOUT_MIN} menit dan dihentikan.`);
    }
    if (error && error.code === 'ENOENT') {
      return status.edit(
        `Perintah "${HERMES_BIN}" tidak ditemukan. Isi HERMES_BIN di .env dengan path lengkap (hasil \`which hermes\`).`
      );
    }
    if (error && !out) {
      const tail = (stderr || String(error.message)).trim().slice(-1500);
      return status.edit(`Hermes gagal:\n\`\`\`\n${tail}\n\`\`\``);
    }

    console.log(`[ask] user=${message.author.id} ${secs}s prompt="${prompt.slice(0, 80)}"`);
    return sendLong(status, out || '(Hermes tidak mengembalikan teks)');
  } catch (err) {
    console.error('Error saat memproses pesan:', err);
    const msg = 'Terjadi error di bot. Cek log pm2.';
    if (status) {
      await status.edit(msg).catch(() => {});
    } else {
      await message.reply({ content: msg, allowedMentions }).catch(() => {});
    }
  }
});

process.on('unhandledRejection', (err) => console.error('unhandledRejection:', err));

client.login(DISCORD_TOKEN);
