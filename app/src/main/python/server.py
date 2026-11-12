#!/usr/bin/env python3
"""اپ وب (PWA): چت + صف کار پس‌زمینه + مدیر مدل‌ها. اجرا: python server.py  ->  http://127.0.0.1:8000
فقط روی localhost گوش می‌دهد. در Termux قبلش: termux-wake-lock"""
import contextlib, io, json, mimetypes, re, shutil, sys, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import agent, models

ROOT = agent.ROOT
JOBS, ALLOW = ROOT / "jobs.json", ROOT / "allowed_packages.txt"
LOCK = threading.Lock()
GPU = threading.Lock()  # همزمان نشدن کار عامل با عوض‌کردن/بنچمارک مدل
state = agent.load(JOBS, {"jobs": [], "paused": False, "next": 1})
tasks = []  # کارهای مدل (دانلود/بنچمارک) - فقط در حافظه
for j in state["jobs"]:
    if j["status"] == "running":
        j["status"] = "queued"
if not ALLOW.exists():
    ALLOW.write_text("pillow\nqrcode\nrequests\nnumpy\nbeautifulsoup4\npyyaml\nmatplotlib\n")


def save():
    JOBS.write_text(json.dumps(state, ensure_ascii=False, indent=1))


def allowed():
    return {l.strip().lower() for l in ALLOW.read_text().splitlines() if l.strip()}


def confirm(q, kind=None, item=None):
    if kind is None or kind == "download":  # ساخت ابزار / دانلود از catalog خودت
        return True
    if kind == "pip" and item.lower() in allowed():
        return True
    raise agent.NeedApproval(kind, item)


agent.confirm = confirm


class Log(io.TextIOBase):
    def __init__(self, job):
        self.job = job

    def write(self, t):
        if t.strip():
            self.job["log"].append(t.strip()[:300])
        return len(t)


def history_for(job):
    done = [j for j in state["jobs"] if j["id"] < job["id"] and j["status"] == "done"][-3:]
    return "\n".join(f"User: {j['msg'][:200]}\nAssistant: {str(j['result'])[:200]}" for j in done)


def worker():
    while True:
        job = None
        with LOCK:
            if not state["paused"]:
                job = next((j for j in state["jobs"] if j["status"] == "queued"), None)
                if job:
                    job["status"], job["log"] = "running", []
                    save()
        if not job:
            time.sleep(1)
            continue
        try:
            with GPU, contextlib.redirect_stdout(Log(job)):
                job["result"] = agent.handle(job["msg"], history=history_for(job))
            job["status"] = "done"
        except agent.NeedApproval as e:
            job["approval"], job["status"] = {"kind": e.kind, "item": e.item}, "needs_approval"
        except Exception as e:
            job["result"], job["status"] = f"خطا: {e}", "failed"
        with LOCK:
            save()


def bg(name, fn):
    t = {"id": int(time.time() * 1000), "name": name, "status": "running", "progress": 0, "msg": ""}
    tasks.append(t)

    def run():
        try:
            def prog(d, total=0):
                if isinstance(d, str):
                    t["msg"] = d
                elif total:
                    t["progress"] = round(100 * d / total)
            res = fn(prog)
            t["status"], t["msg"], t["progress"] = "done", str(res or "")[:200], 100
        except Exception as e:
            t["status"], t["msg"] = "failed", str(e)[:300]
    threading.Thread(target=run, daemon=True).start()


def with_gpu(fn):
    def w(prog):
        with GPU:
            return fn(prog)
    return w


_online = {"t": 0, "v": False}


