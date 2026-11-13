// Bot Discord yang meneruskan pesan yang nge-tag bot ke Hermes (jalan di mesin yang sama dengan Hermes).
// Kalau pesan diawali kata kunci (default: "proyek"), bot langsung menjalankan pipeline.py
// (Gemini -> DeepSeek -> Sonnet -> cek -> DeepSeek) tanpa lewat Hermes, lalu mengirim file hasilnya.
// Model per peran (planning, html, css, node js, preview, fix bug) dan nama proyek diatur lewat isi prompt.
require('dotenv').config();
const fs = require('node:fs');
const path = require('node:path');
const { Client, GatewayIntentBits, AttachmentBuilder, EmbedBuilder } = require('discord.js');
const { execFile, spawn } = require('node:child_process');
const { pipeline } = require('node:stream/promises');
const { Readable } = require('node:stream');

const DISCORD_TOKEN = process.env.DISCORD_TOKEN;
const ALLOWED = (process.env.ALLOWED_USER_IDS || '')
  .split(',')
  .map((s) => s.trim())
  .filter(Boolean);
const HERMES_BIN = process.env.HERMES_BIN || 'hermes';
// 0 = tanpa batas waktu (default)
const TIMEOUT_MIN = Math.max(parseInt(process.env.HERMES_TIMEOUT_MIN || '0', 10) || 0, 0);
const TIMEOUT_MS = TIMEOUT_MIN * 60 * 1000;
const CONTEXT = (process.env.HERMES_CONTEXT || '').trim();

// --- Pengaturan pipeline ---
const PIPELINE_PATH = (process.env.PIPELINE_PATH || '').trim(); // path lengkap ke pipeline.py
const PIPELINE_PYTHON = (process.env.PIPELINE_PYTHON || 'python3').trim();
const PIPELINE_KEYWORD = (process.env.PIPELINE_KEYWORD || 'proyek').trim().toLowerCase();
const PIPELINE_TIMEOUT_MIN = Math.max(parseInt(process.env.PIPELINE_TIMEOUT_MIN || '0', 10) || 0, 0);
const PIPELINE_TIMEOUT_MS = PIPELINE_TIMEOUT_MIN * 60 * 1000;
const PIPELINE_AUTO = !['0', 'false', 'no'].includes((process.env.PIPELINE_AUTO || '1').trim().toLowerCase());
const BOT_NAME = (process.env.BOT_NAME || 'Eruu').trim();
const MAX_FILES = 10; // batas lampiran Discord per pesan
const MAX_FILE_BYTES = 500 * 1024; // batas Discord total sekitar 10 MB per pesan

// --- Pengaturan simpan file lampiran ---
// Folder induk yang boleh jadi tujuan. Default: folder 'projects' di sebelah pipeline.py (atau ./projects).
const SAVE_ROOT = path.resolve(
  (process.env.SAVE_ROOT || '').trim() || path.join(PIPELINE_PATH ? path.dirname(PIPELINE_PATH) : process.cwd(), 'projects')
);

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

// --- Embed progres: satu pesan yang di-edit tiap langkah berubah ---
const ICON = { run: '⏳', done: '✅', fail: '❌', note: 'ℹ️' };
const COLOR = { run: 0x5865f2, done: 0x57f287, fail: 0xed4245 };
const MIN_EDIT_MS = 1200; // jaga supaya tidak kena rate limit edit Discord
const HEARTBEAT_MS = 10000; // perbarui waktu berjalan walau tidak ada langkah baru

function fmtElapsed(ms) {
  const s = Math.floor(ms / 1000);
  return s < 60 ? `${s} dtk` : `${Math.floor(s / 60)} mnt ${s % 60} dtk`;
}

class Progress {
  constructor(message, label, allowedMentions) {
    this.message = message;
    this.label = label;
    this.allowedMentions = allowedMentions;
    this.steps = []; // { id, text, who, state }
    this.state = 'run';
    this.extra = '';
    this.msg = null;
    this.started = Date.now();
    this.timer = null;
    this.lastEdit = 0;
    this.chain = Promise.resolve();
    this.beat = null;
  }

  async init() {
    this.msg = await this.message.reply({ embeds: [this.embed()], allowedMentions: this.allowedMentions });
    this.beat = setInterval(() => this.touch(), HEARTBEAT_MS);
    return this.msg;
  }

  find(id) {
    return this.steps.find((x) => x.id === id);
  }

  start(id, text, who) {
    const cur = this.find(id);
    if (cur) Object.assign(cur, { text, who, state: 'run' });
    else this.steps.push({ id, text, who, state: 'run' });
    this.touch();
  }

