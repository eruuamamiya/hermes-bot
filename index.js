// Bot Discord yang meneruskan pesan yang nge-tag bot ke Hermes (jalan di mesin yang sama dengan Hermes).
// Kalau pesan diawali kata kunci (default: "proyek"), bot langsung menjalankan pipeline.py
// (Gemini -> DeepSeek -> Sonnet -> cek -> DeepSeek) tanpa lewat Hermes, lalu mengirim file hasilnya.
require('dotenv').config();
const fs = require('node:fs');
const path = require('node:path');
const { Client, GatewayIntentBits, AttachmentBuilder } = require('discord.js');
const { execFile, spawn } = require('node:child_process');

const DISCORD_TOKEN = process.env.DISCORD_TOKEN;
const ALLOWED = (process.env.ALLOWED_USER_IDS || '')
  .split(',')
  .map((s) => s.trim())
  .filter(Boolean);
const HERMES_BIN = process.env.HERMES_BIN || 'hermes';
const TIMEOUT_MIN = Math.min(Math.max(parseInt(process.env.HERMES_TIMEOUT_MIN || '10', 10) || 10, 1), 14);
const TIMEOUT_MS = TIMEOUT_MIN * 60 * 1000;
const CONTEXT = (process.env.HERMES_CONTEXT || '').trim();

// --- Pengaturan pipeline ---
const PIPELINE_PATH = (process.env.PIPELINE_PATH || '').trim(); // path lengkap ke pipeline.py
const PIPELINE_PYTHON = (process.env.PIPELINE_PYTHON || 'python3').trim();
const PIPELINE_KEYWORD = (process.env.PIPELINE_KEYWORD || 'proyek').trim().toLowerCase();
const PIPELINE_TIMEOUT_MIN = Math.min(Math.max(parseInt(process.env.PIPELINE_TIMEOUT_MIN || '20', 10) || 20, 1), 60);
const PIPELINE_TIMEOUT_MS = PIPELINE_TIMEOUT_MIN * 60 * 1000;
const MAX_FILES = 10; // batas lampiran Discord per pesan
const MAX_FILE_BYTES = 500 * 1024; // batas Discord total sekitar 10 MB per pesan

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

// --- Antrian: Hermes dan pipeline dijalankan satu per satu ---
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

// --- Pipeline: nama folder proyek yang aman dari ide ---
function makeProjectName(idea) {
  const base =
    idea
      .toLowerCase()
      .normalize('NFKD')
      .replace(/[^a-z0-9]+/g, '-')
      .replace(/^-+|-+$/g, '')
      .slice(0, 30)
      .replace(/-+$/, '') || 'proyek';
  const d = new Date();
  const pad = (n) => String(n).padStart(2, '0');
  return `${base}-${pad(d.getMonth() + 1)}${pad(d.getDate())}-${pad(d.getHours())}${pad(d.getMinutes())}${pad(d.getSeconds())}`;
}

// Kembalikan teks ide kalau pesan diawali kata kunci, atau null kalau bukan perintah pipeline.
function parsePipelineIdea(prompt) {
  const kw = PIPELINE_KEYWORD;
  if (!kw) return null;
  if (prompt.slice(0, kw.length).toLowerCase() !== kw) return null;
  if (prompt.length === kw.length) return '';
  if (!/\s/.test(prompt[kw.length])) return null; // misal "proyeksi ..." bukan perintah
  return prompt.slice(kw.length).trim();
}