def is_online():
    if time.time() - _online["t"] > 20:
        _online.update(t=time.time(), v=agent.online())
    return _online["v"]


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, body, ctype="application/json", code=200):
        b = body if isinstance(body, bytes) else (body if isinstance(body, str) else json.dumps(body, ensure_ascii=False)).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8" if ctype.startswith("text") or ctype == "application/json" else ctype)
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        p = self.path.split("?")[0]
        if p == "/":
            return self.send(PAGE, "text/html")
        if p == "/manifest.json":
            return self.send({"name": "SelfDev", "short_name": "SelfDev", "start_url": "/", "display": "standalone",
                              "background_color": "#111", "theme_color": "#111",
                              "icons": [{"src": "/icon.svg", "sizes": "any", "type": "image/svg+xml"}]})
        if p == "/icon.svg":
            return self.send('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64"><rect width="64" height="64" rx="14" fill="#1d6fe8"/><text x="32" y="43" font-size="30" text-anchor="middle" fill="#fff">AI</text></svg>', "image/svg+xml")
        if p == "/api/state":
            with LOCK:
                return self.send({"jobs": state["jobs"][-40:], "paused": state["paused"], "online": is_online(),
                                  "tools": list(agent.tools()), "advisor": agent.has_advisor()})
        if p == "/api/models":
            r = models.sync()
            return self.send({"reg": r, "catalog": models.catalog(), "tasks": tasks[-8:],
                              "llama": bool(shutil.which(models.LLAMA) or Path(models.LLAMA).exists())})
        if p.startswith("/files/"):
            f = agent.WORK / "outputs" / Path(p[7:]).name
            if f.is_file():
                return self.send(f.read_bytes(), mimetypes.guess_type(f.name)[0] or "application/octet-stream")
        self.send({"error": "not found"}, code=404)

    def do_POST(self):
        d = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        p = self.path
        try:
            if p == "/api/chat":
                with LOCK:
                    j = {"id": state["next"], "msg": str(d["msg"])[:2000], "status": "queued", "log": [], "result": None, "approval": None}
                    state["next"] += 1
                    state["jobs"].append(j)
                    save()
            elif p in ("/api/approve", "/api/deny"):
                with LOCK:
                    j = next(j for j in state["jobs"] if j["id"] == d["id"] and j["status"] == "needs_approval")
                    if p == "/api/approve":
                        if j["approval"]["kind"] == "pip":
                            ALLOW.write_text(ALLOW.read_text().rstrip("\n") + "\n" + j["approval"]["item"].lower() + "\n")
                        j["status"], j["approval"] = "queued", None
                    else:
                        j["status"], j["result"] = "failed", "رد شد"
                    save()
            elif p == "/api/pause":
                state["paused"] = bool(d["paused"])
                save()
            elif p == "/api/models/download":
                bg("دانلود " + d["spec"][-40:], lambda prog: models.download(d["spec"], prog))
            elif p == "/api/models/delete":
                models.delete(d["id"])
            elif p == "/api/models/use":
                bg("انتخاب مدل", with_gpu(lambda prog: models.use(d["role"], d.get("id") or None)))
            elif p == "/api/models/benchmark":
                bg("بنچمارک " + d["id"], with_gpu(lambda prog: models.benchmark(d["id"])))
            elif p == "/api/models/autopick":
                bg("بنچمارک همه + انتخاب بهترین", with_gpu(lambda prog: models.autopick(prog, d.get("council", True))))
            elif p == "/api/models/remote":
                bg("مشاور آنلاین", with_gpu(lambda prog: models.set_remote(d.get("url", ""), d.get("key", ""), d.get("model", ""))))
            else:
                return self.send({"error": "not found"}, code=404)
            self.send({"ok": True})
        except Exception as e:
            self.send({"ok": False, "error": str(e)}, code=400)


