"""NEXORA - LLM Manager: پروفایل مدل‌ها (محلی GGUF و API سازگار با OpenAI/Ollama)، دانلود، بنچمارک،
مدیریت منابع (حداکثر تعداد مدل محلیِ هم‌زمان در RAM با LRU) و لاگ llama-server."""
import hashlib, json, os, re, shutil, socket, subprocess, sys, threading, time, urllib.request, urllib.error
from pathlib import Path
from urllib.parse import urlparse

import agent

ROOT = agent.ROOT
MODELS, LOGS = ROOT / "models", ROOT / "logs"
MODELS.mkdir(exist_ok=True)
LOGS.mkdir(exist_ok=True)
REG, CAT = ROOT / "models.json", ROOT / "models_catalog.json"
LLAMA = os.environ.get("LLAMA_SERVER", "llama-server")
HF = os.environ.get("HF_ENDPOINT", "https://huggingface.co").rstrip("/")  # برای آینه: HF_ENDPOINT=https://...
PORT0 = int(os.environ.get("LLAMA_PORT0", "8080"))
MODES = ("single", "auto", "specialist", "parallel", "debate", "consensus")
TAGS = ("chat", "code", "review", "research")
servers = {}  # model_id -> {"p": Popen, "port": int, "t": last_used, "log": Path}
LOCK = threading.RLock()


# ---------- رجیستری ----------
def reg():
    r = agent.load(REG, {})
    for k, v in {"models": {}, "profiles": {}, "meta": {}, "main": None, "mode": "single", "max_loaded": 2}.items():
        r.setdefault(k, v)
    # مهاجرت از نسخه‌ی قبل (brain/advisor)
    if "active" in r:
        r["main"] = r.pop("active") or r["main"]
    if "advisor" in r:
        a = r.pop("advisor")
        if a:
            r["meta"].setdefault(a, {})["tags"] = ["review"]
            if r["mode"] == "single":
                r["mode"] = "debate"
    ra = r.pop("remote_advisor", None)
    if ra and ra.get("url"):
        r["profiles"]["r:advisor"] = {"name": "Remote advisor", "provider": "openai", "url": norm_url(ra["url"]),
                                      "key": ra.get("key", ""), "model": ra.get("model", ""), "tags": ["review"], "enabled": True}
    return r


def save(r):
    REG.write_text(json.dumps(r, indent=1, ensure_ascii=False))


def sync():
    """فایل‌های gguf دستی را اضافه می‌کند، ورودی‌های بی‌فایل را حذف می‌کند، و مغز را در صورت نبودن اولین مدل می‌گذارد."""
    with LOCK:
        r = reg()
        for f in MODELS.glob("*.gguf"):
            r["models"].setdefault(f.stem, {"file": f.name, "url": "", "score": None, "tps": None})
            r["models"][f.stem]["size_mb"] = round(f.stat().st_size / 1e6)
        for mid in [m for m, v in r["models"].items() if not (MODELS / v["file"]).exists()]:
            del r["models"][mid]
            r["meta"].pop(mid, None)
        if r["main"] and r["main"] not in r["models"] and r["main"] not in r["profiles"]:
            r["main"] = None
        if not r["main"] and r["models"]:
            r["main"] = next(iter(r["models"]))
        save(r)
        return r


def norm_url(u):
    u = u.strip().rstrip("/")
    if not re.match(r"https?://", u):
        raise ValueError("آدرس باید با http:// یا https:// شروع شود")
    if u.endswith("/chat/completions"):
        return u
    return (u if u.endswith("/v1") else u + "/v1") + "/chat/completions"


def _alive(mid):
    s = servers.get(mid)
    return bool(s and s["p"].poll() is None)


def plist():
    r = reg()
    out = []
    for mid, m in r["models"].items():
        meta = r["meta"].get(mid, {})
        out.append({"id": mid, "name": mid, "provider": "local", "tags": meta.get("tags", ["chat", "code"]),
                    "enabled": meta.get("enabled", True), "main": r["main"] == mid, "loaded": _alive(mid),
                    "size_mb": m.get("size_mb"), "score": m.get("score"), "tps": m.get("tps")})
    for pid, p in r["profiles"].items():
        out.append({"id": pid, "name": p["name"], "provider": "api", "url": p["url"], "model": p.get("model", ""),
                    "tags": p.get("tags", ["chat"]), "enabled": p.get("enabled", True), "main": r["main"] == pid, "loaded": None})
    return out


def catalog():
    out = []
    have = {v["file"] for v in reg()["models"].values()}
    for c in agent.load(CAT, []):
        c = dict(c, url=c["url"].replace("{HF}", HF))
        c["downloaded"] = Path(urlparse(c["url"]).path).name in have
        out.append(c)
    return out


