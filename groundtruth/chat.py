"""A chat page on the server, so the people it's for can use it with no client to set up.

Connecting Copilot Studio, Claude, or ChatGPT takes someone clicking through
that product. With ``GT_CHAT=1`` and an ``ANTHROPIC_API_KEY`` on the server,
``/chat`` is a small web chat instead, ready the moment it's deployed. Each
question runs through Claude with exactly the tools MCP clients get and the same
instructions (``./gt prompt``), so it answers the way your evals grade, and every
answer lists the sources its tools returned.

The page is public on purpose: it's for your users, and it never sees the MCP API
key. So it limits each visitor (``GT_CHAT_PER_MINUTE``, default 6), the whole
server (``GT_CHAT_PER_DAY``, default 2000), and the length of a question and a
conversation. Its tool calls are logged like any other, so ``./gt report`` shows
what people ask.
"""
from __future__ import annotations

import html
import json
import os
import threading
import time
from collections import defaultdict, deque
from typing import Any

from groundtruth import ingest, llm
from groundtruth.project import Project

MAX_QUESTION = 2000
MAX_HISTORY = 8  # earlier turns sent back to the model
MAX_BODY = 64_000


def enabled() -> bool:
    return os.environ.get("GT_CHAT", "0") == "1" and llm.available()


