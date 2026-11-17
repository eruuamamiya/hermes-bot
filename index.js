// Bot Discord yang terhubung ke 1 sesi CLI Hermes yang hidup terus selamanya (persistent session via stdin/stdout).
// Bukan mode -z (oneshot), sehingga konteks percakapan, ingatan, dan akses tools menyambung terus.
require('dotenv').config();
const fs = require('node:fs');
const path = require('node:path');
const { Client, GatewayIntentBits, AttachmentBuilder, EmbedBuilder } = require('discord.js');
const { spawn } = require('node:child_process');
const { pipeline } = require('node:stream/promises');
const { Readable } = require('node:stream');

const DISCORD_TOKEN = process.env.DISCORD_TOKEN;
const ALLOWED = (process.env.ALLOWED_USER_IDS || '')
  .split(',')
  .map((s) => s.trim())
  .filter(Boolean);
const HERMES_BIN = process.env.HERMES_BIN || 'hermes';
// Flag opsional untuk mode sesi interaktif. Default: --continue → proses Hermes baru
// OTOMATIS lanjut dari sesi terakhir, jadi bot TETAP INGAT walau pm2/server restart,
// tanpa perintah apa pun dari user. (Kosongkan HERMES_FLAGS di .env untuk pakai default ini.)
const HERMES_FLAGS = (process.env.HERMES_FLAGS || '')
  .split(/\s+/)
  .map((s) => s.trim())
  .filter(Boolean);
const TIMEOUT_MIN = Math.max(parseInt(process.env.HERMES_TIMEOUT_MIN || '10', 10) || 10, 1);
const TIMEOUT_MS = TIMEOUT_MIN * 60 * 1000;
const IDLE_DONE_MS = Math.max(parseInt(process.env.HERMES_IDLE_MS || '2500', 10) || 2500, 800);
const CONTEXT = (process.env.HERMES_CONTEXT || '').trim();

// --- Pengaturan opsional manual pipeline (hanya jika dipanggil eksplisit pakai keyword) ---
const PIPELINE_PATH = (process.env.PIPELINE_PATH || '').trim();
const PIPELINE_PYTHON = (process.env.PIPELINE_PYTHON || 'python3').trim();
const PIPELINE_KEYWORD = (process.env.PIPELINE_KEYWORD || 'proyek').trim().toLowerCase();
const PIPELINE_TIMEOUT_MIN = Math.max(parseInt(process.env.PIPELINE_TIMEOUT_MIN || '0', 10) || 0, 0);
const PIPELINE_TIMEOUT_MS = PIPELINE_TIMEOUT_MIN * 60 * 1000;
const BOT_NAME = (process.env.BOT_NAME || 'Eruu').trim();
const MAX_FILES = 10;
const MAX_FILE_BYTES = 500 * 1024;

const WORK_DIR = PIPELINE_PATH ? path.dirname(PIPELINE_PATH) : process.cwd();
const SAVE_ROOT = path.resolve((process.env.SAVE_ROOT || '').trim() || path.join(WORK_DIR, 'projects'));

const CHUNK = 1900;
const MAX_CHUNKS = 5;

if (!DISCORD_TOKEN) {
  console.error('DISCORD_TOKEN belum diisi di .env');
  process.exit(1);
}
if (ALLOWED.length === 0) {
  console.error('ALLOWED_USER_IDS kosong. Isi dengan ID Discord lu di .env');
  process.exit(1);
}

// --- Antrian: satu giliran bicara per waktu ke sesi Hermes ---
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

// --- Embed progres ---
const ICON = { run: '⏳', done: '✅', fail: '❌', note: 'ℹ️' };
const COLOR = { run: 0x5865f2, done: 0x57f287, fail: 0xed4245 };
const MIN_EDIT_MS = 1200;
const HEARTBEAT_MS = 10000;