// --- Jalankan pipeline.py tanpa shell, dengan laporan tahap lewat onStage ---
function runPipeline(idea, name, onStage) {
  return new Promise((resolve) => {
    let out = '';
    let err = '';
    let buf = '';
    let killed = false;
    let done = false;
    let child;

    const finish = (result) => {
      if (done) return;
      done = true;
      clearTimeout(timer);
      resolve({ out, err, killed, ...result });
    };

    const timer = setTimeout(() => {
      killed = true;
      if (child) child.kill('SIGTERM');
    }, PIPELINE_TIMEOUT_MS);

    // '--' supaya ide yang diawali '-' tidak dibaca sebagai opsi.
    const args = [PIPELINE_PATH, '--name', name, '--', idea];
    child = spawn(PIPELINE_PYTHON, args, {
      cwd: path.dirname(PIPELINE_PATH),
      env: { ...process.env, PYTHONUNBUFFERED: '1' },
    });

    child.stdout.on('data', (d) => {
      const s = d.toString();
      out = (out + s).slice(-20000);
      buf += s;
      let i;
      while ((i = buf.indexOf('\n')) >= 0) {
        const line = buf.slice(0, i).trim();
        buf = buf.slice(i + 1);
        if (/^\d\/5\s/.test(line)) onStage(line);
      }
    });
    child.stderr.on('data', (d) => {
      err = (err + d.toString()).slice(-20000);
    });
    child.on('error', (e) => finish({ code: null, spawnError: e }));
    child.on('close', (code) => finish({ code, spawnError: null }));
  });
}