  done(id, text) {
    const cur = this.find(id);
    if (!cur) return;
    cur.state = 'done';
    if (text) cur.text = text;
    this.touch();
  }

  fail(id, text) {
    const cur = this.find(id);
    if (cur) {
      cur.state = 'fail';
      if (text) cur.text = text;
    } else {
      this.steps.push({ id, text: text || 'Gagal', state: 'fail' });
    }
    this.touch();
  }

  note(text) {
    this.steps.push({ id: `n${this.steps.length}`, text, state: 'note' });
    this.touch();
  }

  apply(e) {
    if (e.e === 'start') this.start(e.id, e.text, e.who);
    else if (e.e === 'done') this.done(e.id, e.text);
    else if (e.e === 'fail') this.fail(e.id, e.text);
    else if (e.e === 'note') this.note(e.text);
  }

  embed() {
    const head = { run: 'sedang bekerja', done: 'selesai', fail: 'gagal' }[this.state];
    const title = `${BOT_NAME} ${head}${this.label ? ` (${this.label})` : ''}:`.slice(0, 250);
    const fmt = (x) => `${ICON[x.state]} ${x.text}${x.who ? ` (${x.who})` : ''}`;
    // Kalau kepanjangan, langkah lama yang sudah selesai disembunyikan supaya muat di embed (maks 4096 karakter)
    const shown = this.steps.slice();
    let hidden = 0;
    const room = 3000;
    while (shown.length > 1 && shown.map(fmt).join('\n').length > room) {
      const i = shown.findIndex((x) => x.state === 'done' || x.state === 'note');
      if (i < 0 || i === shown.length - 1) break;
      shown.splice(i, 1);
      hidden += 1;
    }
    let desc = shown.map(fmt).join('\n');
    if (hidden) desc = `… ${hidden} langkah selesai disembunyikan\n${desc}`;
    if (this.extra) desc += `\n\n${this.extra}`;
    return new EmbedBuilder()
      .setTitle(title)
      .setColor(COLOR[this.state])
      .setDescription((desc || '⏳ Memulai...').slice(0, 4000))
      .setFooter({ text: `⏱ ${fmtElapsed(Date.now() - this.started)}` });
  }

  touch() {
    if (!this.msg || this.timer || this.state !== 'run') return;
    const wait = Math.max(0, MIN_EDIT_MS - (Date.now() - this.lastEdit));
    this.timer = setTimeout(() => {
      this.timer = null;
      this.flush();
    }, wait);
  }

  flush() {
    if (!this.msg) return this.chain;
    this.lastEdit = Date.now();
    this.chain = this.chain.then(() =>
      this.msg.edit({ embeds: [this.embed()], allowedMentions: this.allowedMentions }).catch(() => {})
    );
    return this.chain;
  }