function fmtElapsed(ms) {
  const s = Math.floor(ms / 1000);
  return s < 60 ? `${s} dtk` : `${Math.floor(s / 60)} mnt ${s % 60} dtk`;
}

class Progress {
  constructor(message, label, allowedMentions) {
    this.message = message;
    this.label = label;
    this.allowedMentions = allowedMentions;
    this.steps = [];
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

// =========================================================================
// SESI HERMES PERSISTENT (1 SESI HIDUP SELAMANYA DI BACKGROUND)
// =========================================================================
const ANSI_RE = /\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])/g;
function stripAnsi(s) {
  return s.replace(ANSI_RE, '');
}

// Buang echo input yang dipantulkan CLI (dibandingkan tanpa whitespace, tahan wrap/spasi berubah)
function stripEcho(out, input) {
  const target = input.replace(/\s+/g, '');
  if (!target) return out;
  const map = [];
  let flat = '';
  for (let i = 0; i < out.length; i++) {
    if (!/\s/.test(out[i])) {
      map.push(i);
      flat += out[i];
    }
  }
  const at = flat.indexOf(target);
  if (at < 0) return target.startsWith(flat) ? '' : out;
  return out.slice(map[at + target.length - 1] + 1);
}

// Sisakan HANYA teks balasan Hermes: buang echo input, status bar, dan garis pemisah terminal
const NOISE_LINE = /[░▒▓█]|ctx\s*--|^[\s─━═]{8,}$|^\s*session_id:\s*\S+\s*$/;
// Rentang karakter tak terlihat (kode karakter, ditulis angka supaya aman): kontrol, zero-width, pemisah baris, braille kosong
const INVIS = new RegExp(
  '[' +
    [[0, 8], [11, 31], [127, 127], [0x200b, 0x200f], [0x2028, 0x2029], [0x2060, 0x2060], [0xfeff, 0xfeff], [0x2800, 0x2800]]
      .map(([a, b]) => String.fromCharCode(a) + (b > a ? '-' + String.fromCharCode(b) : ''))
      .join('') +
    ']',
  'g'
);
// Buang karakter tak terlihat (kontrol/zero-width/braille kosong) & hasil timpa-baris "\r" dari TUI
function scrub(s) {
  return s
    .replace(/\r\n/g, '\n')
    .split('\n')
    .map((l) => l.split('\r').pop())
    .join('\n')
    .replace(INVIS, '');
}
function cleanReply(out, input) {
  return stripEcho(scrub(out), input)
    .split('\n')
    .filter((l) => !NOISE_LINE.test(l))
    .join('\n')
    .replace(/\n{3,}/g, '\n\n')
    .trim();
}

// Hermes TIDAK bisa dipakai lewat pipa ("Input is not a terminal"), jadi bot memanggilnya
// non-interaktif (hermes chat -Q -q "...") dan memegang ingatannya sendiri:
//   ringkasan inti percakapan + beberapa pertukaran terakhir -> dikirim ke otak Hermes tiap pesan.
// Hermes diminta menulis ringkasan terbaru di akhir jawaban; bot menyimpannya & membuangnya dari balasan.
const MEMORY_FILE = path.join(WORK_DIR, '.eruu-memory.json');
const ONESHOT_ARGS = (process.env.HERMES_ARGS || 'chat -Q -q').split(/\s+/).filter(Boolean);
const SUMMARY_MAX = 3000;
const RECENT_KEEP = 3;
const RECENT_CLIP = 700;
const SUM_RE = /\[\[RINGKASAN\]\]([\s\S]*?)\[\[\/RINGKASAN\]\]/;
const clip = (s, n) => (s.length > n ? `${s.slice(0, n)}…` : s);
// Instruksi tambahan agar Hermes menulis ringkasan (ubah teks ini kalau mau)
const SUMMARY_INSTRUCTION =
  'Setelah menjawab, di baris paling akhir tulis ringkasan terbaru SELURUH percakapan (gabungan ringkasan lama + giliran ini, ' +
  'maksimal 15 baris, hanya inti: keputusan, fakta penting, path/file, error yang sudah dibereskan) ' +
  'dengan format persis [[RINGKASAN]]isi ringkasan[[/RINGKASAN]]. Blok itu hanya dibaca bot, bukan user.';