def status():
    r = reg()
    m = r["main"]
    if not m:
        return {"main": None, "ready": False, "mode": r["mode"], "msg": "هنوز مدلی انتخاب نشده؛ به تب «مدل‌ها» برو"}
    if m in r["models"]:
        ready = _alive(m)
        return {"main": m, "ready": ready, "mode": r["mode"], "msg": "آماده" if ready else "با اولین پیام بارگذاری می‌شود"}
    return {"main": m, "ready": True, "mode": r["mode"], "msg": "مدل API"}


# ---------- مدیریت پروفایل‌ها ----------
def _entry(r, pid):
    if pid in r["models"]:
        return r["meta"].setdefault(pid, {})
    if pid in r["profiles"]:
        return r["profiles"][pid]
    raise RuntimeError("چنین پروفایلی نیست")


def set_main(pid):
    with LOCK:
        r = reg()
        _entry(r, pid)
        r["main"] = pid
        _entry(r, pid)["enabled"] = True
        save(r)


def set_enabled(pid, flag):
    with LOCK:
        r = reg()
        _entry(r, pid)["enabled"] = bool(flag)
        if not flag and r["main"] == pid:
            r["main"] = None
        save(r)
    if not flag:
        unload(pid)


def set_tags(pid, tags):
    with LOCK:
        r = reg()
        _entry(r, pid)["tags"] = [t for t in tags if t in TAGS] or ["chat"]
        save(r)


def set_mode(mode):
    if mode not in MODES:
        raise RuntimeError("حالت نامعتبر")
    with LOCK:
        r = reg()
        r["mode"] = mode
        save(r)


def set_max_loaded(n):
    with LOCK:
        r = reg()
        r["max_loaded"] = max(1, min(4, int(n)))
        save(r)


def add_profile(name, url, key="", model="", tags=("chat",)):
    with LOCK:
        r = reg()
        slug = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_") or "api"
        pid, i = f"r:{slug}", 2
        while pid in r["profiles"]:
            pid, i = f"r:{slug}{i}", i + 1
        r["profiles"][pid] = {"name": name[:40] or slug, "provider": "openai", "url": norm_url(url), "key": key, "model": model,
                              "tags": [t for t in tags if t in TAGS] or ["chat"], "enabled": True}
        if not r["main"]:
            r["main"] = pid
        save(r)
        return pid


def remove_profile(pid):
    with LOCK:
        r = reg()
        if pid not in r["profiles"]:
            raise RuntimeError("فقط پروفایل‌های API حذف می‌شوند؛ مدل محلی را با «حذف» پاک کن")
        del r["profiles"][pid]
        if r["main"] == pid:
            r["main"] = None
        save(r)


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
        have = 0
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
        if mid == r["main"]:
            raise RuntimeError("این مدل اصلی است؛ اول مدل دیگری را «اصلی» کن")
        m = r["models"].get(mid)
        unload(mid)
        if m:
            (MODELS / m["file"]).unlink(missing_ok=True)
        sync()


# ---------- اجرای llama-server با مدیریت منابع ----------
def _up(port):
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=2)
        return True
    except Exception:
        return False


def _port_free(port):
    with socket.socket() as s:
        return s.connect_ex(("127.0.0.1", port)) != 0


def tail(path, n=500):
    try:
        return "لاگ: " + Path(path).read_text(errors="ignore")[-n:].strip()
    except Exception:
        return ""


def unload(mid):
    with LOCK:
        s = servers.pop(mid, None)
        if s and s["p"].poll() is None:
            s["p"].terminate()
            try:
                s["p"].wait(10)
            except subprocess.TimeoutExpired:
                s["p"].kill()


def load(mid, wait=240):
    """مدل محلی را (در صورت نیاز با خارج‌کردن قدیمی‌ترین مدل) بالا می‌آورد و پورتش را برمی‌گرداند."""
    with LOCK:
        r = reg()
        if mid not in r["models"]:
            raise RuntimeError("چنین مدل محلی‌ای نیست")
        s = servers.get(mid)
        if s and s["p"].poll() is None:
            s["t"] = time.time()
            return s["port"]
        servers.pop(mid, None)
        if not (shutil.which(LLAMA) or Path(LLAMA).exists()):
            raise RuntimeError("llama-server پیدا نشد. در Termux: pkg install llama-cpp (یا مسیرش را در LLAMA_SERVER بگذار)")
        alive = [m for m in servers if _alive(m)]
        while len(alive) >= max(1, int(r["max_loaded"])):
            lru = min(alive, key=lambda m: servers[m]["t"])
            unload(lru)
            alive.remove(lru)
        used = {x["port"] for x in servers.values()}
        port = next(p for p in range(PORT0, PORT0 + 60) if p not in used and _port_free(p))
        log = LOGS / f"llama_{mid}.log"
        cmd = [LLAMA, "-m", str(MODELS / r["models"][mid]["file"]), "--host", "127.0.0.1", "--port", str(port), "-c", "4096"]
        with open(log, "wb") as lf:
            p = subprocess.Popen(cmd, stdout=lf, stderr=subprocess.STDOUT)
        servers[mid] = {"p": p, "port": port, "t": time.time(), "log": log}
        t0 = time.time()
        while time.time() - t0 < wait:
            if p.poll() is not None:
                servers.pop(mid, None)
                raise RuntimeError(f"llama-server بسته شد (کد {p.returncode}؛ احتمالا RAM کم، فایل خراب یا ناسازگاری پردازنده). {tail(log)}")
            if _up(port):
                return port
            time.sleep(1)
        unload(mid)
        raise RuntimeError("بارگذاری مدل بیش از حد طول کشید. " + tail(log))