// Kumpulkan file hasil proyek (kode dulu, dokumen .md/.txt terakhir)
function collectFiles(dir) {
  const found = [];
  const walk = (d, depth) => {
    if (depth > 4) return;
    let entries;
    try {
      entries = fs.readdirSync(d, { withFileTypes: true });
    } catch {
      return;
    }
    for (const e of entries) {
      if (e.name.startsWith('.') || e.name === 'node_modules') continue;
      const full = path.join(d, e.name);
      if (e.isDirectory()) {
        walk(full, depth + 1);
      } else if (e.isFile()) {
        try {
          if (fs.statSync(full).size <= MAX_FILE_BYTES) found.push(full);
        } catch {
          /* abaikan */
        }
      }
    }
  };
  walk(dir, 0);
  const isDoc = (p) => /\.(md|txt)$/i.test(p);
  found.sort((a, b) => Number(isDoc(a)) - Number(isDoc(b)) || a.localeCompare(b));
  return found;
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

// --- Tangani perintah "proyek ...": jalankan pipeline.py langsung ---
async function handlePipeline(message, idea, allowedMentions) {
  if (!PIPELINE_PATH) {
    return message.reply({ content: 'PIPELINE_PATH belum diisi di .env (path lengkap ke pipeline.py).', allowedMentions });
  }
  if (!fs.existsSync(PIPELINE_PATH)) {
    return message.reply({ content: `File pipeline tidak ditemukan: ${PIPELINE_PATH}`, allowedMentions });
  }
  if (!idea) {
    return message.reply({
      content: `Tulis idenya, contoh: @bot ${PIPELINE_KEYWORD} buatkan web html statis profil sederhana`,
      allowedMentions,
    });
  }

  let status = null;
  try {
    const name = makeProjectName(idea);
    const outdir = path.join(path.dirname(PIPELINE_PATH), 'projects', name);

    status = await message.reply({
      content: pending > 0 ? `Menunggu giliran (${pending} perintah di depan)...` : 'Pipeline dimulai...',
      allowedMentions,
    });
    message.channel.sendTyping().catch(() => {});

    // Edit pesan status tiap tahap, diurutkan supaya tidak saling menimpa
    let editChain = Promise.resolve();
    const onStage = (line) => {
      editChain = editChain.then(() =>
        status.edit({ content: `Pipeline berjalan (${name}):\n${line}`, allowedMentions }).catch(() => {})
      );
    };

    const started = Date.now();
    const result = await enqueue(() => {
      onStage('Memulai...');
      return runPipeline(idea, name, onStage);
    });
    const secs = Math.round((Date.now() - started) / 1000);
    await editChain;

    if (result.spawnError && result.spawnError.code === 'ENOENT') {
      return status.edit({
        content: `Python "${PIPELINE_PYTHON}" tidak ditemukan. Isi PIPELINE_PYTHON di .env dengan path lengkapnya.`,
        allowedMentions,
      });
    }
    if (result.killed) {
      return status.edit({
        content: `Pipeline melewati batas waktu ${PIPELINE_TIMEOUT_MIN} menit dan dihentikan. Hasil sebagian mungkin ada di ${outdir}`,
        allowedMentions,
      });
    }
    if (result.code !== 0) {
      const tail = (result.err || result.out || String(result.spawnError || 'tidak ada output')).trim().slice(-1500);
      return status.edit({ content: `Pipeline gagal:\n\`\`\`\n${tail}\n\`\`\``, allowedMentions });
    }

    console.log(`[proyek] user=${message.author.id} ${secs}s name=${name} idea="${idea.slice(0, 80)}"`);

    const idx = result.out.lastIndexOf('5/5 Selesai');
    const summary = (idx >= 0 ? result.out.slice(idx) : result.out.trim().slice(-1200)).trim();
    await sendLong(status, `Pipeline selesai (${secs} detik)\n${summary}`);

    const files = collectFiles(outdir);
    if (files.length > 0) {
      const shown = files.slice(0, MAX_FILES);
      const note = files.length > shown.length ? ` (${shown.length} dari ${files.length} file)` : '';
      await status.channel
        .send({
          content: `File hasil${note}:`,
          files: shown.map(
            (p) => new AttachmentBuilder(p, { name: path.relative(outdir, p).replace(/[\\/]/g, '__') })
          ),
          allowedMentions: { parse: [] },
        })
        .catch((e) => console.error('Gagal kirim lampiran:', e));
    }
  } catch (err) {
    console.error('Error saat menjalankan pipeline:', err);
    const msg = 'Terjadi error di bot saat menjalankan pipeline. Cek log pm2.';
    if (status) {
      await status.edit({ content: msg, allowedMentions }).catch(() => {});
    } else {
      await message.reply({ content: msg, allowedMentions }).catch(() => {});
    }
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
    return message.reply({ content: 'Lau Siape Mpruy.', allowedMentions });
  }

  // Buang tag bot dari teks, sisanya jadi prompt
  const prompt = message.content.replace(new RegExp(`<@!?${client.user.id}>`, 'g'), '').trim();
  if (!prompt) {
    return message.reply({
      content: `Tag aku lalu tulis perintahnya, contoh: @bot cek status server, atau @bot ${PIPELINE_KEYWORD} buatkan web html statis`,
      allowedMentions,
    });
  }

  // Awalan kata kunci -> langsung ke pipeline.py (tanpa Hermes)
  const idea = parsePipelineIdea(prompt);
  if (idea !== null) {
    return handlePipeline(message, idea, allowedMentions);
  }

  let status = null;
  try {
    if (prompt.toLowerCase() === 'ping') {
      return message.reply({ content: `Pong! Latensi ${Math.round(client.ws.ping)} ms`, allowedMentions });
    }

    status = await message.reply({
      content: pending > 0 ? `Menunggu giliran (${pending} perintah di depan)...` : 'EruuBot sedang bekerja...',
      allowedMentions,
    });
    message.channel.sendTyping().catch(() => {});

    const started = Date.now();
    const { error, stdout, stderr } = await enqueue(() => runHermes(prompt));
    const secs = Math.round((Date.now() - started) / 1000);
    const out = stdout.trim();

    if (error && error.killed) {
      return status.edit(`EruuBot melewati batas waktu ${TIMEOUT_MIN} menit dan dihentikan.`);
    }
    if (error && error.code === 'ENOENT') {
      return status.edit(
        `Perintah "${HERMES_BIN}" tidak ditemukan. Isi HERMES_BIN di .env dengan path lengkap (hasil \`which hermes\`).`
      );
    }
    if (error && !out) {
      const tail = (stderr || String(error.message)).trim().slice(-1500);
      return status.edit(`EruuBot gagal:\n\`\`\`\n${tail}\n\`\`\``);
    }

    console.log(`[ask] user=${message.author.id} ${secs}s prompt="${prompt.slice(0, 80)}"`);
    return sendLong(status, out || '(EruuBot tidak mengembalikan teks)');
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
