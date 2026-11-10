#!/usr/bin/env python3
"""
Pipeline multi-model: Gemini (rencana) -> DeepSeek (tambah fitur) -> Sonnet (kode)
-> cek otomatis -> DeepSeek (periksa) -> hasil dikirim ke kamu.

Pakai:
  export VIKEY_API_KEY="..."
  python3 pipeline.py "bot WA yang ..." --name wa-bot
  python3 pipeline.py "..." --name x --rounds 3 --approve

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

TOTAL = {"cost": 0.0, "calls": 0}

# ---------- Skill: aturan tambahan dari folder skills/*.md ----------
SKILLS_DIR = HERE / "skills"
SKILLS_MAX_CHARS = int(os.getenv("SKILLS_MAX_CHARS", "16000"))  # jaga biaya token
SKILL_NAMES = []  # nama skill yang dimuat
SKILL_MISSING = []  # nama skill yang diminta tapi filenya tidak ada
SKILL_SKIPPED = []  # nama skill yang dilewati karena melebihi SKILLS_MAX_CHARS
_FRONT = re.compile(r"\A---\s*\n.*?\n---\s*\n", re.DOTALL)  # front matter (name/description) dibuang
SKILL_TEXT = ""  # teks gabungan yang ditempel ke prompt


def load_skills():
    """Baca skills/*.md (atau hanya yang disebut di env SKILLS=nama1,nama2). Return nama-nama yang dimuat."""
    global SKILL_TEXT
    SKILL_NAMES.clear()
    SKILL_MISSING.clear()
    SKILL_SKIPPED.clear()
    SKILL_TEXT = ""
    want = [s.strip().lower() for s in os.getenv("SKILLS", "").split(",") if s.strip()]
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


def parse_files(text):
    files = {}
    for path, content in FILE_RE.findall(text):
        path = path.strip().strip("`")
        p = pathlib.PurePosixPath(path)
        if p.is_absolute() or ".." in p.parts:
            continue
        lines = content.split("\n")
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        files[str(p)] = "\n".join(lines) + "\n"
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
    return planner_chat(msgs, 1500, "plan")


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
    return vikey_chat(M_DEEPSEEK, msgs, 2000, "expand")


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
    text = vikey_chat(M_SONNET, msgs, 12000, "code", on_text=watcher.feed if watcher else None)
    if watcher:
        watcher.finish(text)
    return text


def stage_review(plan, files, check_problems):
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
    raw = vikey_chat(M_DEEPSEEK, msgs, 2500, "review")
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
    text = vikey_chat(M_SONNET, msgs, 8000, "fix", on_text=watcher.feed if watcher else None)
    if watcher:
        watcher.finish(text)
    return text


# ---------- Alur utama ----------
def run(idea, name, rounds, approve, constraints):
    outdir = HERE / "projects" / name
    outdir.mkdir(parents=True, exist_ok=True)
    decisions = [f"# DECISIONS {name}\n", f"- Ide: {idea}\n"]

    ev("hello")
    names = load_skills()
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
    print("1/5 Rencana (Gemini)...", flush=True)
    ev("start", id="plan", text="Menyusun rencana", who="Gemini")
    plan = stage_plan(idea, constraints)
    ev("done", id="plan")
    decisions.append("- Rencana awal dibuat oleh ketua (Gemini).\n")

    print("2/5 Tambah fitur terlewat (DeepSeek)...", flush=True)
    ev("start", id="expand", text="Menambah fitur yang terlewat", who="DeepSeek")
    plan = stage_expand(plan, idea, constraints)
    ev("done", id="expand")
    decisions.append("- DeepSeek menambah fitur yang terlewat dan memperbarui checklist.\n")
    (outdir / "PROJECT.md").write_text(plan, encoding="utf-8")
    suggested = extract_name(plan, idea)
    (outdir / "NAME.txt").write_text(suggested + "\n", encoding="utf-8")
    print(f"NAMA_SARAN: {suggested}", flush=True)

    if approve:
        print("\n" + plan + "\n")
        if input("Lanjut tulis kode? (ya/tidak): ").strip().lower() not in {"ya", "y", "lanjut"}:
            print("Dihentikan. Rencana tersimpan di", outdir / "PROJECT.md")
            return

    print("3/5 Tulis kode (Sonnet)...", flush=True)
    ev("start", id="code", text="Menulis kode", who="Sonnet")
    files = parse_files(stage_code(plan, constraints, FileWatcher(outdir, "Sonnet", parent="code")))
    if not files:
        ev("fail", id="code", text="Sonnet tidak mengeluarkan file yang benar")
        sys.exit("Sonnet tidak mengeluarkan file dengan format yang benar.")
    write_files(outdir, files)
    decisions.append(f"- Sonnet menulis {len(files)} file: {', '.join(files)}.\n")

    review = {}
    ok = False
    for r in range(1, rounds + 1):
        print(f"4/5 Cek otomatis + periksa (putaran {r}/{rounds})...", flush=True)
        current = read_all(outdir)
        ev("start", id=f"chk{r}", text=f"Cek sintaks otomatis (putaran {r}/{rounds})", who="tanpa model")
        problems = auto_checks(outdir)
        ev("done", id=f"chk{r}", text=f"Cek sintaks otomatis (putaran {r}): " + (f"{len(problems)} masalah" if problems else "lolos"))
        ev("start", id=f"rev{r}", text=f"Memeriksa kode terhadap checklist (putaran {r})", who="DeepSeek")
        review = stage_review(plan, current, problems)
        issues = (review.get("issues") or []) + [p for p in problems if p not in (review.get("issues") or [])]
        failed = [x for x in review.get("results", []) if not x.get("pass", True)]
        ev("done", id=f"rev{r}", text=f"Pemeriksaan putaran {r}: " + (
            "lolos semua" if not issues and not failed else f"{len(issues)} masalah, {len(failed)} butir gagal"))
        if not issues and not failed:
            ok = True
            decisions.append(f"- Putaran {r}: lolos semua checklist.\n")
            break
        decisions.append(f"- Putaran {r}: {len(issues)} masalah, {len(failed)} butir checklist gagal -> diperbaiki Sonnet.\n")
        if r == rounds:
            break
        print("    perbaikan (Sonnet)...", flush=True)
        for f in failed:
            issues.append({"file": "-", "problem": f"Checklist {f.get('id')} gagal: {f.get('note', '')}"})
        ev("start", id=f"fix{r}", text=f"Memperbaiki {len(issues)} masalah", who="Sonnet")
        fixed = parse_files(stage_fix(plan, current, issues, FileWatcher(outdir, "Sonnet", parent=f"fix{r}")))
        if fixed:
            write_files(outdir, fixed)
            decisions.append(f"- Putaran {r}: Sonnet mengubah {', '.join(fixed)}.\n")
        else:
            ev("note", text="Sonnet tidak mengirim file perbaikan")

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
    ap.add_argument("idea", help="deskripsi proyek")
    ap.add_argument("--name", default=None, help="nama folder proyek")
    ap.add_argument("--rounds", type=int, default=3, help="maks putaran periksa-perbaiki")
    ap.add_argument("--approve", action="store_true", help="minta persetujuan sebelum Sonnet nulis kode")
    ap.add_argument("--constraints", default=None, help="path file batasan teknis (default: constraints.md atau bawaan)")
    a = ap.parse_args()

    name = a.name or re.sub(r"[^a-z0-9]+", "-", a.idea.lower())[:30].strip("-") or "proyek"
    cpath = pathlib.Path(a.constraints) if a.constraints else HERE / "constraints.md"
    constraints = cpath.read_text(encoding="utf-8") if cpath.exists() else DEFAULT_CONSTRAINTS
    run(a.idea, name, a.rounds, a.approve, constraints)


if __name__ == "__main__":
    main()
