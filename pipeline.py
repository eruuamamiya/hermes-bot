#!/usr/bin/env python3
"""
Pipeline multi-model: rencana -> tambah fitur -> kode (bertahap) -> cek otomatis -> periksa -> perbaiki.

pipeline.py TIDAK tahu nama model apa pun. Semua model dibaca saat runtime dari models.json.
Nama model hanya datang dari:
  1. prompt pengguna, format  tugas=nama_model   (mis. html=model-a, plan=model-b)
  2. env HERMES_DEFAULT_MODEL (untuk tugas yang tidak disebut di prompt)
Nama itu lalu di-lookup ke models.json["models"]; kalau tidak ada -> error + daftar model valid.
Pengecualian: sentinel "hermes" = binary Hermes via subprocess (env HERMES_BIN, HERMES_TIMEOUT_MIN,
HERMES_CONTEXT), tidak dicari di models.json.

Tugas: plan, expand, code, review, fix, html, css, node, lainnya
  - "fix" tanpa penugasan ikut model "code".
  - html/css/node/lainnya tanpa penugasan ikut model "code".
  - tugas=skip melewati tahap expand/review/fix (plan dan code tidak bisa dilewati).
  - "tanpa preview", "skip fix", dst. di prompt juga melewati tahap itu.

Pakai:
  export HERMES_DEFAULT_MODEL="<key di models.json>"
  python3 pipeline.py "bot WA yang ..., plan=<model>, html=<model>, nama: wa-bot"
  python3 pipeline.py "..." --name x --rounds 3 --approve
  python3 pipeline.py --resume nama-proyek
  python3 pipeline.py "ide asli" --resume nama-proyek
  python3 pipeline.py "..." --group-model "html=<model>,node=<model>"   # prompt tetap lebih utama

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

# ---------- Konfigurasi umum (tidak ada nama model di sini) ----------
POOL_DAILY_LIMIT = int(os.getenv("POOL_DAILY_LIMIT", "480"))  # batas aman per key per hari
POOL_COOLDOWN = 60  # detik istirahat key setelah kena 429

DEFAULT_CONSTRAINTS = """\
- Proyek dijalankan di STB HG680P (Armbian, ARM64, RAM 2GB) lewat pm2.
- Bahasa: Node.js (utamakan) atau Python. Hindari dependensi native yang berat.
- Semua rahasia (token, API key) lewat file .env, sediakan .env.example.
- Sertakan package.json atau requirements.txt yang lengkap dan README singkat cara jalan.
- Kode sederhana, mudah dibaca, dengan komentar singkat di bagian penting.
"""

HTTP_TIMEOUT = int(os.getenv("HTTP_TIMEOUT", "900"))  # batas diam per koneksi (detik)
CODE_MAX_TOKENS = int(os.getenv("CODE_MAX_TOKENS", "6000"))
try:  # parameter tambahan global ke semua model; per model bisa lewat "extra_body" di models.json
    EXTRA_BODY = json.loads(os.getenv("HERMES_EXTRA_BODY", "") or "{}")
except Exception:
    EXTRA_BODY = {}
GROUP_MAX_FILES = int(os.getenv("GROUP_MAX_FILES", "5"))

REGISTRY = {}  # isi models.json["models"], diisi di run()
ROLES = {}  # tugas -> nama model (string) atau "skip", diisi di run()
GROUP_MODELS = {}  # dari --group-model: {"html": "<nama>"}
PROMPT_GROUP_MODELS = {}  # dari prompt: {"html": "<nama>", ...}
EXPLICIT = set()  # tugas yang ditentukan prompt (bukan default)

TOTAL = {"cost": 0.0, "calls": 0}

# ---------- Skill: aturan tambahan dari folder skills/*.md ----------
SKILLS_DIR = HERE / "skills"
SKILLS_MAX_CHARS = int(os.getenv("SKILLS_MAX_CHARS", "40000"))
SKILL_NAMES = []
SKILL_MISSING = []
SKILL_SKIPPED = []
_FRONT = re.compile(r"\A---\s*\n.*?\n---\s*\n", re.DOTALL)
SKILL_TEXT = ""


def _norm(s):
    return re.sub(r"[^a-z0-9]", "", s.lower())


def skills_from_prompt(idea):
    """Skill yang disebut di prompt; tanda strip/spasi diabaikan ('antislop' = anti-slop)."""
    if not SKILLS_DIR.is_dir():
        return []
    toks = {_norm(t) for t in re.findall(r"[\w-]+", idea)}
    return [fp.stem.lower() for fp in sorted(SKILLS_DIR.glob("*.md")) if _norm(fp.stem) in toks]


def load_skills(idea=""):
    """Baca skills/*.md. Prioritas: env SKILLS=a,b > skill yang disebut di prompt > semua."""
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


# =====================================================================
# RESOLVER MODEL (generik; tidak mengandung nama model apa pun)
# =====================================================================
def load_registry():
    """Baca models.json -> dict models. Lokasi: env HERMES_MODELS_FILE atau models.json di samping pipeline.py."""
    path = pathlib.Path(os.getenv("HERMES_MODELS_FILE") or (HERE / "models.json"))
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        sys.exit(f"models.json tidak ditemukan di {path}.")
    except json.JSONDecodeError as e:
        sys.exit(f"models.json tidak valid: {e}")
    models = data.get("models") if isinstance(data, dict) else None
    if not isinstance(models, dict) or not models:
        sys.exit(f'{path} harus berisi objek "models" yang tidak kosong.')
    return models


def resolve_model_id(task, prompt_assignment, registry):
    """Nama model (string mentah) untuk satu tugas: penugasan di prompt, kalau tidak ada -> HERMES_DEFAULT_MODEL.
    Hanya memilih NAMA; validasi isinya lewat get_model_config()."""
    name = (prompt_assignment or "").strip()
    if name:
        return name
    name = os.getenv("HERMES_DEFAULT_MODEL", "").strip()
    if name:
        return name
    sys.exit(f"Tugas '{task}' tidak punya model: tulis {task}=<model> di prompt atau set HERMES_DEFAULT_MODEL. "
             f"Model valid: {', '.join(registry)}.")


def get_model_config(model_id, registry):
    """Entry models.json untuk model_id. Sentinel "hermes" -> marker subprocess (tidak dicari di models.json).
    Tidak ada -> error dengan daftar model valid."""
    if model_id == "hermes":
        return {"type": "hermes"}
    cfg = registry.get(model_id)
    if not isinstance(cfg, dict):
        sys.exit(f"Model '{model_id}' tidak ditemukan di models.json. "
                 f"Model valid: hermes, {', '.join(registry)}.")
    return cfg


# ---------- Penugasan model lewat PROMPT: "tugas=nama_model" ----------
TASK_ALIASES = {
    "plan": "plan", "planning": "plan", "expand": "expand",
    "code": "code", "coding": "code",
    "review": "review", "preview": "review", "fix": "fix",
    "html": "html", "css": "css", "node": "node", "nodejs": "node", "lainnya": "lainnya",
}
ASSIGN_RE = re.compile(
    r"\b(planning|plan|expand|coding|code|preview|review|fix|html|css|nodejs|node|lainnya)\s*=\s*([^\s,;]+)",
    re.IGNORECASE)
SKIP_RE = re.compile(
    r"\b(?:tanpa|skip|lewati|lewatin|jangan|gak\s+usah|gausah|ga\s+usah|no|without)\s+(?:tahap\s+)?"
    r"(preview|review|periksa|fix\s*bug|fix|debug|perbaiki|expand|tambah\s*fitur)\b", re.IGNORECASE)
NAME_RE = re.compile(
    r"\b(?:nama(?:\s*(?:proyek|project|folder))?(?:nya)?\s*[:=]\s*"
    r"|nama\s*(?:proyek|project|folder)(?:nya)?\s+|namai\s+|dinamai\s+|name\s*[:=]\s*)"
    r"[\"'`]?([A-Za-z0-9][A-Za-z0-9_-]{1,40})", re.IGNORECASE)
KIND_LABEL = {"html": "html", "css": "css", "node": "node js", "lainnya": "lainnya"}
GROUP_TASKS = ("html", "css", "node", "lainnya")


def parse_name(idea):
    """Nama proyek dari prompt: 'nama: porto-huma' / 'nama proyek porto-huma' / 'namai porto-huma'."""
    m = NAME_RE.search(idea or "")
    return re.sub(r"[^a-z0-9]+", "-", m.group(1).lower()).strip("-") if m else None


def parse_prompt(idea):
    """Return (assign {tugas: nama_mentah}, skips {tugas}, teks_bersih tanpa token tugas=model).
    Nama model TIDAK divalidasi di sini."""
    assign, skips = {}, set()

    def _take(m):
        task = TASK_ALIASES[m.group(1).lower()]
        val = m.group(2).strip().strip("\"'`()").rstrip(".,;:")
        if val.lower() == "skip":
            skips.add(task)
        elif val:
            assign[task] = val
        return " "

    clean = ASSIGN_RE.sub(_take, idea or "")
    for m in SKIP_RE.finditer(clean):
        w = re.sub(r"\s+", "", m.group(1).lower())
        skips.add("review" if w in ("preview", "review", "periksa") else
                  "fix" if w.startswith(("fix", "debug", "perbaiki")) else "expand")
    return assign, skips, re.sub(r"[ \t]{2,}", " ", clean).strip()


def resolve_roles(idea):
    """Resolve model tiap tugas ke models.json (fail-fast sebelum ada panggilan model).
    Mengisi REGISTRY, ROLES, EXPLICIT, PROMPT_GROUP_MODELS. Return teks prompt bersih."""
    REGISTRY.clear()
    REGISTRY.update(load_registry())
    assign, skips, clean = parse_prompt(idea)
    ROLES.clear()
    EXPLICIT.clear()
    PROMPT_GROUP_MODELS.clear()

    for task in ("plan", "expand", "code", "review"):
        if task in ("expand", "review") and task in skips:
            ROLES[task] = "skip"
            EXPLICIT.add(task)
            continue
        mid = resolve_model_id(task, assign.get(task), REGISTRY)
        get_model_config(mid, REGISTRY)
        ROLES[task] = mid
        if task in assign:
            EXPLICIT.add(task)

    if "fix" in skips:
        ROLES["fix"] = "skip"
        EXPLICIT.add("fix")
    elif "fix" in assign:
        get_model_config(assign["fix"], REGISTRY)
        ROLES["fix"] = assign["fix"]
        EXPLICIT.add("fix")
    else:
        ROLES["fix"] = ROLES["code"]  # fix ikut model code pada run yang sama

    for k in GROUP_TASKS:
        if k in assign:
            get_model_config(assign[k], REGISTRY)
            PROMPT_GROUP_MODELS[k] = assign[k]
    for k, v in GROUP_MODELS.items():
        get_model_config(v, REGISTRY)
    return clean


def who(role):
    return ROLES[role]


# =====================================================================
# Panggilan model (semua parameter dari entry models.json)
# =====================================================================
EVENTS = os.getenv("PIPELINE_EVENTS", "") == "1"


def ev(kind, **kw):
    """kind: start, done, fail, note. Dicetak sebagai '@@{json}' kalau PIPELINE_EVENTS=1."""
    if EVENTS:
        print("@@" + json.dumps({"e": kind, **kw}, ensure_ascii=False), flush=True)


def log_usage(model_id, cfg, usage, tag=""):
    p = usage.get("prompt_tokens", 0) or 0
    c = usage.get("completion_tokens", 0) or 0
    # harga opsional di models.json: "price_in" / "price_out" (Rp per 1M token); default 0 (gratis)
    cost = p * float(cfg.get("price_in", 0)) / 1e6 + c * float(cfg.get("price_out", 0)) / 1e6
    TOTAL["cost"] += cost
    TOTAL["calls"] += 1
    rec = {"t": datetime.datetime.now().isoformat(timespec="seconds"),
           "model": model_id, "tag": tag, "in": p, "out": c, "rp": round(cost, 2)}
    with open(HERE / "usage.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(rec) + "\n")
    print(f"    [{model_id}] in={p} out={c} ~Rp{cost:,.0f}", flush=True)


def _headers(key):
    h = {"Content-Type": "application/json"}
    if key:
        h["Authorization"] = "Bearer " + key
    return h


def http_chat(base, key, model, messages, max_tokens, temperature=0.3, timeout=HTTP_TIMEOUT, extra=None):
    body = json.dumps({"model": model, "messages": messages, "max_tokens": max_tokens,
                       "temperature": temperature, **(extra or {})}).encode()
    req = urllib.request.Request(base.rstrip("/") + "/chat/completions", data=body, headers=_headers(key))
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def http_chat_stream(base, key, model, messages, max_tokens, on_text, temperature=0.3, timeout=HTTP_TIMEOUT, extra=None):
    """Stream; tiap potongan teks diteruskan ke on_text. Return (teks, usage)."""
    body = json.dumps({"model": model, "messages": messages, "max_tokens": max_tokens,
                       "temperature": temperature, "stream": True,
                       "stream_options": {"include_usage": True}, **(extra or {})}).encode()
    req = urllib.request.Request(base.rstrip("/") + "/chat/completions", data=body, headers=_headers(key))
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


def _api_key(model_id, cfg):
    env_name = cfg.get("api_key_env")
    if not env_name:
        return ""
    key = os.getenv(env_name, "")
    if not key:
        sys.exit(f"Env {env_name} belum di-set (dibutuhkan model '{model_id}').")
    return key


def _entry_extra(cfg):
    extra = dict(EXTRA_BODY)
    if isinstance(cfg.get("extra_body"), dict):
        extra.update(cfg["extra_body"])
    return extra


def openai_chat(model_id, cfg, messages, max_tokens=3000, tag="", on_text=None):
    """Panggilan OpenAI-compatible biasa (stream kalau on_text ada). Semua dari entry models.json."""
    base, model, key, extra = cfg["base_url"], cfg["model"], _api_key(model_id, cfg), _entry_extra(cfg)
    last_err = None
    if on_text:
        try:
            text, usage = http_chat_stream(base, key, model, messages, max_tokens, on_text, extra=extra)
            if text:
                if not usage:  # perkirakan kasar (4 karakter ~ 1 token)
                    usage = {"prompt_tokens": sum(len(m["content"]) for m in messages) // 4,
                             "completion_tokens": len(text) // 4}
                log_usage(model_id, cfg, usage, tag)
                return text
        except Exception as e:
            print(f"    stream gagal ({e!r}), pakai panggilan biasa", flush=True)
    for attempt in range(3):
        try:
            data = http_chat(base, key, model, messages, max_tokens, extra=extra)
            log_usage(model_id, cfg, data.get("usage", {}), tag)
            content = data["choices"][0]["message"].get("content")
            if not (content or "").strip():
                last_err = "jawaban kosong (token habis untuk reasoning?)"
                max_tokens = min(max_tokens * 2, 32000)
                print(f"    {last_err}, ulang dengan max_tokens={max_tokens}", flush=True)
                continue
            return content
        except urllib.error.HTTPError as e:
            last_err = f"HTTP {e.code}: {e.read()[:200]!r}"
            if e.code in (429, 500, 502, 503, 504):
                time.sleep(3 * (attempt + 1))
                continue
            break
        except Exception as e:
            last_err = repr(e)
            time.sleep(3 * (attempt + 1))
    raise RuntimeError(f"Panggilan {model_id} gagal: {last_err}")


# ---------- Rotasi key (untuk entry dengan use_gemini_pool: true) ----------
class KeyPool:
    def __init__(self, pool_key):
        keys = []
        kp = HERE / "keys.json"
        if kp.exists():
            keys += json.loads(kp.read_text()).get(pool_key, [])
        keys += [k.strip() for k in os.getenv("POOL_API_KEYS", "").split(",") if k.strip()]
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
            if e["count"] < POOL_DAILY_LIMIT and e["cool"] <= now:
                out.append((e["count"], k))
        out.sort()  # paling sedikit terpakai dulu
        return [k for _, k in out]

    def chat(self, model_id, cfg, messages, max_tokens=3000, tag=""):
        if not self.keys:
            raise RuntimeError(f"Tidak ada key pool untuk model '{model_id}' (keys.json atau env POOL_API_KEYS).")
        for key in self.available():
            e = self._entry(key)
            try:
                data = http_chat(cfg["base_url"], key, cfg["model"], messages, max_tokens, extra=_entry_extra(cfg))
                e["count"] += 1
                self._save()
                log_usage(model_id, cfg, data.get("usage", {}), tag)
                return data["choices"][0]["message"]["content"]
            except urllib.error.HTTPError as e2:
                if e2.code == 429:
                    e["cool"] = time.time() + POOL_COOLDOWN
                    if b"per day" in (e2.read() or b"").lower():
                        e["count"] = POOL_DAILY_LIMIT
                    self._save()
                    continue
                print(f"    {model_id} error HTTP {e2.code}, coba key lain", flush=True)
            except Exception as ex:
                print(f"    {model_id} error {ex!r}, coba key lain", flush=True)
        raise RuntimeError(f"Semua key pool untuk '{model_id}' habis/error")


_POOLS = {}


def get_pool(cfg):
    pk = cfg.get("pool_key", "pool")  # nama daftar key di keys.json
    if pk not in _POOLS:
        _POOLS[pk] = KeyPool(pk)
    return _POOLS[pk]


def hermes_chat(model_id, msgs, tag):
    """Default model: panggil Hermes binary via subprocess, bukan HTTP."""
    bin_path = os.getenv("HERMES_BIN", "hermes")
    timeout = int(os.getenv("HERMES_TIMEOUT_MIN", "10")) * 60
    ctx = os.getenv("HERMES_CONTEXT", "").strip()
    # gabung system + user jadi satu prompt
    body = "\n\n".join(m["content"] for m in msgs)
    prompt = f"{ctx}\n\n{body}" if ctx else body
    try:
        r = subprocess.run([bin_path, prompt], capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        raise RuntimeError(f"HERMES_BIN tidak ditemukan: {bin_path}")
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"Hermes timeout ({timeout}s)")
    if r.returncode != 0:
        raise RuntimeError(f"Hermes gagal (exit {r.returncode}): {r.stderr[:300]}")
    TOTAL["calls"] += 1
    print(f"    [hermes] via {bin_path}", flush=True)
    return r.stdout


def ask(role, msgs, max_tokens, tag, on_text=None, model=None):
    """Panggil model untuk satu tugas. model=<nama> memaksa nama tertentu (tetap lewat lookup models.json)."""
    mid = model or ROLES[role]
    cfg = get_model_config(mid, REGISTRY)
    if cfg.get("type") == "hermes":  # model default: binary hermes via subprocess, bukan HTTP
        return hermes_chat(mid, msgs, tag)
    if cfg.get("use_gemini_pool"):  # kontrak models.json: key diputar dari pool, tanpa stream
        return get_pool(cfg).chat(mid, cfg, msgs, max_tokens, tag)
    return openai_chat(mid, cfg, msgs, max_tokens, tag, on_text=on_text)


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
    cur, buf = None, []  # cadangan: format agak meleset
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
        if fp.is_file() and fp.suffix in {".js", ".mjs", ".cjs", ".py", ".json", ".md", ".txt", ".sh", ".example", ".html", ".css", ".ejs", ""} \
                and "node_modules" not in fp.parts and fp.name not in {"PROJECT.md", "DECISIONS.md", "REVIEW.md", "NAME.txt", "IDEA.txt"}:
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
    """Membaca teks stream dan mengumumkan file yang sedang ditulis model."""
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


# ---------- Kode bertahap per kelompok file ----------
GROUP_SYS = "Kamu pembagi tugas. Jawab HANYA dengan JSON valid, tanpa teks lain."
GROUP_USER = """\
Bagi file proyek di bawah jadi kelompok penulisan BERURUTAN, supaya tiap kelompok ditulis dalam satu panggilan pendek (maksimal {n} file per kelompok, file yang besar sebaiknya sendirian).
Ikuti urutan yang diminta di prompt pengguna (misal html dulu, lalu node js, lalu bagian lain). Kalau prompt tidak menyebut urutan: frontend (html/css/js browser) -> backend -> package.json, .env.example, README.
Ambil nama file dari bagian "Struktur file" di rencana, jangan menambah file lain.

Prompt pengguna:
<<<
{idea}
>>>

Rencana:
<<<
{plan}
>>>

Jawab: {{"groups":[{{"name":"html","files":["public/index.html"]}}]}}"""

CODE_FORMAT_PART = CODE_FORMAT.replace(
    "Tulis SEMUA file proyek dengan format persis ini",
    "Tulis HANYA file yang diminta di atas dengan format persis ini")

PLAN_FILE_RE = re.compile(r"[\w@./-]+\.(?:html|css|js|mjs|cjs|json|md|txt|py|sh|example|ejs)\b")
NOT_FILES = {"node.js", "express.js", "next.js", "vue.js", "react.js", "nuxt.js", "three.js",
             "discord.js", "chart.js", "d3.js", "socket.io.js"}


def _rel(p):
    while p.startswith("./"):
        p = p[2:]
    return p


def fallback_groups(plan):
    """Kalau pembagian gagal: ambil nama file dari rencana, kelompokkan html/css -> node/py -> lainnya."""
    paths = []
    for m in PLAN_FILE_RE.finditer(plan):
        p = _rel(m.group(0))
        if not p or ".." in p or p.startswith("http"):
            continue
        if p.lower() in NOT_FILES and "/" not in p:
            continue
        if p not in paths:
            paths.append(p)
    return split_kinds([{"name": "auto", "files": paths}])


def file_kind(p):
    ext = pathlib.PurePosixPath(p).suffix.lower()
    if ext == ".css":
        return "css"
    if ext in {".html", ".ejs"} or p.startswith(("public/", "views/", "static/")):
        return "html"
    if ext in {".js", ".mjs", ".cjs", ".py"}:
        return "node"
    return "lainnya"


def split_kinds(groups):
    """Pecah tiap kelompok jadi html -> css -> node -> lainnya, supaya tiap bagian bisa punya model sendiri."""
    out = []
    for g in groups:
        by = {}
        for f in g["files"]:
            by.setdefault(file_kind(f), []).append(f)
        for k in GROUP_TASKS:
            if k in by:
                out.append({"name": k, "files": by[k]})
    return out


def normalize_groups(data):
    out, seen = [], set()
    for g in (data or {}).get("groups") or []:
        if not isinstance(g, dict):
            continue
        files = []
        for f in g.get("files") or []:
            f = _rel(str(f).strip().strip("`"))
            p = pathlib.PurePosixPath(f)
            if f and not p.is_absolute() and ".." not in p.parts and f not in seen:
                seen.add(f)
                files.append(f)
        if not files:
            continue
        name = str(g.get("name") or f"kelompok{len(out) + 1}").strip()[:30]
        for i in range(0, len(files), GROUP_MAX_FILES):
            part = files[i:i + GROUP_MAX_FILES]
            out.append({"name": name if i == 0 else f"{name} {i // GROUP_MAX_FILES + 1}", "files": part})
    return out


def stage_groups(plan, idea):
    """Model 'plan' membagi file jadi kelompok berurutan; kalau gagal pakai pembagian otomatis."""
    msgs = [
        {"role": "system", "content": GROUP_SYS},
        {"role": "user", "content": GROUP_USER.format(n=GROUP_MAX_FILES, idea=idea, plan=plan)},
    ]
    try:
        data = extract_json(ask("plan", msgs, 1500, "groups")) or {}
    except Exception as e:
        print(f"    pembagian kelompok gagal ({e!r}), pakai pembagian otomatis", flush=True)
        data = {}
    return split_kinds(normalize_groups(data)) or fallback_groups(plan)


def pick_group_model(g):
    """Prioritas: prompt (html=...) > flag --group-model > model tugas 'code'. Hasilnya nama yang sudah tervalidasi."""
    nm = _norm(g.get("name", ""))
    cands = [PROMPT_GROUP_MODELS.get(nm)]
    cands += [v for k, v in GROUP_MODELS.items() if k and k in nm]
    cands.append(ROLES["code"])
    return next(c for c in cands if c)


def context_files(done, budget=12000, per_file=3000):
    if not done:
        return ""
    out, used = [], 0
    for p, c in done.items():
        if used + min(len(c), per_file) > budget:
            out.append(f"=== {p} === (isi tidak dilampirkan, hanya nama file)")
            continue
        snip = c if len(c) <= per_file else c[:per_file] + "\n... (dipotong)"
        out.append(f"=== {p} ===\n{snip}")
        used += len(snip)
    return ("File yang SUDAH ada (jangan ditulis ulang; samakan nama endpoint, id elemen, dan path dengan file ini):\n"
            + "\n".join(out) + "\n")


def stage_code_group(plan, constraints, files, done, label, model, watcher=None, max_tokens=None):
    names = "\n".join(f"- {f}" for f in files)
    msgs = [
        {"role": "system", "content": "Kamu programmer senior. Tulis kode yang jalan, lengkap, dan sederhana."},
        {"role": "user", "content": (
            f"Rencana proyek:\n{plan}\n\nBatasan teknis:\n{constraints}"
            f"{skill_block('Skill yang WAJIB diikuti (aturan kualitas):')}\n"
            f"Proyek ditulis BERTAHAP. Sekarang HANYA tulis file berikut (kelompok {label}):\n{names}\n\n"
            f"{context_files(done)}\n{CODE_FORMAT_PART}"
        )},
    ]
    text = ask("code", msgs, max_tokens or CODE_MAX_TOKENS, "code:" + label,
               on_text=watcher.feed if watcher else None, model=model)
    if watcher:
        watcher.finish(text)
    return text


def complete_files(raw):
    """Hanya file lengkap (ada === END ===); file yang kepotong di ujung dibuang."""
    files = {}
    for path, content in FILE_RE.findall(raw):
        _clean_file(files, path, content.split("\n"))
    return files


def log_raw(label, raw):
    with open(HERE / "last_code_raw.txt", "a", encoding="utf-8") as f:
        f.write(f"\n##### {label} #####\n{raw}\n")


def write_group(outdir, plan, constraints, name, todo, model, gid):
    """Tulis satu kelompok. File yang kepotong/hilang diulang satu per satu."""
    done = {p: c for p, c in read_all(outdir).items() if p not in todo}
    raw = stage_code_group(plan, constraints, todo, done, name, model, FileWatcher(outdir, model, parent=gid))
    log_raw(name, raw)
    got = complete_files(raw)
    for f in [f for f in todo if f not in got]:
        fid = f"{gid}-{f}"
        print(f"    {f} belum lengkap, ditulis ulang sendiri ({model})", flush=True)
        ev("start", id=fid, text=f"Menulis ulang {f} (satu file)", who=model)
        raw1 = stage_code_group(plan, constraints, [f], {**done, **got}, f, model,
                                FileWatcher(outdir, model, parent=fid), max_tokens=CODE_MAX_TOKENS * 2)
        log_raw(f, raw1)
        one = complete_files(raw1) or parse_files(raw1)
        if f in one:
            got[f] = one[f]
        elif len(one) == 1:
            got[f] = next(iter(one.values()))
        else:
            ev("note", text=f"{f} gagal ditulis ({model})")
        ev("done", id=fid)
    got = {p: c for p, c in got.items() if p in todo or p not in done}
    write_files(outdir, got)
    return got


def code_in_groups(outdir, plan, idea, constraints, resume, decisions):
    """Tahap 3: tulis kode bertahap per kelompok, cek sintaks tiap kelompok."""
    print("3/5 Tulis kode bertahap (per kelompok file)...", flush=True)
    ev("start", id="groups", text="Membagi file jadi kelompok penulisan", who=who("plan"))
    groups = stage_groups(plan, idea)
    if not groups:  # rencana tidak menyebut nama file sama sekali: sekali tulis
        ev("done", id="groups", text="Tidak ada daftar file, tulis sekali jalan")
        L = who("code")
        ev("start", id="code", text="Menulis kode", who=L)
        raw = stage_code(plan, constraints, FileWatcher(outdir, L, parent="code"))
        (HERE / "last_code_raw.txt").write_text(raw, encoding="utf-8")
        files = parse_files(raw)
        if not files:
            ev("fail", id="code", text=f"{L} tidak mengeluarkan file yang benar")
            sys.exit(f"{L} tidak mengeluarkan file dengan format yang benar. Lihat last_code_raw.txt")
        write_files(outdir, files)
        decisions.append(f"- {L} menulis {len(files)} file: {', '.join(files)}.\n")
        return
    ev("done", id="groups", text="Kelompok: " + " -> ".join(g["name"] for g in groups))
    decisions.append("- Kode ditulis bertahap: " + " -> ".join(g["name"] for g in groups) + ".\n")
    (HERE / "last_code_raw.txt").write_text("", encoding="utf-8")
    total = 0
    for gi, g in enumerate(groups, 1):
        todo = [f for f in g["files"] if not (resume and (outdir / f).is_file())]
        if not todo:
            continue
        m = pick_group_model(g)
        gid = f"g{gi}"
        print(f"3/5 Kelompok {gi}/{len(groups)}: {g['name']} ({m}) -> {', '.join(todo)}", flush=True)
        ev("start", id=gid, text=f"Menulis kelompok {g['name']} ({len(todo)} file)", who=m)
        got = write_group(outdir, plan, constraints, g["name"], todo, m, gid)
        ev("done", id=gid)
        total += len(got)
        if not got:
            decisions.append(f"- Kelompok {g['name']} ({m}): tidak ada file yang berhasil ditulis.\n")
            continue
        decisions.append(f"- Kelompok {g['name']} ({m}) menulis {len(got)} file: {', '.join(got)}.\n")
        problems = [p for p in auto_checks(outdir) if p["file"] in got]
        if problems and ROLES["fix"] != "skip":
            fid = f"{gid}fix"
            fm = ROLES["fix"] if "fix" in EXPLICIT else m  # tanpa penugasan fix: ikut model yang menulis kelompok ini
            print(f"    {len(problems)} error sintaks, diperbaiki ({fm})...", flush=True)
            ev("start", id=fid, text=f"Memperbaiki {len(problems)} error sintaks ({g['name']})", who=fm)
            fixed = parse_files(stage_fix(plan, read_all(outdir), problems,
                                          FileWatcher(outdir, fm, parent=fid), model=fm))
            if fixed:
                write_files(outdir, fixed)
                decisions.append(f"- Kelompok {g['name']}: {fm} memperbaiki {', '.join(fixed)}.\n")
            ev("done", id=fid)
    if total == 0 and not read_all(outdir):
        ev("fail", id="groups", text="Tidak ada file yang berhasil ditulis")
        sys.exit("Tidak ada file yang berhasil ditulis. Lihat last_code_raw.txt")


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


def stage_fix(plan, files, issues, watcher=None, model=None):
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
    text = ask("fix", msgs, 8000, "fix", on_text=watcher.feed if watcher else None, model=model)
    if watcher:
        watcher.finish(text)
    return text


# ---------- Alur utama ----------
def run(idea, name, rounds, approve, constraints, resume=False):
    outdir = HERE / "projects" / name
    if resume:
        if not (outdir / "PROJECT.md").exists():
            sys.exit(f"Tidak bisa lanjut: {outdir / 'PROJECT.md'} tidak ada.")
        if not idea:
            ip = outdir / "IDEA.txt"
            if not ip.exists():
                sys.exit("IDEA.txt tidak ada di proyek ini. Kirim ide aslinya sebagai argumen: "
                         f'python3 pipeline.py "ide asli" --resume {name}')
            idea = ip.read_text(encoding="utf-8").strip()

    ev("hello")
    clean = resolve_roles(idea)  # fail-fast: semua model divalidasi ke models.json sebelum ada panggilan

    outdir.mkdir(parents=True, exist_ok=True)
    (outdir / "IDEA.txt").write_text(idea + "\n", encoding="utf-8")
    dp = outdir / "DECISIONS.md"
    if resume and dp.exists():
        decisions = [dp.read_text(encoding="utf-8").rstrip() + "\n", "- Dilanjutkan dengan --resume.\n"]
    else:
        decisions = [f"# DECISIONS {name}\n", f"- Ide: {idea}\n"]
        if resume:
            decisions.append("- Dilanjutkan dengan --resume.\n")

    print("    alur: " + " -> ".join(f"{who(r)} ({t})" for r, t in
          [("plan", "rencana"), ("expand", "tambah fitur"), ("code", "kode"), ("review", "periksa"), ("fix", "perbaiki")]
          if ROLES[r] != "skip"), flush=True)
    setup_txt = (f"plan={who('plan')}, expand={who('expand')}, review={who('review')}, fix={who('fix')}, "
                 + ", ".join(f"{KIND_LABEL[k]}={pick_group_model({'name': k})}" for k in GROUP_TASKS))
    print("    setelan model: " + setup_txt, flush=True)
    ev("note", text="Setelan model: " + setup_txt)
    decisions.append(f"- Setelan model: {setup_txt}.\n")

    names = load_skills(clean)
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
        plan = stage_plan(clean, constraints)
        ev("done", id="plan")
        decisions.append(f"- Rencana awal dibuat oleh {who('plan')}.\n")

        if ROLES["expand"] != "skip":
            print(f"2/5 Tambah fitur terlewat ({who('expand')})...", flush=True)
            ev("start", id="expand", text="Menambah fitur yang terlewat", who=who("expand"))
            plan = stage_expand(plan, clean, constraints)
            ev("done", id="expand")
            decisions.append(f"- {who('expand')} menambah fitur yang terlewat dan memperbarui checklist.\n")
        (outdir / "PROJECT.md").write_text(plan, encoding="utf-8")
        suggested = extract_name(plan, clean)
        (outdir / "NAME.txt").write_text(suggested + "\n", encoding="utf-8")
        print(f"NAMA_SARAN: {suggested}", flush=True)

    if approve:
        print("\n" + plan + "\n")
        if input("Lanjut tulis kode? (ya/tidak): ").strip().lower() not in {"ya", "y", "lanjut"}:
            print("Dihentikan. Rencana tersimpan di", outdir / "PROJECT.md")
            return

    code_in_groups(outdir, plan, clean, constraints, resume, decisions)

    review = {}
    ok = False
    for r in range(1, rounds + 1):
        print(f"4/5 Cek otomatis + periksa (putaran {r}/{rounds})...", flush=True)
        current = read_all(outdir)
        ev("start", id=f"chk{r}", text=f"Cek sintaks otomatis (putaran {r}/{rounds})", who="tanpa model")
        problems = auto_checks(outdir)
        ev("done", id=f"chk{r}", text=f"Cek sintaks otomatis (putaran {r}): " + (f"{len(problems)} masalah" if problems else "lolos"))
        ev("start", id=f"rev{r}", text=f"Memeriksa kode terhadap checklist (putaran {r})",
           who=who("review") if ROLES["review"] != "skip" else "tanpa model")
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
    ap.add_argument("--approve", action="store_true", help="minta persetujuan sebelum kode ditulis")
    ap.add_argument("--group-model", default=os.getenv("GROUP_MODELS", ""), metavar="K=NAMA,...",
                    help='model per kelompok file (nama key di models.json), contoh: "html=<nama>,node=<nama>" (penugasan di prompt tetap lebih utama)')
    ap.add_argument("--constraints", default=None, help="path file batasan teknis (default: constraints.md atau bawaan)")
    a = ap.parse_args()

    if not a.idea and not a.resume:
        ap.error("isi ide proyek, atau pakai --resume NAMA")
    name = a.resume or a.name or parse_name(a.idea) or re.sub(r"[^a-z0-9]+", "-", a.idea.lower())[:30].strip("-") or "proyek"
    for part in a.group_model.split(","):
        if "=" in part:
            k, v = part.split("=", 1)
            if _norm(k) and v.strip():
                GROUP_MODELS[_norm(k)] = v.strip()  # nama mentah; divalidasi ke models.json di resolve_roles()
    cpath = pathlib.Path(a.constraints) if a.constraints else HERE / "constraints.md"
    constraints = cpath.read_text(encoding="utf-8") if cpath.exists() else DEFAULT_CONSTRAINTS
    run(a.idea, name, a.rounds, a.approve, constraints, resume=bool(a.resume))


if __name__ == "__main__":
    main()
