#!/usr/bin/env python3
"""
Pipeline multi-model: Gemini (rencana) -> DeepSeek (tambah fitur) -> Sonnet (kode)
-> cek otomatis -> DeepSeek (periksa) -> hasil dikirim ke kamu.

Pakai:
  export VIKEY_API_KEY="..."
  python3 pipeline.py "bot WA yang ..." --name wa-bot
  python3 pipeline.py "..." --name x --rounds 3 --approve
  python3 pipeline.py --resume nama-proyek          # lanjut dari PROJECT.md (lewati rencana)
  python3 pipeline.py "ide asli" --resume nama-proyek  # kalau IDEA.txt belum ada (proyek lama)

Hasil ada di folder projects/<name>/ : kode, PROJECT.md, DECISIONS.md, REVIEW.md
"""
import argparse
import datetime
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request

HERE = pathlib.Path(__file__).resolve().parent

# ---------- Konfigurasi ----------
VIKEY_URL = os.getenv("VIKEY_BASE_URL", "https://api.vikey.ai/v1")
VIKEY_KEY = os.getenv("VIKEY_API_KEY", "")
GEMINI_URL = os.getenv("GEMINI_BASE_URL", "https://generativelanguage.googleapis.com/v1beta/openai")
M_GEMINI = os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite")  # cek nama persisnya di dashboard Google AI Studio
M_DEEPSEEK = os.getenv("DEEPSEEK_MODEL", "deepseek/deepseek-v4.1-flash-1017")
M_SONNET = os.getenv("SONNET_MODEL", "anthropic/claude-sonnet-5.5")

GEMINI_DAILY_LIMIT = int(os.getenv("GEMINI_DAILY_LIMIT", "480"))  # batas aman dari 500/hari
GEMINI_COOLDOWN = 60  # detik istirahat key setelah kena 429

# Harga Rp per 1M token (input, output)
PRICES = {
    M_SONNET: (18000, 72000),
    M_DEEPSEEK: (1800, 6660),
}

# Konteks tetap untuk semua model (hemat: ditulis sekali, dipakai di semua tahap)
DEFAULT_CONSTRAINTS = """\
- Proyek dijalankan di STB HG680P (Armbian, ARM64, RAM 2GB) lewat pm2.
- Bahasa: Node.js (utamakan) atau Python. Hindari dependensi native yang berat.
- Semua rahasia (token, API key) lewat file .env, sediakan .env.example.
- Sertakan package.json atau requirements.txt yang lengkap dan README singkat cara jalan.
- Kode sederhana, mudah dibaca, dengan komentar singkat di bagian penting.
"""

# Batas diam per koneksi (detik) kalau server berhenti merespons sama sekali; bukan batas total proses.
HTTP_TIMEOUT = int(os.getenv("HTTP_TIMEOUT", "900"))

CODE_MAX_TOKENS = int(os.getenv("CODE_MAX_TOKENS", "12000"))  # batas output tahap kode; naikkan kalau file terpotong

TOTAL = {"cost": 0.0, "calls": 0}

# ---------- Skill: aturan tambahan dari folder skills/*.md ----------
SKILLS_DIR = HERE / "skills"
SKILLS_MAX_CHARS = int(os.getenv("SKILLS_MAX_CHARS", "40000"))  # jaga biaya token
SKILL_NAMES = []  # nama skill yang dimuat
SKILL_MISSING = []  # nama skill yang diminta tapi filenya tidak ada
SKILL_SKIPPED = []  # nama skill yang dilewati karena melebihi SKILLS_MAX_CHARS
_FRONT = re.compile(r"\A---\s*\n.*?\n---\s*\n", re.DOTALL)  # front matter (name/description) dibuang
SKILL_TEXT = ""  # teks gabungan yang ditempel ke prompt


def _norm(s):
    return re.sub(r"[^a-z0-9]", "", s.lower())


def skills_from_prompt(idea):
    """Skill yang disebut di prompt; tanda strip/spasi diabaikan ('antislop' = anti-slop)."""
    if not SKILLS_DIR.is_dir():
        return []
    toks = {_norm(t) for t in re.findall(r"[\w-]+", idea)}
    return [fp.stem.lower() for fp in sorted(SKILLS_DIR.glob("*.md")) if _norm(fp.stem) in toks]