PAGE = r"""<!doctype html><html lang="fa" dir="rtl"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><link rel="manifest" href="/manifest.json">
<title>SelfDev</title><style>
:root{color-scheme:light dark;--bg:#fff;--fg:#111;--mut:#777;--card:#f2f3f5;--ac:#1d6fe8}
@media(prefers-color-scheme:dark){:root{--bg:#121212;--fg:#eee;--card:#222;--mut:#999}}
*{box-sizing:border-box}body{margin:0;font:15px system-ui,Tahoma;background:var(--bg);color:var(--fg);display:flex;flex-direction:column;height:100dvh}
header{display:flex;gap:6px;align-items:center;padding:8px 10px;border-bottom:1px solid var(--card)}
header b{flex:1}.tab{padding:6px 12px;border-radius:14px;background:var(--card);border:0;color:var(--fg)}.tab.on{background:var(--ac);color:#fff}
main{flex:1;overflow:auto;padding:10px}.msg{max-width:92%;padding:8px 11px;border-radius:14px;margin:6px 0;white-space:pre-wrap;word-break:break-word}
.u{background:var(--ac);color:#fff;margin-inline-start:auto}.a{background:var(--card)}small,.mut{color:var(--mut)}
form{display:flex;gap:6px;padding:8px;border-top:1px solid var(--card)}input,textarea{flex:1;padding:10px;border-radius:12px;border:1px solid var(--card);background:var(--card);color:var(--fg);font:inherit}
button{font:inherit;padding:8px 12px;border-radius:12px;border:0;background:var(--ac);color:#fff}button.g{background:var(--card);color:var(--fg)}button.r{background:#c0392b}
.row{background:var(--card);border-radius:12px;padding:10px;margin:8px 0}.row>div{display:flex;gap:6px;flex-wrap:wrap;align-items:center;margin-top:6px}
.badge{background:var(--ac);color:#fff;border-radius:8px;padding:1px 7px;font-size:12px}.bar{height:6px;background:var(--bg);border-radius:4px;overflow:hidden}.bar i{display:block;height:100%;background:var(--ac)}
details{margin-top:4px}pre{white-space:pre-wrap;margin:4px 0;font-size:12px}a{color:var(--ac)}h4{margin:14px 0 4px}</style></head><body>
<header><b>SelfDev</b><span id="net" class="mut"></span><button class="tab on" id="t1" onclick="tab(1)">چت</button><button class="tab" id="t2" onclick="tab(2)">مدل‌ها</button></header>
<main id="chat"></main><main id="mods" style="display:none"></main>
<form id="f"><input id="q" placeholder="بنویس: یک بازی مار بساز..." autocomplete="off"><button>ارسال</button></form>
<script>
const $=s=>document.querySelector(s),api=(p,b)=>fetch(p,{method:'POST',body:JSON.stringify(b||{})}).then(r=>r.json());
const esc=s=>String(s??'').replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
let cur=1;function tab(n){cur=n;$('#chat').style.display=n==1?'':'none';$('#mods').style.display=n==2?'':'none';$('#f').style.display=n==1?'':'none';$('#t1').className='tab'+(n==1?' on':'');$('#t2').className='tab'+(n==2?' on':'');refresh()}
$('#f').onsubmit=e=>{e.preventDefault();const v=$('#q').value.trim();if(!v)return;$('#q').value='';api('/api/chat',{msg:v}).then(refresh)};
const ST={queued:'در صف…',running:'در حال کار…',needs_approval:'منتظر تایید تو',failed:'ناموفق',done:''};
function link(t){return esc(t).replace(/outputs\/([\w\-. ]+)/g,(m,f)=>`<a href="/files/${encodeURIComponent(f)}" target="_blank">${m}</a>`)}
async function chat(){const s=await fetch('/api/state').then(r=>r.json());
 $('#net').textContent=(s.online?'آنلاین':'آفلاین')+(s.advisor?' · ۲ مغز':'');
 let h=`<div class="mut" style="text-align:center">${s.paused?'⏸ صف متوقف است':''} <button class="g" onclick="api('/api/pause',{paused:${!s.paused}}).then(refresh)">${s.paused?'ادامه صف':'توقف صف'}</button></div>`;
 for(const j of s.jobs){h+=`<div class="msg u">${esc(j.msg)}</div><div class="msg a"><small>${ST[j.status]||''}</small>${j.result?'\n'+link(j.result):''}`;
  if(j.status=='needs_approval')h+=`\nبرای ادامه نصب «${esc(j.approval.item)}» (${j.approval.kind}) لازم است.<div><button onclick="api('/api/approve',{id:${j.id}}).then(refresh)">تایید</button> <button class="g" onclick="api('/api/deny',{id:${j.id}}).then(refresh)">رد</button></div>`;
  if(j.log&&j.log.length)h+=`<details><summary class="mut">گزارش کار</summary><pre>${esc(j.log.join('\n'))}</pre></details>`;h+='</div>'}
 const c=$('#chat'),b=c.scrollTop+c.clientHeight>=c.scrollHeight-60;c.innerHTML=h||'<p class="mut">هنوز چیزی نیست.</p>';if(b)c.scrollTop=c.scrollHeight}
async function mods(){const m=await fetch('/api/models').then(r=>r.json()),R=m.reg;let h='';
 if(!m.llama)h+='<div class="row">⚠ llama-server پیدا نشد. در Termux: <code>pkg install llama-cpp</code></div>';
 for(const t of m.tasks.slice().reverse())h+=`<div class="row"><b>${esc(t.name)}</b> <small>${t.status}</small> ${esc(t.msg)}<div class="bar"><i style="width:${t.progress}%"></i></div></div>`;
 h+='<h4>مدل‌های روی گوشی</h4>';const ids=Object.keys(R.models);if(!ids.length)h+='<p class="mut">هنوز مدلی نیست. از پایین دانلود کن یا فایل .gguf را در پوشه‌ی models بگذار.</p>';
 for(const id of ids){const x=R.models[id];h+=`<div class="row"><b>${esc(id)}</b> <small>${x.size_mb||'?'}MB</small> ${R.active==id?'<span class="badge">مغز</span>':''} ${R.advisor==id?'<span class="badge" style="background:#8e44ad">مشاور</span>':''}
 <br><small>امتیاز: ${x.score??'—'}/100 · سرعت تقریبی: ${x.tps??'—'} tok/s</small><div>
 <button onclick="act('use',{role:'brain',id:'${id}'})">مغز</button><button onclick="act('use',{role:'advisor',id:'${id}'})">مشاور</button>
 <button class="g" onclick="act('benchmark',{id:'${id}'})">بنچمارک</button><button class="r" onclick="if(confirm('حذف؟'))act('delete',{id:'${id}'})">حذف</button></div></div>`}
 if(ids.length)h+=`<div class="row"><b>انتخاب خودکار</b><br><small>همه را تست می‌کند؛ بهترین مغز و دومی مشاور می‌شود.</small><div><button onclick="act('autopick',{council:true})">تست همه + دو مغز</button><button class="g" onclick="act('autopick',{council:false})">تست همه + فقط یک مغز</button>${R.advisor?`<button class="g" onclick="act('use',{role:'advisor',id:''})">حذف مشاور</button>`:''}</div></div>`;
 h+='<h4>کاتالوگ</h4>';for(const c of m.catalog)h+=`<div class="row"><b>${esc(c.name)}</b> <small>~${c.size_mb}MB</small><br><small>${esc(c.notes||'')}</small><div>${c.downloaded?'<small>دانلود شده</small>':`<button onclick="act('download',{spec:'${c.id}'})">دانلود</button>`}</div></div>`;
 h+=`<h4>دانلود با لینک مستقیم (.gguf)</h4><div class="row"><div style="margin:0"><input id="u" dir="ltr" placeholder="https://.../model.gguf"><button onclick="act('download',{spec:$('#u').value})">دانلود</button></div><small>اگر قطع شد، دوباره بزن؛ از همان‌جا ادامه می‌دهد.</small>${window.Android?'<div><button class="g" onclick="Android.pickModel()">وارد کردن فایل .gguf از حافظه گوشی</button></div>':''}</div>
 <h4>مشاور آنلاین (اختیاری)</h4><div class="row"><small>وقتی اینترنت هست، یک مدل قوی‌تر مشاور شود. کلید روی همین گوشی در models.json ذخیره می‌شود.</small><div><input id="ru" dir="ltr" placeholder="آدرس .../v1/chat/completions" value="${esc(R.remote_advisor?.url||'')}"><input id="rm" dir="ltr" placeholder="نام مدل"><input id="rk" dir="ltr" type="password" placeholder="API key"></div><div><button onclick="act('remote',{url:$('#ru').value,model:$('#rm').value,key:$('#rk').value})">ذخیره</button><button class="g" onclick="act('remote',{url:''})">حذف</button></div></div>`;
 if(!document.activeElement||document.activeElement.tagName!='INPUT')$('#mods').innerHTML=h}
async function act(a,b){const r=await api('/api/models/'+a,b);if(!r.ok)alert(r.error);refresh()}
function refresh(){(cur==1?chat:mods)()}refresh();setInterval(refresh,2500);
</script></body></html>"""

def main():
    models.sync()
    if models.reg()["active"]:
        bg("بارگذاری مدل", with_gpu(lambda prog: models.apply()))
    threading.Thread(target=worker, daemon=True).start()
    print("http://127.0.0.1:8000")
    ThreadingHTTPServer(("127.0.0.1", 8000), H).serve_forever()


if __name__ == "__main__":
    main()