class Limits:
    """A per-visitor rate (per minute) and a server-wide daily cap, in memory."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._recent: dict[str, deque] = defaultdict(deque)
        self._day = ("", 0)

    def allow(self, visitor: str, now: float | None = None) -> str | None:
        """None if this question may run; otherwise why not."""
        now = time.time() if now is None else now
        per_minute = int(os.environ.get("GT_CHAT_PER_MINUTE") or 6)
        per_day = int(os.environ.get("GT_CHAT_PER_DAY") or 2000)
        today = time.strftime("%Y-%m-%d", time.gmtime(now))
        with self._lock:
            day, count = self._day if self._day[0] == today else (today, 0)
            if count >= per_day:
                return "The assistant has answered all it can for today. Please try again tomorrow."
            recent = self._recent[visitor]
            while recent and now - recent[0] > 60:
                recent.popleft()
            if len(recent) >= per_minute:
                return "That's a lot of questions at once. Please wait a minute and try again."
            recent.append(now)
            self._day = (day, count + 1)
        return None


LIMITS = Limits()


def _url_of_capture(project: Project, rel: str) -> str | None:
    meta = ingest.page_meta(project.root / rel)
    url = meta.get("source_url")
    return str(url) if url and str(url).startswith(("http://", "https://")) else None


def sources(project: Project, calls: list[dict]) -> list[dict[str, str]]:
    """What the answer was built from, as links people can open: fact sources and the pages read."""
    out: dict[str, str] = {}
    for call in calls:
        result = call.get("result")
        if call["name"] == "read_wiki_page" and isinstance(result, dict) and result.get("title"):
            for rel in result.get("sources") or []:
                url = _url_of_capture(project, str(rel))
                if url:
                    out.setdefault(url, str(result["title"]))
        elif isinstance(result, dict):
            for row in result.get("results") or []:
                url = str(row.get("source") or "")
                if not url.startswith(("http://", "https://")) and row.get("source_file"):
                    url = _url_of_capture(project, str(row["source_file"])) or ""
                if url.startswith(("http://", "https://")):
                    out.setdefault(url, url)
    return [{"url": u, "title": t} for u, t in list(out.items())[:8]]


def answer(project: Project, question: str, history: list[dict[str, str]], gate=None) -> dict[str, Any]:
    from groundtruth import server
    turns = [{"role": h["role"], "content": str(h["content"])[:MAX_QUESTION]} for h in history[-MAX_HISTORY:]
             if isinstance(h, dict) and h.get("role") in ("user", "assistant") and h.get("content")]
    out = llm.agent(question, server.instructions(project), server.tool_specs(project),
                    lambda name, args: server.call_tool(project, name, args, gate=gate),
                    history=turns, max_turns=8)
    return {"answer": out["response"], "sources": sources(project, out["tool_calls"])}


def handle(project: Project, body: bytes, visitor: str, gate=None) -> tuple[int, dict[str, Any]]:
    """POST /chat/ask: {"question": "...", "history": [{"role", "content"}...]} -> (status, reply)."""
    if len(body) > MAX_BODY:
        return 413, {"error": "That conversation is too long. Start a new one."}
    try:
        data = json.loads(body or b"{}")
    except ValueError:
        return 400, {"error": "Send JSON: {\"question\": \"...\"}"}
    question = str(data.get("question") or "").strip()
    if not question:
        return 400, {"error": "Ask a question."}
    if len(question) > MAX_QUESTION:
        return 413, {"error": f"Please keep a question under {MAX_QUESTION} characters."}
    why = LIMITS.allow(visitor)
    if why:
        return 429, {"error": why}
    try:
        return 200, answer(project, question, data.get("history") or [], gate)
    except llm.LLMUnavailable as exc:
        return 503, {"error": f"The assistant isn't available right now ({exc})."}
    except Exception:  # noqa: BLE001 - never leak a trace to a visitor
        return 500, {"error": "Something went wrong. Please try again."}


def page(project: Project) -> str:
    name = html.escape(project.name)
    about = html.escape(project.about or f"Ask about {project.name}.")
    return PAGE.replace("{{NAME}}", name).replace("{{ABOUT}}", about)


PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{{NAME}}</title>
<style>
:root{--bg:#fff;--fg:#1a1a1a;--muted:#666;--line:#e3e3e3;--me:#eef3ff;--accent:#2952cc}
@media (prefers-color-scheme:dark){:root{--bg:#141414;--fg:#eee;--muted:#aaa;--line:#333;--me:#1e2740;--accent:#8aa8ff}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:16px/1.5 system-ui,sans-serif}
main{max-width:720px;margin:0 auto;padding:16px;display:flex;flex-direction:column;min-height:100vh}
header h1{font-size:1.25rem;margin:8px 0 0}header p{color:var(--muted);margin:4px 0 16px}
#log{flex:1;display:flex;flex-direction:column;gap:12px}
.msg{padding:10px 14px;border-radius:12px;border:1px solid var(--line);white-space:pre-wrap;overflow-wrap:anywhere}
.me{background:var(--me);align-self:flex-end;max-width:85%}.bot{align-self:flex-start;max-width:100%}
.src{font-size:.85rem;color:var(--muted);margin-top:6px}.src a{color:var(--accent)}
form{display:flex;gap:8px;position:sticky;bottom:0;background:var(--bg);padding:12px 0}
textarea{flex:1;resize:none;font:inherit;padding:10px;border-radius:10px;border:1px solid var(--line);
background:var(--bg);color:var(--fg)}button{font:inherit;padding:0 18px;border-radius:10px;border:0;
background:var(--accent);color:#fff;cursor:pointer}button:disabled{opacity:.5}
</style></head><body><main>
<header><h1>{{NAME}}</h1><p>{{ABOUT}}</p></header>
<div id="log" aria-live="polite"></div>
<form id="f"><label for="q" hidden>Your question</label>
<textarea id="q" rows="2" maxlength="2000" placeholder="Ask a question" required></textarea>
<button id="go">Ask</button></form></main>
<script>
const log=document.getElementById("log"),f=document.getElementById("f"),q=document.getElementById("q"),go=document.getElementById("go");
const history=[];
function add(cls,text,sources){const d=document.createElement("div");d.className="msg "+cls;d.textContent=text;
 if(sources&&sources.length){const s=document.createElement("div");s.className="src";s.append("Sources: ");
  sources.forEach((x,i)=>{if(!/^https?:\\/\\//.test(x.url))return;const a=document.createElement("a");a.href=x.url;
   a.textContent=x.title||x.url;a.target="_blank";a.rel="noopener";if(i)s.append(" · ");s.append(a)});d.append(s)}
 log.append(d);d.scrollIntoView({block:"end"});return d}
f.addEventListener("submit",async e=>{e.preventDefault();const question=q.value.trim();if(!question)return;
 add("me",question);q.value="";go.disabled=true;const wait=add("bot","…");
 try{const r=await fetch("chat/ask",{method:"POST",headers:{"Content-Type":"application/json"},
   body:JSON.stringify({question,history})});const data=await r.json();wait.remove();
  if(!r.ok){add("bot",data.error||"Something went wrong.");return}
  add("bot",data.answer,data.sources);history.push({role:"user",content:question},{role:"assistant",content:data.answer});
 }catch(err){wait.remove();add("bot","Couldn't reach the assistant. Please try again.")}finally{go.disabled=false;q.focus()}});
q.addEventListener("keydown",e=>{if(e.key==="Enter"&&!e.shiftKey){e.preventDefault();f.requestSubmit()}});
</script></body></html>
"""