def endpoint(pid):
    r = reg()
    if pid in r["models"]:
        return {"url": f"http://127.0.0.1:{load(pid)}/v1/chat/completions"}
    p = r["profiles"].get(pid)
    if p:
        return {"url": p["url"], "key": p.get("key", ""), "model": p.get("model", "")}
    raise RuntimeError(f"پروفایل {pid} پیدا نشد")


def advisor_pid():
    """مشاور: اولین پروفایل فعالِ غیر اصلی (ترجیحا با برچسب review). در حالت single وجود ندارد."""
    r = reg()
    if r["mode"] == "single":
        return None
    others = [p for p in plist() if p["enabled"] and not p["main"]]
    rev = [p for p in others if "review" in p["tags"]]
    pick = rev or others
    return pick[0]["id"] if pick else None


def resolve(role):
    r = reg()
    if role == "brain":
        return endpoint(r["main"]) if r["main"] else None
    if role == "advisor":
        a = advisor_pid()
        return endpoint(a) if a else None
    return endpoint(role)


agent.RESOLVER = resolve
agent.ADVISOR_FN = advisor_pid


def apply():
    """مدل اصلیِ محلی را از قبل بارگذاری می‌کند تا اولین پیام کند نباشد."""
    r = reg()
    if r["main"] in r["models"]:
        load(r["main"])


# ---------- بنچمارک (کاملا خودکار، بدون قاضی LLM) ----------
def _py_ok(code):
    return agent.run_code_ok(code)


def _run_tests(role):
    pts, tps = 0, None
    L = lambda p, **k: agent.llm(p, role=role, **k)
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
    try:
        t = time.time()
        txt = L("Write a Python function is_prime(n) that returns True if n is prime. Reply with one ```python block only.")
        dt = max(time.time() - t, 0.01)
        tps = round(len(txt) / 4 / dt, 1)
        if _py_ok(agent.extract_code(txt) + "\nassert is_prime(2) and is_prime(97) and not is_prime(1) and not is_prime(91) and not is_prime(0)"):
            pts += 30
    except Exception:
        pass
    try:
        if "391" in L("What is 17*23? Reply with only the number."):
            pts += 10
    except Exception:
        pass
    try:
        t = L("یک جمله کوتاه فارسی درباره دریا بنویس.")
        letters = [c for c in t if c.isalpha()]
        if len(t) >= 12 and letters and sum("؀" <= c <= "ۿ" for c in letters) / len(letters) > 0.6:
            pts += 15
    except Exception:
        pass
    try:
        code = agent.extract_code(L("Fix the bug. Reply with one ```python block only:\n```python\ndef avg(xs):\n    return sum(xs)/(len(xs)+1)\n```"))
        if _py_ok(code + "\nassert avg([2,4])==3 and avg([5])==5"):
            pts += 20
    except Exception:
        pass
    return pts, tps


def benchmark(mid):
    with LOCK:
        was = _alive(mid)
        port = load(mid)
        agent.ENDPOINTS["bench"] = {"url": f"http://127.0.0.1:{port}/v1/chat/completions"}
        try:
            score, tps = _run_tests("bench")
        finally:
            if not was:
                unload(mid)
        r = reg()
        r["models"][mid].update(score=score, tps=tps, bench_ts=int(time.time()))
        save(r)
        return score, tps


def autopick(on_progress=lambda m: None, use_council=True):
    """همه‌ی مدل‌های محلی را تست می‌کند؛ بهترین «اصلی» می‌شود و دومی (در صورت use_council) مشاور + حالت بحث و نقد."""
    ids = list(sync()["models"])
    if not ids:
        raise RuntimeError("مدلی دانلود نشده")
    for mid in ids:
        on_progress(f"تست {mid}")
        benchmark(mid)
    with LOCK:
        r = reg()
        ranked = sorted(ids, key=lambda m: (r["models"][m]["score"] or 0, r["models"][m]["tps"] or 0), reverse=True)
        r["main"] = ranked[0]
        if use_council and len(ranked) > 1:
            r["meta"].setdefault(ranked[1], {}).update(tags=["review"], enabled=True)
            r["mode"] = "debate"
        else:
            r["mode"] = "single"
        save(r)
    return ranked
