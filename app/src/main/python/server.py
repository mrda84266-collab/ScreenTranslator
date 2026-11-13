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

    def file(self, base, name, default="application/octet-stream"):
        f = base / Path(name).name
        if f.is_file():
            return self.send(f.read_bytes(), mimetypes.guess_type(f.name)[0] or default)
        self.send({"error": "not found"}, code=404)

    def do_GET(self):
        p = self.path.split("?")[0]
        if p == "/":
            return self.send(PAGE, "text/html")
        if p == "/manifest.json":
            return self.send({"name": "NEXORA", "short_name": "NEXORA", "start_url": "/", "display": "standalone",
                              "background_color": "#111", "theme_color": "#111",
                              "icons": [{"src": "/icon.svg", "sizes": "any", "type": "image/svg+xml"}]})
        if p == "/icon.svg":
            return self.send('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64"><rect width="64" height="64" rx="14" fill="#1d6fe8"/><text x="32" y="43" font-size="30" text-anchor="middle" fill="#fff">N</text></svg>', "image/svg+xml")
        if p == "/api/state":
            with LOCK:
                tl = [{"name": k, "desc": v.split(" | ")[0]} for k, v in agent.tools().items()]
                return self.send({"jobs": state["jobs"][-40:], "paused": state["paused"], "online": is_online(),
                                  "tools": tl, "brain": models.status()})
        if p == "/api/models":
            r = models.sync()
            return self.send({"reg": {k: r[k] for k in ("mode", "main", "max_loaded")}, "profiles": models.plist(),
                              "catalog": models.catalog(), "tasks": tasks[-8:], "modes": list(models.MODES), "tags": list(models.TAGS),
                              "llama": bool(shutil.which(models.LLAMA) or Path(models.LLAMA).exists())})
        if p == "/api/ext":
            d = agent.ROOT / "ui_ext"
            on = [{"name": f.stem, "code": f.read_text(encoding="utf-8")} for f in sorted(d.glob("*.js"))] if d.exists() else []
            off = [f.name[:-7] for f in sorted(d.glob("*.js.off"))] if d.exists() else []
            return self.send({"ext": on, "disabled": off, "errors": agent.load(d / "errors.json", {}) if d.exists() else {}})
        if p.startswith("/engines/"):
            return self.file(agent.WORK / "engines", p[9:], "application/javascript")
        if p.startswith("/packs/"):
            parts = p[7:].split("/", 1)
            if len(parts) == 2:
                return self.file(agent.WORK / "packs" / Path(parts[0]).name, parts[1], "application/javascript")
        if p.startswith("/files/"):
            return self.file(agent.WORK / "outputs", p[7:])
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
            elif p == "/api/models/benchmark":
                bg("بنچمارک " + d["id"], with_gpu(lambda prog: models.benchmark(d["id"])))
            elif p == "/api/models/autopick":
                bg("بنچمارک همه + انتخاب بهترین", with_gpu(lambda prog: models.autopick(prog, d.get("council", True))))
            elif p == "/api/models/preload":
                bg("بارگذاری مدل اصلی", with_gpu(lambda prog: models.apply()))
            elif p == "/api/profiles/main":
                models.set_main(d["id"])
            elif p == "/api/profiles/toggle":
                models.set_enabled(d["id"], d["enabled"])
            elif p == "/api/profiles/tags":
                models.set_tags(d["id"], d["tags"])
            elif p == "/api/profiles/add":
                models.add_profile(d["name"], d["url"], d.get("key", ""), d.get("model", ""), d.get("tags") or ["chat"])
            elif p == "/api/profiles/remove":
                models.remove_profile(d["id"])
            elif p == "/api/mode":
                models.set_mode(d["mode"])
            elif p == "/api/max_loaded":
                models.set_max_loaded(d["n"])
            elif p == "/api/ext_error":
                ext_disable(d["name"], d.get("error", ""))
            elif p == "/api/ext_reset":
                for f in (agent.ROOT / "ui_ext").glob("*.js"):
                    ext_disable(f.stem, "reset")
            else:
                return self.send({"error": "not found"}, code=404)
            self.send({"ok": True})
        except Exception as e:
            self.send({"ok": False, "error": str(e)}, code=400)