def load_skills(idea=""):
    """Baca skills/*.md. Prioritas pilihan: env SKILLS=nama1,nama2 > skill yang disebut di prompt > semua. Return nama yang dimuat."""
    global SKILL_TEXT
    SKILL_NAMES.clear()
    SKILL_MISSING.clear()
    SKILL_SKIPPED.clear()
    SKILL_TEXT = ""
    want = [s.strip().lower() for s in os.getenv("SKILLS", "").split(",") if s.strip()]
    if not want and idea:
        want = skills_from_prompt(idea)
    if not SKILLS_DIR.is_dir():
        SKILL_MISSING.extend(want)
        return SKILL_NAMES
    have = {fp.stem.lower() for fp in SKILLS_DIR.glob("*.md")}
    SKILL_MISSING.extend(w for w in want if w not in have)
    blocks, used = [], 0
    for fp in sorted(SKILLS_DIR.glob("*.md")):
        if want and fp.stem.lower() not in want:
            continue
        try:
            body = _FRONT.sub("", fp.read_text(encoding="utf-8"), count=1).strip()
        except Exception:
            continue
        if not body:
            continue
        if used + len(body) > SKILLS_MAX_CHARS:
            SKILL_SKIPPED.append(fp.stem)
            continue
        blocks.append(f"### Skill: {fp.stem}\n{body}")
        SKILL_NAMES.append(fp.stem)
        used += len(body)
    if blocks:
        SKILL_TEXT = "\n\n".join(blocks)
    return SKILL_NAMES


def skill_block(heading):
    return f"\n\n{heading}\n{SKILL_TEXT}\n" if SKILL_TEXT else ""

# ---------- Alur model ditentukan PROMPT (prioritas 1); yang tidak disebut ikut default ----------
LABEL = {"gemini": "Gemini", "deepseek": "DeepSeek", "sonnet": "Sonnet", "skip": "dilewati"}
VALID = set(LABEL)
ROLE_DEFAULT = {"plan": "gemini", "expand": "deepseek", "code": "sonnet", "review": "deepseek", "fix": "same"}  # fix "same" = ikut model code
ROLES = dict(ROLE_DEFAULT)
MODEL_WORDS = re.compile(r"gemini|deep\s*seek|sonnet|claude", re.IGNORECASE)
# Larangan model: "tanpa sonnet", "tanpa deepseek", "tanpa gemini", "jangan pakai X", "no X", "without X", dst.
BAN_RE = re.compile(
    r"\b(?:jangan|jgn|janganlah|ngga?k?|gak?|tidak|tdk|tanpa|no|without|don'?t|do not)\s+"
    r"(?:usah\s+)?(?:(?:pakai|pake|pakek|memakai|menggunakan|gunakan|use|using)\s+)?"
    r"(sonnet|claude|gemini|deep\s*seek)\b",
    re.IGNORECASE,
)
ROUTER_SYS = "Kamu pembaca instruksi. Jawab HANYA dengan JSON valid, tanpa teks lain."
ROUTER_USER = """\
Dari prompt pengguna di bawah, ambil model mana yang diminta mengerjakan tiap peran.
Peran:
- plan: membuat rencana / struktur project
- expand: menambah fitur yang terlewat ke rencana
- code: menulis file kode
- review: preview / memeriksa hasil / mencari bug
- fix: memperbaiki masalah yang ketemu
Nilai: "gemini", "deepseek", "sonnet", atau "skip" (kalau peran itu diminta tidak dikerjakan). Pakai null untuk peran yang TIDAK disebut prompt.
"banned": daftar model yang dilarang ("jangan pakai X", "tanpa X"), isi dari "gemini","deepseek","sonnet".
Ikuti urutan dan pembagian kerja persis seperti tertulis di prompt. Abaikan bagian prompt yang tidak soal pembagian model.

Prompt:
<<<
{idea}
>>>

Jawab: {{"plan":null,"expand":null,"code":null,"review":null,"fix":null,"banned":[]}}"""


def who(role):
    return LABEL[ROLES[role]]


def resolve_roles(idea, no_sonnet_flag=False):
    """Prioritas: PROMPT > flag --no-sonnet > default. Return (roles, model_dilarang)."""
    roles = dict(ROLE_DEFAULT)
    banned = set()
    if no_sonnet_flag:
        banned.add("sonnet")
    for m in BAN_RE.finditer(idea):  # "tanpa/jangan pakai sonnet|deepseek|gemini|claude"
        w = re.sub(r"\s+", "", m.group(1).lower())
        banned.add("sonnet" if w == "claude" else w)  # "deep seek" sudah jadi "deepseek" lewat penghapusan spasi
    if MODEL_WORDS.search(idea):  # prompt menyebut model -> Gemini (gratis) menerjemahkan jadi pembagian peran
        try:
            data = extract_json(planner_chat(
                [{"role": "system", "content": ROUTER_SYS},
                 {"role": "user", "content": ROUTER_USER.format(idea=idea)}], 600, "route")) or {}
        except Exception as e:
            print(f"    router gagal ({e!r}), pakai default + deteksi kata kunci", flush=True)
            data = {}
        for m in data.get("banned") or []:
            m = str(m).lower().replace(" ", "")
            if m in {"gemini", "deepseek", "sonnet"}:
                banned.add(m)
        for r in ROLE_DEFAULT:
            v = data.get(r)
            v = str(v).lower().strip() if v is not None else ""
            if v in VALID | {"same"}:
                roles[r] = v
    for r in ("plan", "code"):  # peran wajib tidak boleh skip
        if roles[r] in ("skip", "same"):
            roles[r] = ROLE_DEFAULT[r] if ROLE_DEFAULT[r] != "same" else "deepseek"
    if roles["fix"] == "same":
        roles["fix"] = roles["code"]
    pref = [m for m in ("deepseek", "gemini", "sonnet") if m not in banned]
    for r, v in list(roles.items()):  # model terlarang diganti
        if v in banned:
            roles[r] = pref[0] if pref else v
    return roles, sorted(banned)