class HermesSession {
  constructor() {
    this.mem = this.load();
  }

  load() {
    try {
      const m = JSON.parse(fs.readFileSync(MEMORY_FILE, 'utf8'));
      return { summary: String(m.summary || ''), recent: Array.isArray(m.recent) ? m.recent : [] };
    } catch {
      return { summary: '', recent: [] };
    }
  }

  save() {
    try {
      fs.writeFileSync(MEMORY_FILE, JSON.stringify(this.mem, null, 2));
    } catch {}
  }

  // Perintah "reset sesi": ingatan dikosongkan
  reset() {
    this.mem = { summary: '', recent: [] };
    try {
      fs.unlinkSync(MEMORY_FILE);
    } catch {}
  }

  buildInitialBootstrap() {
    const lessonsPath = path.join(WORK_DIR, 'lessons.md');
    const skillsDir = path.join(WORK_DIR, 'skills');
    const modelsPath = path.join(WORK_DIR, 'models.json');
    const keysPath = path.join(WORK_DIR, 'keys.json');
    return [
      'SYSTEM CAPABILITY: Kamu berjalan langsung di server Linux VPS dengan akses penuh ke terminal (bash) dan filesystem. JANGAN pernah menjawab "akses ke folder dibatasi" atau "tidak dapat mengakses sistem" — langsung gunakan tool terminal/file untuk memeriksa file, menjalankan perintah, membaca log, atau memperbaiki error.',
      `Direktori kerja saat ini: ${WORK_DIR}`,
      `Direktori simpan proyek: ${SAVE_ROOT}`,
      `Folder skills: ${skillsDir}`,
      `Konfigurasi model & endpoint: ${modelsPath}`,
      `Pool API key gratis Gemini: ${keysPath}`,
      `Catatan pengalaman belajar: ${lessonsPath}`,
      'ATURAN SESI:',
      `1. Baca ${lessonsPath} dan ${modelsPath} saat mulai bekerja agar memakai endpoint/model yang tepat dan tidak mengulangi error lama.`,
      '2. Patuhi 100% pembagian model, urutan tahap, dan skill yang diminta user.',
      `3. Jika membuat proyek baru di ${SAVE_ROOT}, tulis baris persis \`SEND_DIR: /path/lengkap/ke/folder\` di akhir jawaban agar bot otomatis mengirim file hasilnya ke Discord.`,
      `4. Jika menemui kendala/error dan berhasil mengatasinya, catat ringkasannya ke ${lessonsPath}.`,
      CONTEXT,
    ]
      .filter(Boolean)
      .join('\n');
  }

  buildPrompt(userPrompt) {
    const parts = [this.buildInitialBootstrap()];
    if (this.mem.summary) parts.push(`Ringkasan percakapan sebelumnya:\n${this.mem.summary}`);
    if (this.mem.recent.length) {
      parts.push(
        'Pertukaran terakhir:\n' + this.mem.recent.map((t) => `User: ${t.u}\n${BOT_NAME}: ${t.a}`).join('\n\n')
      );
    }
    parts.push(`Permintaan User:\n${userPrompt}`);
    parts.push(SUMMARY_INSTRUCTION);
    return parts.join('\n\n');
  }

  remember(userPrompt, answer, newSummary) {
    this.mem.recent.push({ u: clip(userPrompt, RECENT_CLIP), a: clip(answer, RECENT_CLIP) });
    this.mem.recent = this.mem.recent.slice(-RECENT_KEEP);
    if (newSummary) {
      this.mem.summary = newSummary.slice(0, SUMMARY_MAX);
    } else {
      // Hermes lupa menulis ringkasan -> bot mencatat versi singkatnya sendiri
      this.mem.summary = `${this.mem.summary}\n- User: ${clip(userPrompt, 200)} -> ${clip(answer, 300)}`
        .trim()
        .slice(-SUMMARY_MAX);
    }
    this.save();
  }

