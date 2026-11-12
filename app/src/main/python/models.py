"""مدیر مدل‌ها: دانلود (با ادامه‌ی قطع‌شده)، حذف، بنچمارک خودکار، انتخاب مغز و مشاور، اجرای llama-server."""
import hashlib, json, os, re, shutil, subprocess, sys, threading, time, urllib.request, urllib.error
from pathlib import Path
from urllib.parse import urlparse

import agent

ROOT = agent.ROOT
MODELS = ROOT / "models"
MODELS.mkdir(exist_ok=True)
REG, CAT = ROOT / "models.json", ROOT / "models_catalog.json"
LLAMA = os.environ.get("LLAMA_SERVER", "llama-server")
HF = os.environ.get("HF_ENDPOINT", "https://huggingface.co").rstrip("/")  # برای آینه: HF_ENDPOINT=https://...
PORTS = {"brain": 8080, "advisor": 8081, "bench": 8090}
procs = {}  # role -> (Popen, model_id)
LOCK = threading.RLock()


def reg():
    r = agent.load(REG, {})
    for k, v in {"active": None, "advisor": None, "remote_advisor": None, "models": {}}.items():
        r.setdefault(k, v)
    return r


def save(r):
    REG.write_text(json.dumps(r, indent=1, ensure_ascii=False))


def sync():
    """فایل‌های gguf دستی را هم به لیست اضافه می‌کند و ورودی‌های بی‌فایل را حذف می‌کند."""
    with LOCK:
        r = reg()
        for f in MODELS.glob("*.gguf"):
            r["models"].setdefault(f.stem, {"file": f.name, "url": "", "score": None, "tps": None})
            r["models"][f.stem]["size_mb"] = round(f.stat().st_size / 1e6)
        for mid in [m for m, v in r["models"].items() if not (MODELS / v["file"]).exists()]:
            del r["models"][mid]
            for role in ("active", "advisor"):
                if r[role] == mid:
                    r[role] = None
        save(r)
        return r


def catalog():
    out = []
    have = {v["file"] for v in reg()["models"].values()}
    for c in agent.load(CAT, []):
        c = dict(c, url=c["url"].replace("{HF}", HF))
        c["downloaded"] = Path(urlparse(c["url"]).path).name in have
        out.append(c)
    return out


# ---------- دانلود ----------
def download(spec, on_progress=lambda d, t: None):
    cat = {c["id"]: c for c in catalog()}
    entry = cat.get(spec, {})
    url = entry.get("url") or spec.replace("{HF}", HF)
    fname = Path(urlparse(url).path).name
    if not fname.endswith(".gguf"):
        raise ValueError("فقط فایل .gguf پشتیبانی می‌شود")
    dest = MODELS / fname
    part = dest.with_name(fname + ".part")
    have = part.stat().st_size if part.exists() else 0
    headers = dict(agent.UA)
    if have:
        headers["Range"] = f"bytes={have}-"
    try:
        r = urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=60)
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"دانلود رد شد (HTTP {e.code}). آدرس را بررسی کن یا HF_ENDPOINT را روی یک آینه بگذار") from e
    if have and r.status != 206:
        have = 0  # سرور Range را نادیده گرفت
    cr = r.headers.get("Content-Range")
    total = int(cr.split("/")[-1]) if cr else int(r.headers.get("Content-Length", 0)) + have
    if total and shutil.disk_usage(MODELS).free < (total - have) * 1.1:
        raise RuntimeError("فضای خالی کافی نیست")
    done = have
    with open(part, "ab" if have else "wb") as f:
        while True:
            chunk = r.read(1 << 20)
            if not chunk:
                break
            f.write(chunk)
            done += len(chunk)
            on_progress(done, total)
    if total and done < total:
        raise RuntimeError("دانلود ناقص ماند؛ دوباره بزن تا از همان‌جا ادامه دهد")
    want = entry.get("sha256")
    if want and hashlib.sha256(part.read_bytes()).hexdigest() != want:
        part.unlink()
        raise RuntimeError("sha256 فایل با مقدار کاتالوگ نمی‌خواند؛ فایل حذف شد")
    part.rename(dest)
    sync()
    return dest.stem


def delete(mid):
    with LOCK:
        r = reg()
        if mid in (r["active"], r["advisor"]):
            raise RuntimeError("این مدل در حال استفاده است؛ اول مدل دیگری را انتخاب کن")
        m = r["models"].get(mid)
        if m:
            (MODELS / m["file"]).unlink(missing_ok=True)
        sync()


# ---------- اجرای سرور ----------
def _up(port):
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=2)
        return True
    except Exception:
        return False


def stop(role):
    p = procs.pop(role, None)
    if p and p[0].poll() is None:
        p[0].terminate()
        try:
            p[0].wait(10)
        except subprocess.TimeoutExpired:
            p[0].kill()