def ask(role, msgs, max_tokens, tag, on_text=None):
    """Panggil model yang ditugaskan untuk peran ini."""
    m = ROLES[role]
    if m == "gemini":  # tanpa stream; kalau semua key Gemini gagal, planner_chat fallback ke DeepSeek
        return planner_chat(msgs, max_tokens, tag)
    return vikey_chat(M_SONNET if m == "sonnet" else M_DEEPSEEK, msgs, max_tokens, tag, on_text=on_text)


# Event progres untuk bot Discord: dicetak sebagai baris "@@{json}" kalau PIPELINE_EVENTS=1.
EVENTS = os.getenv("PIPELINE_EVENTS", "") == "1"


def ev(kind, **kw):
    """kind: start (mulai langkah), done (selesai, boleh ganti teks), fail, note (catatan)."""
    if EVENTS:
        print("@@" + json.dumps({"e": kind, **kw}, ensure_ascii=False), flush=True)


# ---------- Util umum ----------
def log_usage(model, usage, tag=""):
    p = usage.get("prompt_tokens", 0) or 0
    c = usage.get("completion_tokens", 0) or 0
    price = PRICES.get(model, (0, 0))
    cost = p * price[0] / 1e6 + c * price[1] / 1e6
    TOTAL["cost"] += cost
    TOTAL["calls"] += 1
    rec = {
        "t": datetime.datetime.now().isoformat(timespec="seconds"),
        "model": model, "tag": tag, "in": p, "out": c, "rp": round(cost, 2),
    }
    with open(HERE / "usage.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(rec) + "\n")
    print(f"    [{model.split('/')[-1]}] in={p} out={c} ~Rp{cost:,.0f}", flush=True)


def http_chat(base, key, model, messages, max_tokens, temperature=0.3, timeout=HTTP_TIMEOUT):
    body = json.dumps({
        "model": model, "messages": messages,
        "max_tokens": max_tokens, "temperature": temperature,
    }).encode()
    req = urllib.request.Request(
        base.rstrip("/") + "/chat/completions", data=body,
        headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def http_chat_stream(base, key, model, messages, max_tokens, on_text, temperature=0.3, timeout=HTTP_TIMEOUT):
    """Panggil model dengan stream; tiap potongan teks diteruskan ke on_text. Return (teks, usage)."""
    body = json.dumps({
        "model": model, "messages": messages,
        "max_tokens": max_tokens, "temperature": temperature,
        "stream": True, "stream_options": {"include_usage": True},
    }).encode()
    req = urllib.request.Request(
        base.rstrip("/") + "/chat/completions", data=body,
        headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"},
    )
    parts, usage = [], {}
    with urllib.request.urlopen(req, timeout=timeout) as r:
        for raw in r:
            line = raw.decode("utf-8", "ignore").strip()
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if payload == "[DONE]":
                break
            try:
                obj = json.loads(payload)
            except Exception:
                continue
            if obj.get("usage"):
                usage = obj["usage"]
            piece = ((obj.get("choices") or [{}])[0].get("delta") or {}).get("content") or ""
            if piece:
                parts.append(piece)
                on_text(piece)
    return "".join(parts), usage


def vikey_chat(model, messages, max_tokens=3000, tag="", on_text=None):
    if not VIKEY_KEY:
        sys.exit("VIKEY_API_KEY belum di-set.")
    last_err = None
    if on_text:  # coba stream dulu supaya progres per file kelihatan; kalau gagal, jalan biasa
        try:
            text, usage = http_chat_stream(VIKEY_URL, VIKEY_KEY, model, messages, max_tokens, on_text)
            if text:
                if not usage:  # penyedia tidak kirim usage: perkirakan kasar (4 karakter ~ 1 token)
                    usage = {"prompt_tokens": sum(len(m["content"]) for m in messages) // 4,
                             "completion_tokens": len(text) // 4}
                log_usage(model, usage, tag)
                return text
        except Exception as e:
            print(f"    stream gagal ({e!r}), pakai panggilan biasa", flush=True)
    for attempt in range(3):
        try:
            data = http_chat(VIKEY_URL, VIKEY_KEY, model, messages, max_tokens)
            log_usage(model, data.get("usage", {}), tag)
            return data["choices"][0]["message"]["content"]
        except urllib.error.HTTPError as e:
            last_err = f"HTTP {e.code}: {e.read()[:200]!r}"
            if e.code in (429, 500, 502, 503, 504):
                time.sleep(3 * (attempt + 1))
                continue
            break
        except Exception as e:  # jaringan, timeout, dll
            last_err = repr(e)
            time.sleep(3 * (attempt + 1))
    raise RuntimeError(f"Panggilan {model} gagal: {last_err}")


# ---------- Rotasi key Gemini ----------
class GeminiPool:
    def __init__(self):
        keys = []
        kp = HERE / "keys.json"
        if kp.exists():
            keys += json.loads(kp.read_text()).get("gemini", [])
        keys += [k.strip() for k in os.getenv("GEMINI_API_KEYS", "").split(",") if k.strip()]
        self.keys = list(dict.fromkeys(keys))
        self.state_path = HERE / "keys_state.json"
        try:
            self.state = json.loads(self.state_path.read_text())
        except Exception:
            self.state = {}

    @staticmethod
    def _id(key):
        return key[-8:]

    def _save(self):
        self.state_path.write_text(json.dumps(self.state))

    def _entry(self, key):
        today = str(datetime.date.today())
        e = self.state.get(self._id(key))
        if not e or e.get("day") != today:
            e = {"day": today, "count": 0, "cool": 0}
            self.state[self._id(key)] = e
        return e

    def available(self):
        now = time.time()
        out = []
        for k in self.keys:
            e = self._entry(k)
            if e["count"] < GEMINI_DAILY_LIMIT and e["cool"] <= now:
                out.append((e["count"], k))
        out.sort()  # pakai yang paling sedikit terpakai dulu
        return [k for _, k in out]

    def chat(self, messages, max_tokens=3000, tag=""):
        for key in self.available():
            e = self._entry(key)
            try:
                data = http_chat(GEMINI_URL, key, M_GEMINI, messages, max_tokens)
                e["count"] += 1
                self._save()
                u = data.get("usage", {})
                print(f"    [gemini] in={u.get('prompt_tokens', 0)} out={u.get('completion_tokens', 0)} (gratis)", flush=True)
                return data["choices"][0]["message"]["content"]
            except urllib.error.HTTPError as e2:
                if e2.code == 429:
                    e["cool"] = time.time() + GEMINI_COOLDOWN
                    if b"per day" in (e2.read() or b"").lower():
                        e["count"] = GEMINI_DAILY_LIMIT
                    self._save()
                    continue
                print(f"    gemini error HTTP {e2.code}, coba key lain", flush=True)
                continue
            except Exception as ex:
                print(f"    gemini error {ex!r}, coba key lain", flush=True)
                continue
        raise RuntimeError("Semua key Gemini habis/error")


POOL = GeminiPool()


def planner_chat(messages, max_tokens=3000, tag=""):
    """Ketua (Gemini). Kalau gagal semua, fallback ke DeepSeek."""
    if POOL.keys:
        try:
            return POOL.chat(messages, max_tokens, tag)
        except RuntimeError as e:
            print(f"    {e} -> fallback DeepSeek", flush=True)
            ev("note", text="Gemini gagal, pindah ke DeepSeek")
    return vikey_chat(M_DEEPSEEK, messages, max_tokens, tag)


# ---------- Parsing & cek file ----------
FILE_RE = re.compile(r"=== FILE: (.+?) ===\n(.*?)\n=== END ===", re.DOTALL)


HEAD_LINE = re.compile(r"^\W*=== FILE:\s*(.+?)\s*===\W*$")


def _clean_file(files, path, lines):
    path = path.strip().strip("`")
    p = pathlib.PurePosixPath(path)
    if not path or p.is_absolute() or ".." in p.parts:
        return
    if lines and lines[0].startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    files[str(p)] = "\n".join(lines) + "\n"


def parse_files(text):
    files = {}
    for path, content in FILE_RE.findall(text):
        _clean_file(files, path, content.split("\n"))
    if files:
        return files
    # cadangan: format agak meleset (tanpa === END ===, output terpotong, header dibungkus **...**)
    cur, buf = None, []
    for line in text.split("\n"):
        m = HEAD_LINE.match(line)
        if m:
            if cur:
                _clean_file(files, cur, buf)
            cur, buf = m.group(1), []
        elif cur is not None:
            if line.strip().startswith("=== END"):
                _clean_file(files, cur, buf)
                cur = None
            else:
                buf.append(line)
    if cur:
        _clean_file(files, cur, buf)
    return files


def write_files(outdir, files):
    for rel, content in files.items():
        fp = outdir / rel
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(content, encoding="utf-8")


def read_all(outdir):
    res = {}
    for fp in sorted(outdir.rglob("*")):
        if fp.is_file() and fp.suffix in {".js", ".mjs", ".cjs", ".py", ".json", ".md", ".txt", ".sh", ".example", ""} \
                and "node_modules" not in fp.parts and fp.name not in {"PROJECT.md", "DECISIONS.md", "REVIEW.md", "NAME.txt"}:
            try:
                res[str(fp.relative_to(outdir))] = fp.read_text(encoding="utf-8")
            except Exception:
                pass
    return res


def auto_checks(outdir):
    """Cek sintaks otomatis (gratis, tanpa model). Return list masalah."""
    problems = []
    node = shutil.which("node")
    for fp in sorted(outdir.rglob("*")):
        if not fp.is_file() or "node_modules" in fp.parts:
            continue
        rel = str(fp.relative_to(outdir))
        try:
            if fp.suffix in {".js", ".mjs", ".cjs"} and node:
                r = subprocess.run([node, "--check", str(fp)], capture_output=True, text=True, timeout=30)
                if r.returncode != 0:
                    problems.append({"file": rel, "problem": "node --check gagal: " + r.stderr.strip()[:400]})
            elif fp.suffix == ".py":
                r = subprocess.run([sys.executable, "-m", "py_compile", str(fp)], capture_output=True, text=True, timeout=30)
                if r.returncode != 0:
                    problems.append({"file": rel, "problem": "py_compile gagal: " + r.stderr.strip()[:400]})
            elif fp.suffix == ".json":
                json.loads(fp.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            problems.append({"file": rel, "problem": f"JSON tidak valid: {e}"})
        except Exception as e:
            problems.append({"file": rel, "problem": f"cek gagal: {e!r}"})
    return problems


def extract_json(text):
    a, b = text.find("{"), text.rfind("}")
    if a == -1 or b == -1:
        return None
    try:
        return json.loads(text[a:b + 1])
    except Exception:
        return None


class FileWatcher:
    """Membaca teks yang sedang di-stream dan mengumumkan file yang sedang ditulis model."""
    HEAD = re.compile(r"=== FILE: (.+?) ===")

    def __init__(self, outdir, who, parent=None):
        self.outdir, self.who, self.parent = outdir, who, parent
        self.buf, self.open, self.names, self.fid = "", None, [], ""

    def feed(self, chunk):
        self.buf += chunk
        while True:
            if self.open is None:
                m = self.HEAD.search(self.buf)
                if not m:
                    self.buf = self.buf[-300:]
                    return
                path = m.group(1).strip().strip("`")
                self.buf = self.buf[m.end():]
                self.open = path
                self.names.append(path)
                if self.parent:
                    ev("done", id=self.parent)
                verb = "Mengubah" if (self.outdir / path).exists() else "Membuat"
                self.fid = f"f:{self.parent or ''}:{path}"
                ev("start", id=self.fid, text=f"{verb} {path}", who=self.who)
            else:
                i = self.buf.find("=== END ===")
                if i < 0:
                    self.buf = self.buf[-20:]
                    return
                self.buf = self.buf[i + len("=== END ==="):]
                ev("done", id=self.fid)
                self.open = None

    def finish(self, full_text):
        """Panggil setelah model selesai. Kalau stream tidak jalan, umumkan dari teks utuh."""
        if not self.names:
            self.buf = ""
            self.feed(full_text)
        if self.open:
            ev("done", id=self.fid)
            self.open = None
        if self.parent:
            ev("done", id=self.parent)


# ---------- Tahap-tahap ----------
FORMAT_PLAN = """\
Format jawaban (Markdown, ringkas, poin saja):
## Nama proyek
(satu nama saja: huruf kecil, angka, strip, maksimal 25 karakter, contoh: bot-musik)
## Tujuan
## Fitur
## Struktur file
## Checklist selesai
(Tulis checklist bernomor C1, C2, ... dan tiap butir harus bisa diperiksa dari kode.)
"""


def slugify(text):
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:25].strip("-")
    return s if len(s) >= 2 else "proyek-baru"


def extract_name(plan, idea):
    m = re.search(r"##\s*Nama proyek\s*\n+\s*[`*\"']*([A-Za-z0-9][A-Za-z0-9-]{1,30})", plan)
    return m.group(1).lower() if m else slugify(idea)


def stage_plan(idea, constraints):
    msgs = [
        {"role": "system", "content": "Kamu ketua proyek. Buat rencana yang ringkas dan realistis. Jawab dalam bahasa Indonesia."},
        {"role": "user", "content": f"Ide proyek:\n{idea}\n\nBatasan teknis:\n{constraints}\n{FORMAT_PLAN}\nMaksimal 350 kata."},
    ]
    return ask("plan", msgs, 1500, "plan")


def stage_expand(plan, idea, constraints):
    msgs = [
        {"role": "system", "content": "Kamu reviewer rencana. Jawab dalam bahasa Indonesia."},
        {"role": "user", "content": (
            f"Ide asli:\n{idea}\n\nBatasan teknis:\n{constraints}\n\nRencana saat ini:\n{plan}\n\n"
            "Tugas: tambahkan MAKSIMAL 5 fitur penting yang terlewat (jangan melebar dari ide asli), "
            "perbaiki struktur file kalau perlu, dan tambahkan butir checklist untuk fitur baru. "
            "Pertahankan bagian Nama proyek (ganti kalau ada nama yang lebih cocok). "
            "Kembalikan rencana LENGKAP yang sudah diperbarui dengan format yang sama. Maksimal 450 kata."
        )},
    ]
    return ask("expand", msgs, 2000, "expand")


CODE_FORMAT = """\
Tulis SEMUA file proyek dengan format persis ini, tanpa teks lain di luar blok:

=== FILE: path/relatif/file.js ===
(isi file, tanpa pagar ```)
=== END ===

Aturan: path relatif, jangan pakai ".." dan jangan path absolut. Jangan tulis file rahasia asli (.env), cukup .env.example.
"""


def stage_code(plan, constraints, watcher=None):
    msgs = [
        {"role": "system", "content": "Kamu programmer senior. Tulis kode yang jalan, lengkap, dan sederhana."},
        {"role": "user", "content": f"Rencana proyek:\n{plan}\n\nBatasan teknis:\n{constraints}{skill_block('Skill yang WAJIB diikuti (aturan kualitas):')}\n{CODE_FORMAT}"},
    ]
    text = ask("code", msgs, CODE_MAX_TOKENS, "code", on_text=watcher.feed if watcher else None)
    if watcher:
        watcher.finish(text)
    return text


def stage_review(plan, files, check_problems):
    if ROLES["review"] == "skip":
        return {"results": [], "issues": [], "summary": "Pemeriksaan model dilewati sesuai prompt (hanya cek sintaks otomatis)."}
    code_dump = "\n".join(f"=== {p} ===\n{c}" for p, c in files.items())
    msgs = [
        {"role": "system", "content": "Kamu reviewer kode yang teliti. Jawab HANYA dengan JSON valid."},
        {"role": "user", "content": (
            f"Rencana + checklist:\n{plan}{skill_block('Skill (tiap aturan yang dilanggar kode = satu issue nyata, sebut file dan baris/bagiannya):')}\nKode:\n{code_dump}\n\n"
            f"Hasil cek otomatis (sintaks):\n{json.dumps(check_problems, ensure_ascii=False) if check_problems else 'semua lolos'}\n\n"
            "Periksa tiap butir checklist terhadap kode. Jawab JSON persis seperti ini:\n"
            '{"results":[{"id":"C1","pass":true,"note":"singkat"}],'
            '"issues":[{"file":"path","problem":"apa yang salah dan cara memperbaiki"}],'
            '"summary":"2-3 kalimat bahasa biasa: kode ini ngapain dan cara menjalankannya"}\n'
            "Hanya masukkan ke issues masalah nyata (bug, fitur checklist yang belum ada, dependensi hilang). "
            "Jangan menilai gaya penulisan."
        )},
    ]
    raw = ask("review", msgs, 2500, "review")
    data = extract_json(raw)
    if data is None:
        return {"results": [], "issues": [{"file": "-", "problem": "Review tidak menghasilkan JSON valid"}], "summary": raw[:300]}
    return data


def stage_fix(plan, files, issues, watcher=None):
    names = {i.get("file") for i in issues}
    relevant = {p: c for p, c in files.items() if p in names} or files
    code_dump = "\n".join(f"=== {p} ===\n{c}" for p, c in relevant.items())
    msgs = [
        {"role": "system", "content": "Kamu programmer senior. Perbaiki hanya yang disebut."},
        {"role": "user", "content": (
            f"Rencana:\n{plan}{skill_block('Skill yang WAJIB diikuti:')}\nMasalah yang harus diperbaiki:\n{json.dumps(issues, ensure_ascii=False, indent=1)}\n\n"
            f"File terkait saat ini:\n{code_dump}\n\n"
            "Kirim HANYA file yang berubah (isi lengkap tiap file), format:\n" + CODE_FORMAT
        )},
    ]
    text = ask("fix", msgs, 8000, "fix", on_text=watcher.feed if watcher else None)
    if watcher:
        watcher.finish(text)
    return text


# ---------- Alur utama ----------
def run(idea, name, rounds, approve, constraints, no_sonnet=False, resume=False):
    outdir = HERE / "projects" / name
    if resume:
        if not (outdir / "PROJECT.md").exists():
            sys.exit(f"Tidak bisa lanjut: {outdir / 'PROJECT.md'} tidak ada.")
        if not idea:
            ip = outdir / "IDEA.txt"
            if not ip.exists():
                sys.exit("IDEA.txt tidak ada di proyek ini. Kirim ide aslinya sebagai argumen: "
                         f'python3 pipelinenew.py "ide asli" --resume {name}')
            idea = ip.read_text(encoding="utf-8").strip()
    outdir.mkdir(parents=True, exist_ok=True)
    (outdir / "IDEA.txt").write_text(idea + "\n", encoding="utf-8")  # supaya --resume tahu ide aslinya
    dp = outdir / "DECISIONS.md"
    if resume and dp.exists():
        decisions = [dp.read_text(encoding="utf-8").rstrip() + "\n", "- Dilanjutkan dengan --resume.\n"]
    else:
        decisions = [f"# DECISIONS {name}\n", f"- Ide: {idea}\n"]
        if resume:
            decisions.append("- Dilanjutkan dengan --resume.\n")

    ev("hello")
    roles, banned = resolve_roles(idea, no_sonnet)
    ROLES.clear()
    ROLES.update(roles)
    print("    alur (sesuai prompt): " + " -> ".join(f"{who(r)} ({t})" for r, t in
          [("plan", "rencana"), ("expand", "tambah fitur"), ("code", "kode"), ("review", "periksa"), ("fix", "perbaiki")]
          if ROLES[r] != "skip") + (f" | dilarang: {', '.join(banned)}" if banned else ""), flush=True)
    names = load_skills(idea)
    if names:
        ev("start", id="skills", text="Memuat skill: " + ", ".join(names))
        print(f"    skill dimuat: {', '.join(names)}", flush=True)
        ev("done", id="skills")
    for m in SKILL_MISSING:
        print(f"    skill tidak ditemukan: {m}", flush=True)
        ev("note", text=f'Skill "{m}" tidak ditemukan')
    for m in SKILL_SKIPPED:
        print(f"    skill dilewati (melebihi {SKILLS_MAX_CHARS} karakter): {m}", flush=True)
        ev("note", text=f'Skill "{m}" dilewati: total melebihi {SKILLS_MAX_CHARS} karakter (naikkan SKILLS_MAX_CHARS)')
    if resume:
        plan = (outdir / "PROJECT.md").read_text(encoding="utf-8")
        print("1-2/5 Dilewati (--resume): memakai PROJECT.md yang sudah ada.", flush=True)
        ev("note", text="Lanjut dari PROJECT.md, tahap rencana dan tambah fitur dilewati")
    else:
        print(f"1/5 Rencana ({who('plan')})...", flush=True)
        ev("start", id="plan", text="Menyusun rencana", who=who("plan"))
        plan = stage_plan(idea, constraints)
        ev("done", id="plan")
        decisions.append(f"- Rencana awal dibuat oleh ketua ({who('plan')}).\n")

        if ROLES["expand"] != "skip":
            print(f"2/5 Tambah fitur terlewat ({who('expand')})...", flush=True)
            ev("start", id="expand", text="Menambah fitur yang terlewat", who=who("expand"))
            plan = stage_expand(plan, idea, constraints)
            ev("done", id="expand")
            decisions.append(f"- {who('expand')} menambah fitur yang terlewat dan memperbarui checklist.\n")
        (outdir / "PROJECT.md").write_text(plan, encoding="utf-8")
        suggested = extract_name(plan, idea)
        (outdir / "NAME.txt").write_text(suggested + "\n", encoding="utf-8")
        print(f"NAMA_SARAN: {suggested}", flush=True)

    if approve:
        print("\n" + plan + "\n")
        if input("Lanjut tulis kode? (ya/tidak): ").strip().lower() not in {"ya", "y", "lanjut"}:
            print("Dihentikan. Rencana tersimpan di", outdir / "PROJECT.md")
            return

    L = who("code")
    if resume and read_all(outdir):
        print("3/5 Kode sudah ada, lanjut ke pemeriksaan.", flush=True)
        ev("note", text="File kode sudah ada, langsung ke pemeriksaan")
    else:
        print(f"3/5 Tulis kode ({L})...", flush=True)
        ev("start", id="code", text="Menulis kode", who=L)
        raw = stage_code(plan, constraints, FileWatcher(outdir, L, parent="code"))
        (HERE / "last_code_raw.txt").write_text(raw, encoding="utf-8")  # untuk debug kalau format meleset
        files = parse_files(raw)
        if not files:
            ev("fail", id="code", text=f"{L} tidak mengeluarkan file yang benar")
            sys.exit(f"{L} tidak mengeluarkan file dengan format yang benar. Lihat last_code_raw.txt")
        write_files(outdir, files)
        decisions.append(f"- {L} menulis {len(files)} file: {', '.join(files)}.\n")

    review = {}
    ok = False
    for r in range(1, rounds + 1):
        print(f"4/5 Cek otomatis + periksa (putaran {r}/{rounds})...", flush=True)
        current = read_all(outdir)
        ev("start", id=f"chk{r}", text=f"Cek sintaks otomatis (putaran {r}/{rounds})", who="tanpa model")
        problems = auto_checks(outdir)
        ev("done", id=f"chk{r}", text=f"Cek sintaks otomatis (putaran {r}): " + (f"{len(problems)} masalah" if problems else "lolos"))
        ev("start", id=f"rev{r}", text=f"Memeriksa kode terhadap checklist (putaran {r})", who=who("review") if ROLES["review"] != "skip" else "tanpa model")
        review = stage_review(plan, current, problems)
        issues = (review.get("issues") or []) + [p for p in problems if p not in (review.get("issues") or [])]
        failed = [x for x in review.get("results", []) if not x.get("pass", True)]
        ev("done", id=f"rev{r}", text=f"Pemeriksaan putaran {r}: " + (
            "lolos semua" if not issues and not failed else f"{len(issues)} masalah, {len(failed)} butir gagal"))
        if not issues and not failed:
            ok = True
            decisions.append(f"- Putaran {r}: lolos semua checklist.\n")
            break
        decisions.append(f"- Putaran {r}: {len(issues)} masalah, {len(failed)} butir checklist gagal -> diperbaiki {who('fix')}.\n")
        if r == rounds or ROLES["fix"] == "skip":
            break
        FX = who("fix")
        print(f"    perbaikan ({FX})...", flush=True)
        for f in failed:
            issues.append({"file": "-", "problem": f"Checklist {f.get('id')} gagal: {f.get('note', '')}"})
        ev("start", id=f"fix{r}", text=f"Memperbaiki {len(issues)} masalah", who=FX)
        fixed = parse_files(stage_fix(plan, current, issues, FileWatcher(outdir, FX, parent=f"fix{r}")))
        if fixed:
            write_files(outdir, fixed)
            decisions.append(f"- Putaran {r}: {FX} mengubah {', '.join(fixed)}.\n")
        else:
            ev("note", text=f"{FX} tidak mengirim file perbaikan")

    status = "LOLOS" if ok else "ADA SISA MASALAH (lihat REVIEW.md)"
    rv = ["# REVIEW\n", f"Status: {status}\n\n", "## Ringkasan\n", f"{review.get('summary', '-')}\n\n", "## Checklist\n"]
    for x in review.get("results", []):
        rv.append(f"- {x.get('id')}: {'lulus' if x.get('pass') else 'GAGAL'} - {x.get('note', '')}\n")
    if review.get("issues"):
        rv.append("\n## Masalah tersisa\n")
        for i in review["issues"]:
            rv.append(f"- {i.get('file')}: {i.get('problem')}\n")
    ev("start", id="docs", text="Menyimpan REVIEW.md dan DECISIONS.md")
    (outdir / "REVIEW.md").write_text("".join(rv), encoding="utf-8")
    (outdir / "DECISIONS.md").write_text("".join(decisions), encoding="utf-8")
    ev("done", id="docs")

    print("5/5 Selesai.")
    print(f"    Status : {status}")
    print(f"    Folder : {outdir}")
    print(f"    Panggilan model: {TOTAL['calls']}, perkiraan biaya: Rp{TOTAL['cost']:,.0f}")
    print(f"    Ringkasan: {review.get('summary', '-')}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("idea", nargs="?", default="", help="deskripsi proyek (boleh kosong kalau --resume dan IDEA.txt ada)")
    ap.add_argument("--resume", default=None, metavar="NAMA", help="lanjutkan proyek di projects/NAMA dari PROJECT.md")
    ap.add_argument("--name", default=None, help="nama folder proyek")
    ap.add_argument("--rounds", type=int, default=3, help="maks putaran periksa-perbaiki")
    ap.add_argument("--approve", action="store_true", help="minta persetujuan sebelum Sonnet nulis kode")
    ap.add_argument("--no-sonnet", action="store_true", help="jangan pakai Sonnet (prompt tetap lebih utama)")
    ap.add_argument("--constraints", default=None, help="path file batasan teknis (default: constraints.md atau bawaan)")
    a = ap.parse_args()

    if not a.idea and not a.resume:
        ap.error("isi ide proyek, atau pakai --resume NAMA")
    name = a.resume or a.name or re.sub(r"[^a-z0-9]+", "-", a.idea.lower())[:30].strip("-") or "proyek"
    cpath = pathlib.Path(a.constraints) if a.constraints else HERE / "constraints.md"
    constraints = cpath.read_text(encoding="utf-8") if cpath.exists() else DEFAULT_CONSTRAINTS
    run(a.idea, name, a.rounds, a.approve, constraints, a.no_sonnet, resume=bool(a.resume))


if __name__ == "__main__":
    main()