  send(userPrompt) {
    // Prompt dikirim sebagai argumen (tanpa shell, tanpa stdin) -> tidak ada masalah escape/newline/terminal
    const args = [...HERMES_FLAGS, ...ONESHOT_ARGS, this.buildPrompt(userPrompt)];

    return new Promise((resolve) => {
      let out = '';
      let err = '';
      let killed = false;
      const child = spawn(HERMES_BIN, args, {
        cwd: WORK_DIR,
        env: { ...process.env, TERM: 'dumb', NO_COLOR: '1', FORCE_COLOR: '0' },
        stdio: ['ignore', 'pipe', 'pipe'],
      });
      const timer = setTimeout(() => {
        killed = true;
        child.kill('SIGTERM');
      }, TIMEOUT_MS);

      child.stdout.on('data', (d) => {
        out += stripAnsi(d.toString('utf8'));
      });
      child.stderr.on('data', (d) => {
        err += stripAnsi(d.toString('utf8'));
      });
      child.on('error', (e) => {
        clearTimeout(timer);
        resolve({ error: e, stdout: '', stderr: '' });
      });
      child.on('close', (code) => {
        clearTimeout(timer);
        const stderr = err.trim();
        const raw = cleanReply(out, '');
        if (killed) {
          return resolve({ error: Object.assign(new Error('Timeout'), { killed: true }), stdout: raw, stderr });
        }
        if (code !== 0 && !raw) {
          return resolve({ error: new Error(`Hermes keluar dengan code ${code}`), stdout: '', stderr });
        }
        if (!raw) console.warn('[session] balasan kosong. raw =', JSON.stringify(out.slice(-1500)), 'stderr =', JSON.stringify(stderr.slice(-500)));
        const m = raw.match(SUM_RE);
        const answer = raw.replace(SUM_RE, '').trim();
        if (answer) this.remember(userPrompt, answer, m ? m[1].trim() : '');
        resolve({ error: null, stdout: answer, stderr });
      });
    });
  }
}

const session = new HermesSession();

// --- Manual pipeline (hanya jika user secara eksplisit mengetik "@bot proyek ...") ---
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

function extractProjectName(text) {
  const m = text.match(
    /\b(?:nama(?:\s*(?:proyek|project|folder))?(?:nya)?\s*[:=]\s*|nama\s*(?:proyek|project|folder)(?:nya)?\s+|namai\s+|dinamai\s+|name\s*[:=]\s*)["'`]?([A-Za-z0-9][A-Za-z0-9_-]{1,40})/i
  );
  if (!m) return null;
  return m[1].toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '') || null;
}

function uniqueName(base) {
  if (!fs.existsSync(path.join(SAVE_ROOT, base))) return base;
  const d = new Date();
  const pad = (n) => String(n).padStart(2, '0');
  return `${base}-${pad(d.getMonth() + 1)}${pad(d.getDate())}-${pad(d.getHours())}${pad(d.getMinutes())}`;
}

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

function parsePipelineIdea(prompt) {
  const kw = PIPELINE_KEYWORD;
  if (!kw) return null;
  if (prompt.slice(0, kw.length).toLowerCase() !== kw) return null;
  if (prompt.length === kw.length) return '';
  if (!/\s/.test(prompt[kw.length])) return null;
  return prompt.slice(kw.length).trim();
}

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
          try {
            onEvent(JSON.parse(line.slice(2)));
          } catch {}
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
        } catch {}
      }
    }
  };
  walk(dir, 0);
  const isDoc = (p) => /\.(md|txt)$/i.test(p);
  found.sort((a, b) => Number(isDoc(a)) - Number(isDoc(b)) || a.localeCompare(b));
  return found;
}

