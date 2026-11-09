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

TOTAL = {"cost": 0.0, "calls": 0}


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


def http_chat(base, key, model, messages, max_tokens, temperature=0.3, timeout=240):
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


def vikey_chat(model, messages, max_tokens=3000, tag=""):
    if not VIKEY_KEY:
        sys.exit("VIKEY_API_KEY belum di-set.")
    last_err = None
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


def stage_code(plan, constraints):
    msgs = [
        {"role": "system", "content": "Kamu programmer senior. Tulis kode yang jalan, lengkap, dan sederhana."},
        {"role": "user", "content": f"Rencana proyek:\n{plan}\n\nBatasan teknis:\n{constraints}\n\n{CODE_FORMAT}"},
    ]
    return vikey_chat(M_SONNET, msgs, 12000, "code")


def stage_review(plan, files, check_problems):
    code_dump = "\n".join(f"=== {p} ===\n{c}" for p, c in files.items())
    msgs = [
        {"role": "system", "content": "Kamu reviewer kode yang teliti. Jawab HANYA dengan JSON valid."},
        {"role": "user", "content": (
            f"Rencana + checklist:\n{plan}\n\nKode:\n{code_dump}\n\n"
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


def stage_fix(plan, files, issues):
    names = {i.get("file") for i in issues}
    relevant = {p: c for p, c in files.items() if p in names} or files
    code_dump = "\n".join(f"=== {p} ===\n{c}" for p, c in relevant.items())
    msgs = [
        {"role": "system", "content": "Kamu programmer senior. Perbaiki hanya yang disebut."},
        {"role": "user", "content": (
            f"Rencana:\n{plan}\n\nMasalah yang harus diperbaiki:\n{json.dumps(issues, ensure_ascii=False, indent=1)}\n\n"
            f"File terkait saat ini:\n{code_dump}\n\n"
            "Kirim HANYA file yang berubah (isi lengkap tiap file), format:\n" + CODE_FORMAT
        )},
    ]
    return vikey_chat(M_SONNET, msgs, 8000, "fix")


# ---------- Alur utama ----------
def run(idea, name, rounds, approve, constraints):
    outdir = HERE / "projects" / name
    outdir.mkdir(parents=True, exist_ok=True)
    decisions = [f"# DECISIONS {name}\n", f"- Ide: {idea}\n"]

    print("1/5 Rencana (Gemini)...", flush=True)
    plan = stage_plan(idea, constraints)
    decisions.append("- Rencana awal dibuat oleh ketua (Gemini).\n")

    print("2/5 Tambah fitur terlewat (DeepSeek)...", flush=True)
    plan = stage_expand(plan, idea, constraints)
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
    files = parse_files(stage_code(plan, constraints))
    if not files:
        sys.exit("Sonnet tidak mengeluarkan file dengan format yang benar.")
    write_files(outdir, files)
    decisions.append(f"- Sonnet menulis {len(files)} file.\n")

    review = {}
    ok = False
    for r in range(1, rounds + 1):
        print(f"4/5 Cek otomatis + periksa (putaran {r}/{rounds})...", flush=True)
        current = read_all(outdir)
        problems = auto_checks(outdir)
        review = stage_review(plan, current, problems)
        issues = (review.get("issues") or []) + [p for p in problems if p not in (review.get("issues") or [])]
        failed = [x for x in review.get("results", []) if not x.get("pass", True)]
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
        fixed = parse_files(stage_fix(plan, current, issues))
        if fixed:
            write_files(outdir, fixed)

    status = "LOLOS" if ok else "ADA SISA MASALAH (lihat REVIEW.md)"
    rv = ["# REVIEW\n", f"Status: {status}\n\n", "## Ringkasan\n", f"{review.get('summary', '-')}\n\n", "## Checklist\n"]
    for x in review.get("results", []):
        rv.append(f"- {x.get('id')}: {'lulus' if x.get('pass') else 'GAGAL'} - {x.get('note', '')}\n")
    if review.get("issues"):
        rv.append("\n## Masalah tersisa\n")
        for i in review["issues"]:
            rv.append(f"- {i.get('file')}: {i.get('problem')}\n")
    (outdir / "REVIEW.md").write_text("".join(rv), encoding="utf-8")
    (outdir / "DECISIONS.md").write_text("".join(decisions), encoding="utf-8")

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