  // Akhiri: semua langkah yang masih jalan ditutup sesuai hasil akhir, lalu edit terakhir
  async finish(state, extra) {
    clearInterval(this.beat);
    clearTimeout(this.timer);
    this.timer = null;
    this.state = state;
    for (const x of this.steps) if (x.state === 'run') x.state = state === 'done' ? 'done' : 'fail';
    if (extra) this.extra = extra;
    await this.flush();
  }
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

// Nama proyek dari prompt: "nama: porto-huma", "nama proyek porto-huma", "namai porto-huma".
// Harus sama dengan parse_name() di pipeline.py (huruf kecil, angka, strip).
function extractProjectName(text) {
  const m = text.match(
    /\b(?:nama(?:\s*(?:proyek|project|folder))?(?:nya)?\s*[:=]\s*|nama\s*(?:proyek|project|folder)(?:nya)?\s+|namai\s+|dinamai\s+|name\s*[:=]\s*)["'`]?([A-Za-z0-9][A-Za-z0-9_-]{1,40})/i
  );
  if (!m) return null;
  return m[1].toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '') || null;
}

// Kalau folder dengan nama itu sudah ada, tambahkan tanggal-jam supaya proyek lama tidak tertimpa.
function uniqueName(base) {
  const root = path.join(path.dirname(PIPELINE_PATH), 'projects');
  if (!fs.existsSync(path.join(root, base))) return base;
  const d = new Date();
  const pad = (n) => String(n).padStart(2, '0');
  return `${base}-${pad(d.getMonth() + 1)}${pad(d.getDate())}-${pad(d.getHours())}${pad(d.getMinutes())}`;
}

// Ambil pilihan skill dari pesan: "... skill: anti-slop, seo". Ditulis di akhir pesan atau di baris sendiri.
function extractSkills(text) {
  const m = text.match(/(^|\s)skill\s*:\s*([^\n]*)/i);
  if (!m) return { idea: text, skills: [] };
  const skills = [
    ...new Set(
      m[2]
        .split(/[,\s]+/)
        .map((x) => x.trim().toLowerCase())
        .filter((x) => /^[a-z0-9_-]+$/.test(x))
    ),
  ];
  const idea = (text.slice(0, m.index) + ' ' + text.slice(m.index + m[0].length)).replace(/\s+/g, ' ').trim();
  return { idea, skills };
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
function runPipeline(idea, name, onStage, onEvent, skills = []) {
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
      if (timer) clearTimeout(timer);
      resolve({ out, err, killed, ...result });
    };

    const timer = PIPELINE_TIMEOUT_MS
      ? setTimeout(() => {
          killed = true;
          if (child) child.kill('SIGTERM');
        }, PIPELINE_TIMEOUT_MS)
      : null;

    // '--' supaya ide yang diawali '-' tidak dibaca sebagai opsi.
    const args = [PIPELINE_PATH, '--name', name, '--', idea];
    child = spawn(PIPELINE_PYTHON, args, {
      cwd: path.dirname(PIPELINE_PATH),
      env: {
        ...process.env,
        PYTHONUNBUFFERED: '1',
        PIPELINE_EVENTS: '1',
        ...(skills.length ? { SKILLS: skills.join(',') } : {}),
      },
    });

    child.stdout.on('data', (d) => {
      buf += d.toString();
      let i;
      while ((i = buf.indexOf('\n')) >= 0) {
        const raw = buf.slice(0, i);
        const line = raw.trim();
        buf = buf.slice(i + 1);
        if (line.startsWith('@@')) {
          // baris event progres dari pipeline.py: tidak dimasukkan ke ringkasan akhir
          try {
            onEvent(JSON.parse(line.slice(2)));
          } catch {
            /* abaikan baris rusak */
          }
          continue;
        }
        out = (out + raw + '\n').slice(-20000);
        if (/^\d\/5\s/.test(line)) onStage(line);
      }
    });
    child.stderr.on('data', (d) => {
      err = (err + d.toString()).slice(-20000);
    });
    child.on('error', (e) => finish({ code: null, spawnError: e }));
    child.on('close', (code) => {
      if (buf.trim()) out = (out + buf).slice(-20000);
      finish({ code, spawnError: null });
    });
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

// Kirim jawaban panjang sebagai pesan baru (balasan pertama ke pesan user, sisanya di channel)
async function sendAnswer(message, text, allowedMentions) {
  let parts = splitText(text);
  if (parts.length > MAX_CHUNKS) {
    parts = parts.slice(0, MAX_CHUNKS);
    parts[parts.length - 1] += '\n\n(jawaban dipotong, terlalu panjang)';
  }
  await message.reply({ content: parts[0], allowedMentions });
  for (const p of parts.slice(1)) {
    await message.channel.send({ content: p, allowedMentions: { parse: [] } });
  }
}

// Permintaan membuat proyek ("buatkan web ...", "bikin bot ...") otomatis lewat pipeline supaya progresnya kelihatan
function looksLikeBuild(prompt) {
  if (!PIPELINE_AUTO || !PIPELINE_PATH) return false;
  const p = prompt.toLowerCase();
  return (
    /^(tolong\s+)?(buat(kan|in)?|bikin(in)?|bangun(kan)?|rakit)\s/.test(p) &&
    /\b(web|website|situs|bot|app|aplikasi|api|script|skrip|program|game|landing|html|node|python|dashboard|portofolio|portfolio|proyek)\b/.test(p)
  );
}


// --- Simpan lampiran Discord ke folder tujuan ---
// Tujuan diambil dari teks: path absolut (/home/.../projects/test) atau relatif ke SAVE_ROOT ("folder test").
function extractDestination(text) {
  const abs = text.match(/(?:^|[\s"'`(])((?:\/[A-Za-z0-9._@+-]+)+\/?)/);
  if (abs) return abs[1];
  const rel = text.match(/\b(?:folder|direktori|dir|ke)\s+["'`]?([A-Za-z0-9._@+-]+(?:\/[A-Za-z0-9._@+-]+)*)/i);
  if (rel && !/^(folder|di|dalam|ke)$/i.test(rel[1])) return rel[1];
  return null;
}

// Path absolut dipakai apa adanya; path relatif dihitung dari SAVE_ROOT. Tanpa pembatasan.
function resolveDestination(dest) {
  return path.resolve(dest.startsWith('/') ? dest : path.join(SAVE_ROOT, dest));
}

// Unduh apa adanya (streaming langsung ke disk, tanpa batas ukuran, tanpa pemeriksaan isi).
async function downloadTo(url, file) {
  const res = await fetch(url);
  if (!res.ok || !res.body) throw new Error(`HTTP ${res.status}`);
  await pipeline(Readable.fromWeb(res.body), fs.createWriteStream(file));
  return (await fs.promises.stat(file)).size;
}

function fmtSize(b) {
  return b < 1024 ? `${b} B` : b < 1048576 ? `${(b / 1024).toFixed(1)} KB` : `${(b / 1048576).toFixed(1)} MB`;
}

async function handleUpload(message, prompt, allowedMentions) {
  const dest = extractDestination(prompt);
  if (!dest) {
    return message.reply({
      content: `Mau disimpan ke folder mana? Contoh: @bot masukan ke folder ${SAVE_ROOT}/test`,
      allowedMentions,
    });
  }
  const dir = resolveDestination(dest);
  try {
    await fs.promises.mkdir(dir, { recursive: true });
    message.channel.sendTyping().catch(() => {});
    const ok = [];
    const bad = [];
    for (const att of message.attachments.values()) {
      const name = att.name || 'file';
      try {
        const size = await downloadTo(att.url, path.join(dir, name));
        ok.push(`✅ ${name} (${fmtSize(size)})`);
      } catch (e) {
        bad.push(`❌ ${name}: ${e.message}`);
      }
    }
    console.log(`[upload] user=${message.author.id} dir=${dir} ok=${ok.length} gagal=${bad.length}`);
    const lines = [`Tersimpan di \`${dir}\`:`, ...ok, ...bad];
    return sendAnswer(message, lines.join('\n'), allowedMentions);
  } catch (err) {
    console.error('Error saat menyimpan lampiran:', err);
    return message.reply({ content: `Gagal menyimpan file: ${err.message}`, allowedMentions });
  }
}

// --- Tangani perintah "proyek ...": jalankan pipeline.py langsung, progres lewat embed ---
async function handlePipeline(message, rawIdea, allowedMentions) {
  if (!PIPELINE_PATH) {
    return message.reply({ content: 'PIPELINE_PATH belum diisi di .env (path lengkap ke pipeline.py).', allowedMentions });
  }
  if (!fs.existsSync(PIPELINE_PATH)) {
    return message.reply({ content: `File pipeline tidak ditemukan: ${PIPELINE_PATH}`, allowedMentions });
  }
  const { idea, skills } = extractSkills(rawIdea);
  if (!idea) {
    return message.reply({
      content: `Tulis idenya, contoh: @bot ${PIPELINE_KEYWORD} buatkan web html statis profil sederhana, html deepseek, css sonnet, preview gemini, nama: profil-saya`,
      allowedMentions,
    });
  }

  let progress = null;
  try {
    // Nama dari prompt ("nama: porto-huma") kalau ada; kalau tidak, otomatis dari ide + jam
    const custom = extractProjectName(idea);
    const name = custom ? uniqueName(custom) : makeProjectName(idea);
    const outdir = path.join(path.dirname(PIPELINE_PATH), 'projects', name);

    progress = new Progress(message, name, allowedMentions);
    if (pending > 0) progress.start('queue', `Menunggu giliran (${pending} perintah di depan)`);
    await progress.init();
    message.channel.sendTyping().catch(() => {});

    // Pipeline versi lama tidak mengirim event: kalau begitu, tampilkan baris "N/5 ..." sebagai langkah
    let sawEvents = false;
    let lastStage = null;
    const onEvent = (e) => {
      sawEvents = true;
      progress.apply(e);
    };
    const onStage = (line) => {
      if (sawEvents) return;
      if (lastStage) progress.done(lastStage);
      lastStage = `stg-${line.slice(0, 3)}`;
      progress.start(lastStage, line.replace(/^\d\/5\s+/, '').replace(/\.+$/, ''));
    };

    const started = Date.now();
    const result = await enqueue(() => {
      progress.done('queue');
      return runPipeline(idea, name, onStage, onEvent, skills);
    });
    const secs = Math.round((Date.now() - started) / 1000);

    if (result.spawnError && result.spawnError.code === 'ENOENT') {
      return progress.finish('fail', `Python "${PIPELINE_PYTHON}" tidak ditemukan. Isi PIPELINE_PYTHON di .env dengan path lengkapnya.`);
    }
    if (result.killed) {
      return progress.finish(
        'fail',
        `Melewati batas waktu ${PIPELINE_TIMEOUT_MIN} menit dan dihentikan. Hasil sebagian mungkin ada di ${outdir}`
      );
    }
    if (result.code !== 0) {
      const tail = (result.err || result.out || String(result.spawnError || 'tidak ada output')).trim().slice(-900);
      return progress.finish('fail', `\`\`\`\n${tail}\n\`\`\``);
    }

    console.log(`[proyek] user=${message.author.id} ${secs}s name=${name} idea="${idea.slice(0, 80)}"`);

    const idx = result.out.lastIndexOf('5/5 Selesai');
    const summary = (idx >= 0 ? result.out.slice(idx) : result.out.trim().slice(-1200)).trim();
    await progress.finish('done', `\`\`\`\n${summary.slice(0, 900)}\n\`\`\``);

    const files = collectFiles(outdir);
    if (files.length > 0) {
      const shown = files.slice(0, MAX_FILES);
      const note = files.length > shown.length ? ` (${shown.length} dari ${files.length} file)` : '';
      await message.channel
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
    if (progress && progress.msg) {
      await progress.finish('fail', msg).catch(() => {});
    } else {
      await message.reply({ content: msg, allowedMentions }).catch(() => {});
    }
  }
}

// --- Tangani perintah biasa: Hermes dijalankan sekali jalan (-z), jadi langkahnya hanya antri -> proses -> selesai ---
async function handleHermes(message, prompt, allowedMentions) {
  let progress = null;
  try {
    progress = new Progress(message, null, allowedMentions);
    if (pending > 0) progress.start('queue', `Menunggu giliran (${pending} perintah di depan)`);
    await progress.init();
    message.channel.sendTyping().catch(() => {});

    const started = Date.now();
    const { error, stdout, stderr } = await enqueue(() => {
      progress.done('queue');
      progress.start('hermes', 'Eruu Bot memproses perintah', 'Eruu Bot');
      return runHermes(prompt);
    });
    const secs = Math.round((Date.now() - started) / 1000);
    const out = stdout.trim();

    if (error && error.killed) {
      return progress.finish('fail', `Melewati batas waktu ${TIMEOUT_MIN} menit dan dihentikan.`);
    }
    if (error && error.code === 'ENOENT') {
      return progress.finish(
        'fail',
        `Perintah "${HERMES_BIN}" tidak ditemukan. Isi HERMES_BIN di .env dengan path lengkap (hasil \`which hermes\`).`
      );
    }
    if (error && !out) {
      const tail = (stderr || String(error.message)).trim().slice(-900);
      return progress.finish('fail', `\`\`\`\n${tail}\n\`\`\``);
    }

    console.log(`[ask] user=${message.author.id} ${secs}s prompt="${prompt.slice(0, 80)}"`);
    await progress.finish('done');
    return sendAnswer(message, out || `(${BOT_NAME} tidak mengembalikan teks)`, allowedMentions);
  } catch (err) {
    console.error('Error saat memproses pesan:', err);
    const msg = 'Terjadi error di bot. Cek log pm2.';
    if (progress && progress.msg) {
      await progress.finish('fail', msg).catch(() => {});
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
    return message.reply({ content: 'Lau siape mpruy', allowedMentions });
  }

  // Buang tag bot dari teks, sisanya jadi prompt
  const prompt = message.content.replace(new RegExp(`<@!?${client.user.id}>`, 'g'), '').trim();
  if (!prompt) {
    return message.reply({
      content: `Tag aku lalu tulis perintahnya, contoh: @bot cek status server, atau @bot ${PIPELINE_KEYWORD} buatkan web html statis`,
      allowedMentions,
    });
  }

  if (prompt.toLowerCase() === 'ping') {
    return message.reply({ content: `Pong! Latensi ${Math.round(client.ws.ping)} ms`, allowedMentions });
  }

  // Ada lampiran (dan bukan perintah pipeline) -> download dan simpan ke folder tujuan
  if (message.attachments.size > 0 && parsePipelineIdea(prompt) === null) {
    return handleUpload(message, prompt, allowedMentions);
  }

  // Awalan kata kunci, atau permintaan membuat proyek -> langsung ke pipeline.py (tanpa Hermes)
  const idea = parsePipelineIdea(prompt);
  if (idea !== null) return handlePipeline(message, idea, allowedMentions);
  if (looksLikeBuild(prompt)) return handlePipeline(message, prompt, allowedMentions);

  return handleHermes(message, prompt, allowedMentions);
});

process.on('unhandledRejection', (err) => console.error('unhandledRejection:', err));

client.login(DISCORD_TOKEN);