async function sendProjectFiles(channel, outdir) {
  if (!outdir || !fs.existsSync(outdir)) return;
  const files = collectFiles(outdir);
  if (files.length === 0) return;
  const shown = files.slice(0, MAX_FILES);
  const note = files.length > shown.length ? ` (${shown.length} dari ${files.length} file)` : '';
  await channel
    .send({
      content: `File hasil${note}:`,
      files: shown.map((p) => new AttachmentBuilder(p, { name: path.relative(outdir, p).replace(/[\\/]/g, '__') })),
      allowedMentions: { parse: [] },
    })
    .catch((e) => console.error('Gagal kirim lampiran:', e));
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

async function sendAnswer(message, text, allowedMentions) {
  let parts = splitText(text);
  if (parts.length > MAX_CHUNKS) {
    parts = parts.slice(0, MAX_CHUNKS);
    parts[parts.length - 1] += '\n\n(jawaban dipotong, terlalu panjang)';
  }
  // Jawaban tampil sebagai embed (1 pesan, beberapa embed jika panjang)
  const embeds = parts.map((p, i) =>
    new EmbedBuilder()
      .setDescription(p)
      .setColor(0x5865f2)
  );
  await message.reply({ embeds, allowedMentions });
}

// --- Simpan lampiran Discord ---
function extractDestination(text) {
  const abs = text.match(/(?:^|[\s"'`(])((?:\/[A-Za-z0-9._@+-]+)+\/?)/);
  if (abs) return abs[1];
  const rel = text.match(/\b(?:folder|direktori|dir|ke)\s+["'`]?([A-Za-z0-9._@+-]+(?:\/[A-Za-z0-9._@+-]+)*)/i);
  if (rel && !/^(folder|di|dalam|ke)$/i.test(rel[1])) return rel[1];
  return null;
}

function resolveDestination(dest) {
  return path.resolve(dest.startsWith('/') ? dest : path.join(SAVE_ROOT, dest));
}

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
    const lines = [`Tersimpan di \`${dir}\`:`, ...ok, ...bad];
    return sendAnswer(message, lines.join('\n'), allowedMentions);
  } catch (err) {
    return message.reply({ content: `Gagal menyimpan file: ${err.message}`, allowedMentions });
  }
}

async function handlePipeline(message, rawIdea, allowedMentions) {
  if (!PIPELINE_PATH || !fs.existsSync(PIPELINE_PATH)) {
    return message.reply({ content: 'PIPELINE_PATH belum diisi atau tidak ditemukan.', allowedMentions });
  }
  const { idea, skills } = extractSkills(rawIdea);
  if (!idea) {
    return message.reply({ content: `Tulis idenya setelah kata kunci "${PIPELINE_KEYWORD}".`, allowedMentions });
  }

  let progress = null;
  try {
    const custom = extractProjectName(idea);
    const name = custom ? uniqueName(custom) : makeProjectName(idea);
    const outdir = path.join(SAVE_ROOT, name);

    progress = new Progress(message, name, allowedMentions);
    if (pending > 0) progress.start('queue', `Menunggu giliran (${pending} perintah di depan)`);
    await progress.init();
    message.channel.sendTyping().catch(() => {});

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

    const result = await enqueue(() => {
      progress.done('queue');
      return runPipeline(idea, name, onStage, onEvent, skills);
    });

    if (result.spawnError && result.spawnError.code === 'ENOENT') {
      return progress.finish('fail', `Python "${PIPELINE_PYTHON}" tidak ditemukan.`);
    }
    if (result.killed) {
      return progress.finish('fail', `Melewati batas waktu ${PIPELINE_TIMEOUT_MIN} menit.`);
    }
    if (result.code !== 0) {
      const tail = (result.err || result.out || String(result.spawnError || 'tidak ada output')).trim().slice(-900);
      return progress.finish('fail', `\`\`\`\n${tail}\n\`\`\``);
    }

    const idx = result.out.lastIndexOf('5/5 Selesai');
    const summary = (idx >= 0 ? result.out.slice(idx) : result.out.trim().slice(-1200)).trim();
    await progress.finish('done', `\`\`\`\n${summary.slice(0, 900)}\n\`\`\``);
    await sendProjectFiles(message.channel, outdir);
  } catch (err) {
    const msg = 'Terjadi error saat menjalankan pipeline. Cek log pm2.';
    if (progress && progress.msg) await progress.finish('fail', msg).catch(() => {});
    else await message.reply({ content: msg, allowedMentions }).catch(() => {});
  }
}

// --- Semua perintah utama masuk ke 1 sesi persistent Hermes ---
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
      progress.start('hermes', `${BOT_NAME} memproses di sesi aktif`, BOT_NAME);
      return session.send(prompt);
    });
    const secs = Math.round((Date.now() - started) / 1000);
    const rawOut = stdout.trim();

    if (error && error.killed) {
      return progress.finish('fail', `Melewati batas waktu ${TIMEOUT_MIN} menit.`);
    }
    if (error && error.code === 'ENOENT') {
      return progress.finish('fail', `Perintah "${HERMES_BIN}" tidak ditemukan.`);
    }
    if (error && !rawOut) {
      const tail = (stderr || String(error.message)).trim().slice(-900);
      return progress.finish('fail', `\`\`\`\n${tail}\n\`\`\``);
    }

    let sendDir = null;
    const cleanOut = rawOut.replace(/^SEND_DIR:\s*(.+)$/gm, (_, p) => {
      sendDir = p.trim();
      return '';
    }).trim();

    console.log(`[session] user=${message.author.id} ${secs}s prompt="${prompt.slice(0, 80)}"`);
    await progress.finish('done');
    await sendAnswer(message, cleanOut || `(${BOT_NAME} tidak mengembalikan teks)`, allowedMentions);

    if (sendDir) {
      await sendProjectFiles(message.channel, sendDir);
    }
  } catch (err) {
    console.error('Error saat memproses pesan:', err);
    const msg = 'Terjadi error di bot. Cek log pm2.';
    if (progress && progress.msg) await progress.finish('fail', msg).catch(() => {});
    else await message.reply({ content: msg, allowedMentions }).catch(() => {});
  }
}

