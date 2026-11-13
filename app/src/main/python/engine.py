"""NEXORA - Multi-LLM Engine: single / auto / specialist / parallel / debate / consensus."""
import re
import time
from concurrent.futures import ThreadPoolExecutor

import agent
import models

CRITIQUE = "Question:\n{q}\n\nDraft answer:\n{a}\n\nList mistakes or missing points briefly (under 100 words)."
REVISE = "Question:\n{q}\n\nDraft:\n{a}\n\nReviewer notes:\n{c}\n\nWrite the final improved answer in the user's language."
CONSENSUS = "Question:\n{q}\n\nAnswers from several assistants:\n{answers}\n\nCompare them, keep what is correct, fix disagreements, and write ONE best final answer in the user's language."


def classify(prompt):
    if re.search(r"کد|برنامه|تابع|باگ|اسکریپت|python|code|function|bug|script|html|javascript|class ", prompt, re.I):
        return "code"
    if re.search(r"تحقیق|جستجو|مقایسه|search|research|compare", prompt, re.I):
        return "research"
    return "chat"


def _name(pid):
    return next((p["name"] for p in models.plist() if p["id"] == pid), pid)


def _ask(pid, prompt):
    t = time.time()
    out = agent.llm(prompt, role=pid)
    print(f"[{_name(pid)}] پاسخ داد ({len(out)} حرف، {time.time() - t:.0f} ثانیه)")
    return out


def _pick(plist, task, main):
    tagged = [p["id"] for p in plist if p["enabled"] and task in p["tags"]]
    return main if main in tagged or not tagged else tagged[0]


def _all(ids, prompt):
    """مدل‌های API هم‌زمان (thread)، مدل‌های محلی پشت‌سرهم (RAM)."""
    remote = [i for i in ids if i.startswith("r:")]
    res = {}
    with ThreadPoolExecutor(max_workers=max(1, len(remote))) as ex:
        futs = {i: ex.submit(_ask, i, prompt) for i in remote}
        for i in ids:
            if not i.startswith("r:"):
                res[i] = _ask(i, prompt)
        for i, f in futs.items():
            res[i] = f.result()
    return [(i, res[i]) for i in ids]


def answer(prompt, task=None, mode=None):
    r = models.reg()
    mode = mode or r["mode"]
    task = task or classify(prompt)
    pl = models.plist()
    ids = [p["id"] for p in pl if p["enabled"]]
    main = r["main"] if r["main"] in ids else (ids[0] if ids else None)
    if not main:
        return agent.llm(prompt)  # بدون پروفایل: آدرس پیش‌فرض (LLM_URL)
    if mode == "auto":
        mode = "single" if len(ids) < 2 else ("specialist" if task in ("code", "research") else ("debate" if len(prompt) > 400 else "single"))
        print(f"حالت خودکار: {mode} (نوع کار: {task})")
    if mode == "specialist":
        return _ask(_pick(pl, task, main), prompt)
    if mode == "parallel":
        return "\n\n".join(f"— {_name(i)} —\n{t}" for i, t in _all(ids, prompt))
    if mode == "consensus":
        outs = _all(ids, prompt)
        if len(outs) == 1:
            return outs[0][1]
        return _ask(main, CONSENSUS.format(q=prompt, answers="\n\n".join(f"[{_name(i)}]\n{t}" for i, t in outs)))
    if mode == "debate":
        others = [i for i in ids if i != main]
        ans = _ask(main, prompt)
        for k in range(2 if others else 1):
            critic = others[k % len(others)] if others else main
            crit = _ask(critic, CRITIQUE.format(q=prompt, a=ans))
            ans = _ask(main, REVISE.format(q=prompt, a=ans, c=crit))
        return ans
    return _ask(main, prompt)