def ext_disable(name, err):
    d = agent.ROOT / "ui_ext"
    f = d / (Path(name).name + ".js")
    if f.exists():
        f.rename(f.with_name(f.name + ".off"))
    errs = agent.load(d / "errors.json", {})
    errs[Path(name).name] = str(err)[:300]
    (d / "errors.json").write_text(json.dumps(errs, ensure_ascii=False))


PAGE = r"""<!doctype html><html lang="fa" dir="rtl"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><link rel="manifest" href="/manifest.json">
<title>NEXORA</title><style>
:root{color-scheme:light dark;--bg:#fff;--fg:#111;--mut:#777;--card:#f2f3f5;--ac:#1d6fe8}
@media(prefers-color-scheme:dark){:root{--bg:#121212;--fg:#eee;--card:#222;--mut:#999}}
*{box-sizing:border-box}body{margin:0;font:15px system-ui,Tahoma;background:var(--bg);color:var(--fg);display:flex;flex-direction:column;height:100dvh}
header{display:flex;gap:6px;align-items:center;padding:8px 10px;border-bottom:1px solid var(--card);overflow-x:auto;white-space:nowrap}
header b{margin-inline-end:auto}.tab{padding:6px 12px;border-radius:14px;background:var(--card);border:0;color:var(--fg)}.tab.on{background:var(--ac);color:#fff}
main{flex:1;overflow:auto;padding:10px}.msg{max-width:92%;padding:8px 11px;border-radius:14px;margin:6px 0;white-space:pre-wrap;word-break:break-word}
.u{background:var(--ac);color:#fff;margin-inline-start:auto}.a{background:var(--card)}small,.mut{color:var(--mut)}
form{display:flex;gap:6px;padding:8px;border-top:1px solid var(--card)}input,textarea,select{flex:1;padding:10px;border-radius:12px;border:1px solid var(--card);background:var(--card);color:var(--fg);font:inherit;min-width:0}
button{font:inherit;padding:8px 12px;border-radius:12px;border:0;background:var(--ac);color:#fff}button.g{background:var(--card);color:var(--fg)}button.r{background:#c0392b}button.s{padding:3px 9px;font-size:12px}
.row{background:var(--card);border-radius:12px;padding:10px;margin:8px 0}.row>div{display:flex;gap:6px;flex-wrap:wrap;align-items:center;margin-top:6px}
.badge{background:var(--ac);color:#fff;border-radius:8px;padding:1px 7px;font-size:12px}.bar{height:6px;background:var(--bg);border-radius:4px;overflow:hidden}.bar i{display:block;height:100%;background:var(--ac)}
details{margin-top:4px}pre{white-space:pre-wrap;margin:4px 0;font-size:12px}a{color:var(--ac)}h4{margin:14px 0 4px}
#tools{display:flex;gap:6px;overflow-x:auto;padding:4px 8px;border-top:1px solid var(--card)}#tools button{white-space:nowrap}.warn{background:#c0392b22;border:1px solid #c0392b}</style></head><body>
<header><b>NEXORA</b><span id="net" class="mut"></span><span id="tabs"><button class="tab on" id="t1" onclick="tab(1)">چت</button> <button class="tab" id="t2" onclick="tab(2)">مدل‌ها</button></span></header>
<main id="chat"></main><main id="mods" style="display:none"></main>
<div id="tools"></div>
<form id="f"><span id="chips"></span><button type="button" id="att" class="g" style="display:none" onclick="Android.pickFile()">📎</button><input id="q" placeholder="بنویس: سلام / یک بازی مار بساز..." autocomplete="off"><button>ارسال</button></form>
<script>
const $=s=>document.querySelector(s),api=(p,b)=>fetch(p,{method:'POST',body:JSON.stringify(b||{})}).then(r=>r.json());
const esc=s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const post=async(p,b)=>{const r=await api(p,b);if(!r.ok)alert(r.error);refresh()};
let cur=1;const exts=[];
function tab(n){cur=n;$('#chat').style.display=n==1?'':'none';$('#mods').style.display=n==2?'':'none';$('#f').style.display=n==1?'':'none';$('#tools').style.display=n==1?'':'none';
 $('#t1').className='tab'+(n==1?' on':'');$('#t2').className='tab'+(n==2?' on':'');
 for(const x of exts){x.m.style.display=n==x.id?'':'none';x.b.className='tab'+(n==x.id?' on':'')}
 const x=exts.find(e=>e.id==n);if(x){try{x.fn(x.m)}catch(e){reportErr(x.name,e)}}else refresh()}
let atts=[];if(window.Android)$('#att').style.display='';
function onAttached(n){atts.push(n);$('#chips').innerHTML=atts.map(x=>`<small class="badge">${esc(x.slice(0,14))}</small>`).join(' ')}
$('#f').onsubmit=e=>{e.preventDefault();let v=$('#q').value.trim();if(!v)return;v+=atts.map(n=>'\n[فایل پیوست: uploads/'+n+']').join('');atts=[];$('#chips').innerHTML='';$('#q').value='';api('/api/chat',{msg:v}).then(refresh)};
const ST={queued:'در صف…',running:'در حال کار…',needs_approval:'منتظر تایید تو',failed:'ناموفق',done:''};
function link(t){return esc(t).replace(/outputs\/([\w\-. ]+)/g,(m,f)=>`<a href="/files/${encodeURIComponent(f)}" target="_blank">${m}</a>${window.Android?` <button class="g s" onclick="Android.save('${f}')">💾</button>`:''}`)}
async function chat(){const s=await fetch('/api/state').then(r=>r.json());const B=s.brain;
 $('#net').textContent=(s.online?'آنلاین':'آفلاین')+' · '+B.mode;
 let h=`<div class="row ${B.main?'':'warn'}"><small>مغز: <b>${esc(B.main||'—')}</b> · ${esc(B.msg)}</small>${B.main?'':`<div><button onclick="tab(2)">رفتن به مدل‌ها</button></div>`}</div>`;
 h+=`<div class="mut" style="text-align:center">${s.paused?'⏸ صف متوقف است':''} <button class="g s" onclick="post('/api/pause',{paused:${!s.paused}})">${s.paused?'ادامه صف':'توقف صف'}</button></div>`;
 for(const j of s.jobs){h+=`<div class="msg u">${esc(j.msg)}</div><div class="msg a"><small>${ST[j.status]||''}</small>${j.result?'\n'+link(j.result):''}`;
  if(j.status=='needs_approval')h+=`\nبرای ادامه نصب «${esc(j.approval.item)}» (${j.approval.kind}) لازم است.<div><button onclick="post('/api/approve',{id:${j.id}})">تایید</button> <button class="g" onclick="post('/api/deny',{id:${j.id}})">رد</button></div>`;
  if(j.log&&j.log.length)h+=`<details><summary class="mut">فعالیت مدل‌ها و مراحل</summary><pre>${esc(j.log.join('\n'))}</pre></details>`;h+='</div>'}
 const c=$('#chat'),b=c.scrollTop+c.clientHeight>=c.scrollHeight-60;c.innerHTML=h;if(b)c.scrollTop=c.scrollHeight;
 $('#tools').innerHTML=s.tools.map(t=>`<button class="g s" title="${esc(t.desc)}" onclick="$('#q').value='از ابزار ${esc(t.name)} استفاده کن: ';$('#q').focus()">🛠 ${esc(t.name)}</button>`).join('')}
let PL=[];
async function mods(){const m=await fetch('/api/models').then(r=>r.json());PL=m.profiles;let h='';
 if(!m.llama)h+='<div class="row warn">⚠ llama-server پیدا نشد؛ فقط مدل‌های API کار می‌کنند.</div>';
 for(const t of m.tasks.slice().reverse())h+=`<div class="row ${t.status=='failed'?'warn':''}"><b>${esc(t.name)}</b> <small>${t.status}</small> ${esc(t.msg)}<div class="bar"><i style="width:${t.progress}%"></i></div></div>`;
 const ML={single:'تک‌مدل',auto:'خودکار',specialist:'متخصص (بر اساس برچسب)',parallel:'موازی (همه جواب بدهند)',debate:'بحث و نقد',consensus:'اجماع'};
 h+=`<div class="row"><b>حالت چندمدلی</b><div><select onchange="post('/api/mode',{mode:this.value})">${m.modes.map(x=>`<option value="${x}" ${x==m.reg.mode?'selected':''}>${ML[x]}</option>`).join('')}</select></div>
 <div><small>حداکثر مدل محلیِ هم‌زمان در RAM:</small><select onchange="post('/api/max_loaded',{n:+this.value})">${[1,2,3,4].map(n=>`<option ${n==m.reg.max_loaded?'selected':''}>${n}</option>`).join('')}</select></div></div>`;
 h+='<h4>پروفایل‌های مدل</h4>';if(!PL.length)h+='<p class="mut">مدلی نیست. از پایین دانلود کن، فایل .gguf وارد کن، یا مدل API اضافه کن.</p>';
 PL.forEach((p,i)=>{h+=`<div class="row"><b>${esc(p.name)}</b> <span class="badge" style="background:${p.provider=='api'?'#8e44ad':'#16a085'}">${p.provider=='api'?'API':'محلی'}</span> ${p.main?'<span class="badge">اصلی</span>':''} ${p.loaded?'<span class="badge" style="background:#27ae60">در RAM</span>':''} ${p.enabled?'':'<small>(غیرفعال)</small>'}
 <br><small>${p.provider=='local'?`${p.size_mb||'?'}MB · امتیاز ${p.score??'—'}/100 · سرعت ~${p.tps??'—'} tok/s`:esc(p.url)}</small>
 <div>${m.tags.map(t=>`<button class="s ${p.tags.includes(t)?'':'g'}" onclick="tag(${i},'${t}')">${t}</button>`).join('')}</div>
 <div><button onclick="post('/api/profiles/main',{id:PL[${i}].id})">اصلی</button><button class="g" onclick="post('/api/profiles/toggle',{id:PL[${i}].id,enabled:${!p.enabled}})">${p.enabled?'غیرفعال':'فعال'}</button>
 ${p.provider=='local'?`<button class="g" onclick="post('/api/models/benchmark',{id:PL[${i}].id})">بنچمارک</button><button class="r" onclick="if(confirm('فایل مدل حذف شود؟'))post('/api/models/delete',{id:PL[${i}].id})">حذف</button>`:`<button class="r" onclick="post('/api/profiles/remove',{id:PL[${i}].id})">حذف</button>`}</div></div>`});
 h+=`<div class="row"><b>انتخاب خودکار</b><br><small>همه‌ی مدل‌های محلی تست می‌شوند؛ بهترین اصلی می‌شود.</small><div><button onclick="post('/api/models/autopick',{council:true})">تست همه + بحث و نقد</button><button class="g" onclick="post('/api/models/autopick',{council:false})">تست همه + تک‌مدل</button><button class="g" onclick="post('/api/models/preload')">بارگذاری مدل اصلی</button></div></div>`;
 h+='<h4>کاتالوگ دانلود</h4>';for(const c of m.catalog)h+=`<div class="row"><b>${esc(c.name)}</b> <small>~${c.size_mb}MB</small><br><small>${esc(c.notes||'')}</small><div>${c.downloaded?'<small>دانلود شده</small>':`<button onclick="post('/api/models/download',{spec:'${c.id}'})">دانلود</button>`}</div></div>`;
 h+=`<h4>دانلود با لینک مستقیم (.gguf)</h4><div class="row"><div style="margin:0"><input id="u" dir="ltr" placeholder="https://.../model.gguf"><button onclick="post('/api/models/download',{spec:$('#u').value})">دانلود</button></div><small>اگر قطع شد، دوباره بزن؛ از همان‌جا ادامه می‌دهد.</small>${window.Android?'<div><button class="g" onclick="Android.pickModel()">وارد کردن فایل .gguf از حافظه گوشی</button></div>':''}</div>
 <h4>افزودن مدل API (OpenAI / Ollama / llama.cpp / هر سرویس سازگار)</h4><div class="row"><small>کلید فقط در حافظه‌ی خصوصی همین اپ ذخیره می‌شود (رمزنگاری نشده).</small>
 <div><input id="an" placeholder="نام"><input id="au" dir="ltr" placeholder="http://host:11434/v1"></div><div><input id="am" dir="ltr" placeholder="نام مدل (مثلا qwen2.5:7b)"><input id="ak" dir="ltr" type="password" placeholder="API key (اختیاری)"></div>
 <div><button onclick="post('/api/profiles/add',{name:$('#an').value,url:$('#au').value,model:$('#am').value,key:$('#ak').value,tags:['chat','code']})">افزودن</button></div></div>
 <h4>افزونه‌های رابط</h4><div class="row"><small>اگر افزونه‌ای صفحه را خراب کرد:</small><div><button class="r" onclick="post('/api/ext_reset').then(()=>location.reload())">غیرفعال کردن همه</button></div></div>`;
 const a=document.activeElement;if(!a||!['INPUT','SELECT'].includes(a.tagName))$('#mods').innerHTML=h}
function tag(i,t){const p=PL[i];const s=new Set(p.tags);s.has(t)?s.delete(t):s.add(t);post('/api/profiles/tags',{id:p.id,tags:[...s]})}
// ---- افزونه‌های رابط (UI را عامل می‌تواند گسترش دهد؛ هر افزونه ایزوله است و با خطا خودکار غیرفعال می‌شود) ----
let curExt='';
function reportErr(name,err){api('/api/ext_error',{name,error:String(err)})}
const ext={post:api,addTab(title,fn){const name=curExt,id='x'+exts.length,m=document.createElement('main'),b=document.createElement('button');
 m.id=id;m.style.display='none';document.body.insertBefore(m,$('#tools'));b.className='tab';b.textContent=title;b.onclick=()=>tab(id);$('#tabs').appendChild(document.createTextNode(' '));$('#tabs').appendChild(b);exts.push({id,m,b,fn,name})}};
async function loadExt(){try{if(localStorage.extLoading)await api('/api/ext_reset');localStorage.extLoading='1'}catch(e){}
 try{const l=await fetch('/api/ext').then(r=>r.json());for(const e of l.ext){curExt=e.name;try{new Function('ext',e.code)(ext)}catch(err){reportErr(e.name,err)}}
  const dis=Object.entries(l.errors||{}).filter(([k])=>l.disabled.includes(k));if(dis.length&&!sessionStorage.seenExt){sessionStorage.seenExt='1';alert('افزونه غیرفعال شد: '+dis.map(([k,v])=>k+' ('+v+')').join('\n'))}}catch(e){}
 try{localStorage.removeItem('extLoading')}catch(e){}}
function refresh(){if(cur==1)chat();else if(cur==2)mods()}
loadExt();refresh();setInterval(refresh,2500);
</script></body></html>"""


def main():
    models.sync()
    try:
        agent.seed()
    except Exception as e:
        print("seed:", e)
    if models.reg()["main"] in models.reg()["models"]:
        bg("بارگذاری مدل اصلی", with_gpu(lambda prog: models.apply()))
    threading.Thread(target=worker, daemon=True).start()
    print("http://127.0.0.1:8000")
    ThreadingHTTPServer(("127.0.0.1", 8000), H).serve_forever()


if __name__ == "__main__":
    main()