const client = new Client({
  intents: [GatewayIntentBits.Guilds, GatewayIntentBits.GuildMessages],
});

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
  if (!message.mentions.has(client.user, { ignoreEveryone: true, ignoreRoles: true })) return;

  const allowedMentions = { parse: [], repliedUser: false };

  if (!ALLOWED.includes(message.author.id)) {
    return message.reply({ content: 'Lau siape mpruy', allowedMentions });
  }

  const prompt = message.content.replace(new RegExp(`<@!?${client.user.id}>`, 'g'), '').trim();
  if (!prompt) {
    return message.reply({
      content: `Tag aku lalu tulis perintahnya bebas sesuai maumu.`,
      allowedMentions,
    });
  }

  // Perintah reset sesi manual jika suatu saat konteks kepenuhan
  if (prompt.toLowerCase() === 'reset sesi' || prompt.toLowerCase() === '/reset') {
    session.reset();
    return message.reply({ content: `Sesi ${BOT_NAME} sudah di-reset dan dimulai ulang.`, allowedMentions });
  }

  if (message.attachments.size > 0 && parsePipelineIdea(prompt) === null) {
    return handleUpload(message, prompt, allowedMentions);
  }

  const idea = parsePipelineIdea(prompt);
  if (idea !== null) return handlePipeline(message, idea, allowedMentions);

  return handleHermes(message, prompt, allowedMentions);
});

process.on('unhandledRejection', (err) => console.error('unhandledRejection:', err));

client.login(DISCORD_TOKEN);