def start(role, mid, wait=240):
    if role in procs and procs[role][1] == mid and procs[role][0].poll() is None:
        return
    stop(role)
    if not (shutil.which(LLAMA) or Path(LLAMA).exists()):
        raise RuntimeError("llama-server پیدا نشد. در Termux: pkg install llama-cpp (یا مسیرش را در LLAMA_SERVER بگذار)")
    f = MODELS / reg()["models"][mid]["file"]
    p = subprocess.Popen([LLAMA, "-m", str(f), "--host", "127.0.0.1", "--port", str(PORTS[role]), "-c", "4096"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    procs[role] = (p, mid)
    t = time.time()
    while time.time() - t < wait:
        if p.poll() is not None:
            procs.pop(role, None)
            raise RuntimeError("llama-server بسته شد (احتمالا RAM کم است یا فایل خراب)")
        if _up(PORTS[role]):
            return
        time.sleep(1)
    stop(role)
    raise RuntimeError("بارگذاری مدل بیش از حد طول کشید")


def url_of(role):
    return f"http://127.0.0.1:{PORTS[role]}/v1/chat/completions"


def apply():
    """سرورها را طبق انتخاب فعلی روشن می‌کند و agent را به آن‌ها وصل می‌کند."""
    with LOCK:
        r = reg()
        if r["active"]:
            start("brain", r["active"])
            agent.ENDPOINTS["brain"] = url_of("brain")
        if r["remote_advisor"] and agent.online():
            stop("advisor")
            agent.ENDPOINTS["advisor"] = r["remote_advisor"]
        elif r["advisor"]:
            start("advisor", r["advisor"])
            agent.ENDPOINTS["advisor"] = url_of("advisor")
        else:
            stop("advisor")
            agent.ENDPOINTS["advisor"] = None


def use(role, mid):
    with LOCK:
        r = reg()
        if mid and mid not in r["models"]:
            raise RuntimeError("چنین مدلی نیست")
        r["active" if role == "brain" else "advisor"] = mid
        save(r)
        apply()


def set_remote(url, key="", model=""):
    r = reg()
    r["remote_advisor"] = {"url": url, "key": key, "model": model} if url else None
    save(r)
    apply()


# ---------- بنچمارک (کاملا خودکار، بدون قاضی LLM) ----------
def _py_ok(code):
    return agent.run_code_ok(code)


def _run_tests(role):
    pts, tps = 0, None
    L = lambda p, **k: agent.llm(p, role=role, **k)
    # 1) مسیریابی JSON و استخراج آرگومان (25)
    try:
        out = agent.extract_json(L(agent.ROUTER.format(tools="- add_numbers: add two numbers | run(a, b)", history="(none)",
                                                       request="جمع ۲۳ و ۱۹ رو حساب کن"), json_mode=True))
        if out.get("tool") == "add_numbers":
            pts += 10
            a = out.get("args") or {}
            if sorted(int(float(str(v))) for v in a.values() if re.fullmatch(r"[\d.]+", str(v))) == [19, 23]:
                pts += 15
    except Exception:
        pass
    # 2) کدنویسی (30) + سرعت
    try:
        t = time.time()
        txt = L("Write a Python function is_prime(n) that returns True if n is prime. Reply with one ```python block only.")
        dt = max(time.time() - t, 0.01)
        tps = round(len(txt) / 4 / dt, 1)
        code = agent.extract_code(txt)
        if _py_ok(code + "\nassert is_prime(2) and is_prime(97) and not is_prime(1) and not is_prime(91) and not is_prime(0)"):
            pts += 30
    except Exception:
        pass
    # 3) ریاضی/پیروی از دستور (10)
    try:
        if "391" in L("What is 17*23? Reply with only the number."):
            pts += 10
    except Exception:
        pass
    # 4) فارسی (15)
    try:
        t = L("یک جمله کوتاه فارسی درباره دریا بنویس.")
        letters = [c for c in t if c.isalpha()]
        if len(t) >= 12 and letters and sum("؀" <= c <= "ۿ" for c in letters) / len(letters) > 0.6:
            pts += 15
    except Exception:
        pass
    # 5) رفع باگ (20)
    try:
        code = agent.extract_code(L("Fix the bug. Reply with one ```python block only:\n```python\ndef avg(xs):\n    return sum(xs)/(len(xs)+1)\n```"))
        if _py_ok(code + "\nassert avg([2,4])==3 and avg([5])==5"):
            pts += 20
    except Exception:
        pass
    return pts, tps


def benchmark(mid):
    with LOCK:
        running = procs.get("brain", (None, None))[1] == mid and procs["brain"][0].poll() is None
        role = "brain" if running else "bench"
        if not running:
            start("bench", mid)
            agent.ENDPOINTS["bench"] = url_of("bench")
        try:
            score, tps = _run_tests(role)
        finally:
            if not running:
                stop("bench")
        r = reg()
        r["models"][mid].update(score=score, tps=tps, bench_ts=int(time.time()))
        save(r)
        return score, tps


def autopick(on_progress=lambda m: None, use_council=True):
    """همه‌ی مدل‌ها را تست می‌کند؛ بهترین (امتیاز، بعد سرعت) می‌شود مغز و دومی مشاور."""
    ids = list(sync()["models"])
    if not ids:
        raise RuntimeError("مدلی دانلود نشده")
    for mid in ids:
        on_progress(f"تست {mid}")
        benchmark(mid)
    r = reg()
    ranked = sorted(ids, key=lambda m: (r["models"][m]["score"] or 0, r["models"][m]["tps"] or 0), reverse=True)
    r["active"] = ranked[0]
    r["advisor"] = ranked[1] if use_council and len(ranked) > 1 else None
    save(r)
    apply()
    return ranked
