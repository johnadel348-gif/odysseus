#!/usr/bin/env python3
"""Odysseus Pocket — a tiny, Android-friendly slice of the Odysseus AI workspace.

One file. Three dependencies (starlette, uvicorn, httpx — all pure Python, so
they install anywhere with no compiler: Termux/Android included). No Docker,
no build step.

What it keeps from the full Odysseus app:
  * an agent chat loop with streaming-quality OpenAI-compatible backends
    (Ollama, LM Studio, llama.cpp server, llamafile, KoboldCpp, OpenAI, ...)
  * two tools the model can call on its own: web search + notes/memory
  * persistent chats, notes, and per-device preferences

What it deliberately drops: email, calendar, documents, gallery, MCP,
shell, cookbook, Docker, GPU profiles, and the 41k-line desktop UI.

Run it:   python server.py            (or ./run.sh — creates a venv for you)
Open it:  http://localhost:8000      on the phone that runs the server,
          http://<phone-ip>:8000     from any other device on the Wi-Fi.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.parse import parse_qs, unquote, urlparse

import httpx
import uvicorn
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, StreamingResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

# --------------------------------------------------------------------------
# Configuration (env vars, all optional — see .env.example / README.md)
# --------------------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
DATA_DIR = Path(os.environ.get("POCKET_DATA_DIR", BASE_DIR / "data")).resolve()

HOST = os.environ.get("POCKET_HOST", "0.0.0.0")
PORT = int(os.environ.get("POCKET_PORT", "8000"))

# Any OpenAI-compatible /v1 endpoint works. Default is Ollama on this device.
DEFAULT_BASE_URL = os.environ.get("POCKET_BASE_URL", "http://127.0.0.1:11434/v1")
DEFAULT_API_KEY = os.environ.get("POCKET_API_KEY", "")
DEFAULT_MODEL = os.environ.get("POCKET_MODEL", "")  # empty = auto-detect

# Search backend: "auto" tries DuckDuckGo, falls back to SerpApi if a key is
# set; "serpapi" or "duckduckgo" force one; "none" disables web search.
SEARCH_BACKEND = os.environ.get("POCKET_SEARCH_BACKEND", "auto")
SERPAPI_KEY = os.environ.get("SERPAPI_KEY", "")

# Optional shared secret. When set, every /api/* call must send it — the PWA
# asks for it once in Settings and stores it in localStorage. Recommended as
# soon as the server is reachable from other devices.
ACCESS_TOKEN = os.environ.get("POCKET_TOKEN", "")

MAX_TOOL_ROUNDS = 4          # agent loop safety cap (search/note rounds)
HISTORY_MESSAGE_CAP = 30     # messages of history sent to the model
CHAT_TIMEOUT_S = 240         # local models on phones can be slow
SEARCH_TIMEOUT_S = 15

DATA_DIR.mkdir(parents=True, exist_ok=True)

UA = ("Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126 Mobile Safari/537.36")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_id() -> str:
    return uuid.uuid4().hex[:12]


# --------------------------------------------------------------------------
# Tiny JSON storage (atomic writes, safe for phone flash memory)
# --------------------------------------------------------------------------

class JsonStore:
    """One JSON file in memory + atomic write-through to disk."""

    def __init__(self, path: Path, default: dict):
        self.path = path
        self.default = default
        self._lock = asyncio.Lock()
        self._cache: Optional[dict] = None

    def _read(self) -> dict:
        if self._cache is not None:
            return self._cache
        try:
            self._cache = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            self._cache = json.loads(json.dumps(self.default))
        return self._cache

    async def load(self) -> dict:
        async with self._lock:
            return self._read()

    async def save(self, data: dict) -> None:
        async with self._lock:
            self._cache = data
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1),
                           encoding="utf-8")
            os.replace(tmp, self.path)  # atomic on POSIX and Windows


chats_store = JsonStore(DATA_DIR / "chats.json", {"version": 1, "chats": []})
notes_store = JsonStore(DATA_DIR / "notes.json", {"version": 1, "notes": []})
prefs_store = JsonStore(DATA_DIR / "prefs.json", {"version": 1, "prefs": {}})


# --------------------------------------------------------------------------
# Settings (env defaults, overridable at runtime from the Settings sheet)
# --------------------------------------------------------------------------

class Settings:
    """Runtime settings. Prefs saved in the UI take precedence over env."""

    def __init__(self) -> None:
        self.base_url = DEFAULT_BASE_URL
        self.api_key = DEFAULT_API_KEY
        self.model = DEFAULT_MODEL
        self.system_prompt = ""
        self.tool_mode = True

    async def refresh(self) -> "Settings":
        prefs = (await prefs_store.load()).get("prefs", {})
        self.base_url = (prefs.get("backend") or DEFAULT_BASE_URL).rstrip("/")
        if "/v1" not in self.base_url:
            self.base_url = self.base_url.rstrip("/") + "/v1"
        saved_key = prefs.get("api_key")
        self.api_key = saved_key if saved_key else DEFAULT_API_KEY
        self.model = prefs.get("model") or DEFAULT_MODEL
        self.system_prompt = prefs.get("system_prompt") or ""
        self.tool_mode = bool(prefs.get("tool_mode", True))
        return self


settings = Settings()


# --------------------------------------------------------------------------
# Model catalog (auto-detect the model when none is configured)
# --------------------------------------------------------------------------

_models_cache: dict = {"ids": [], "ts": 0.0, "error": ""}


async def list_models() -> list[str]:
    """Fetch the backend's model catalog.

    The timeout is generous (15 s) on purpose: cloud catalogs like OpenRouter's
    are large JSON documents, and phones on mobile networks need the headroom.
    Failures are remembered so the UI can show the real reason.
    """
    _models_cache["error"] = ""
    if time.time() - _models_cache["ts"] < 30 and _models_cache["ids"]:
        return _models_cache["ids"]
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(15, connect=8)) as client:
            r = await client.get(
                f"{settings.base_url}/models",
                headers=_auth_headers(),
            )
            r.raise_for_status()
            payload = r.json()
        # OpenAI-style {"data": [...]} — but accept a bare [...] too, since a
        # few lightweight backends return a plain array.
        items = payload.get("data", payload) if isinstance(payload, dict) else payload
        ids = [m.get("id", "") for m in items if isinstance(m, dict)]
        _models_cache["ids"] = [i for i in ids if i]
        _models_cache["ts"] = time.time()
        if not _models_cache["ids"]:
            _models_cache["error"] = "backend returned an empty model list"
    except Exception as exc:
        _models_cache["ids"] = []
        _models_cache["ts"] = 0.0
        _models_cache["error"] = f"{type(exc).__name__}: {exc}"[:180]
    return _models_cache["ids"]


async def resolve_model() -> str:
    if settings.model:
        return settings.model
    ids = await list_models()
    if not ids:
        detail = _models_cache.get("error") or "no response"
        raise LlmError(
            f"No model available at {settings.base_url} ({detail}). "
            "Set a model in Settings or check the backend."
        )
    # On OpenRouter, auto-pick a free model so the zero-config default costs
    # nothing; everywhere else the first detected model is the default.
    if "openrouter.ai" in settings.base_url:
        free = [i for i in ids if i.endswith(":free")]
        if free:
            return free[0]
    return ids[0]


def _auth_headers() -> dict:
    h = {}
    if settings.api_key:
        h["Authorization"] = f"Bearer {settings.api_key}"
    return h


class LlmError(Exception):
    pass


# --------------------------------------------------------------------------
# Tools: notes + web search
# --------------------------------------------------------------------------

TOOLS_SPEC = [
    {
        "type": "function",
        "function": {
            "name": "search_web",
            "description": (
                "Search the public web for current information, facts, news, "
                "prices, weather — anything you are unsure about or that may "
                "have changed recently."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string",
                              "description": "Web search query"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "save_note",
            "description": (
                "Save a note to the user's personal memory for later. Use it "
                "when the user asks to remember/save/note something, or when "
                "a fact will clearly be useful in future conversations."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "Short title"},
                    "content": {"type": "string",
                                "description": "The note content (markdown)"},
                },
                "required": ["title", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_notes",
            "description": "List the user's saved notes (id, title, tags).",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_note",
            "description": "Read the full content of one saved note by id.",
            "parameters": {
                "type": "object",
                "properties": {"id": {"type": "string"}},
                "required": ["id"],
            },
        },
    },
]


async def note_create(title: str, content: str) -> dict:
    data = await notes_store.load()
    note = {
        "id": new_id(),
        "title": (title or "Untitled note").strip()[:120],
        "content": (content or "").strip(),
        "tags": [],
        "created_at": now_iso(),
        "updated_at": now_iso(),
    }
    data["notes"].insert(0, note)
    await notes_store.save(data)
    return {"ok": True, "id": note["id"], "title": note["title"]}


async def note_list() -> dict:
    data = await notes_store.load()
    return {"ok": True, "notes": [
        {"id": n["id"], "title": n["title"], "tags": n.get("tags", []),
         "updated_at": n["updated_at"]}
        for n in data["notes"]
    ]}


async def note_read(note_id: str) -> dict:
    data = await notes_store.load()
    for n in data["notes"]:
        if n["id"] == note_id:
            return {"ok": True, "id": n["id"], "title": n["title"],
                    "content": n["content"]}
    return {"ok": False, "error": f"note '{note_id}' not found"}


def _strip_tags(html: str) -> str:
    html = re.sub(r"<(script|style)[\s\S]*?</\1>", " ", html, flags=re.I)
    html = re.sub(r"<[^>]+>", " ", html)
    html = html.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    html = html.replace("&quot;", '"').replace("&#x27;", "'").replace("&#39;", "'")
    html = html.replace("&nbsp;", " ").replace("&hellip;", "…")
    return re.sub(r"\s+", " ", html).strip()


def _unwrap_ddg_url(href: str) -> str:
    """DuckDuckGo wraps results in /l/?uddg=<encoded> — unwrap to real URL."""
    if "duckduckgo.com/l/" in href or href.startswith("//duckduckgo.com"):
        try:
            qs = parse_qs(urlparse(href).query)
            return unquote(qs.get("uddg", [href])[0])
        except Exception:
            return href
    return href


async def web_search(query: str) -> dict:
    query = (query or "").strip()
    if not query:
        return {"ok": False, "error": "empty query"}
    backend = SEARCH_BACKEND
    if backend == "auto":
        backend = "serpapi" if SERPAPI_KEY else "duckduckgo"
    if backend in ("none", "off", "disabled"):
        return {"ok": False, "error": "web search is disabled"}
    if backend == "serpapi":
        return await _search_serpapi(query)
    return await _search_duckduckgo(query)


async def _search_duckduckgo(query: str) -> dict:
    try:
        async with httpx.AsyncClient(timeout=SEARCH_TIMEOUT_S, follow_redirects=True,
                                     headers={"User-Agent": UA}) as client:
            r = await client.get("https://html.duckduckgo.com/html/",
                                 params={"q": query})
            r.raise_for_status()
            html = r.text
    except Exception as exc:
        return {"ok": False, "error": f"search failed: {exc}"}

    results = []
    # Each result block: <a ... class="result__a" href="URL">TITLE</a> ... snippet
    pattern = re.compile(
        r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>'
        r'[\s\S]{0,600}?<a[^>]+class="result__snippet"[^>]*>(.*?)</a>',
        re.I | re.S,
    )
    for m in pattern.finditer(html):
        url = _unwrap_ddg_url(m.group(1))
        title = _strip_tags(m.group(2))
        snippet = _strip_tags(m.group(3))
        if not url.startswith("http"):
            continue
        results.append({"title": title[:150], "url": url,
                        "snippet": snippet[:280]})
        if len(results) >= 6:
            break
    if not results:
        return {"ok": False, "error": "no results (the search engine may be "
                                      "rate-limiting — try again or set a "
                                      "SerpApi key)"}
    return {"ok": True, "query": query, "results": results}


async def _search_serpapi(query: str) -> dict:
    try:
        async with httpx.AsyncClient(timeout=SEARCH_TIMEOUT_S) as client:
            r = await client.get("https://serpapi.com/search.json",
                                 params={"q": query, "api_key": SERPAPI_KEY})
            r.raise_for_status()
            organic = r.json().get("organic_results", [])
    except Exception as exc:
        return {"ok": False, "error": f"search failed: {exc}"}
    results = [{"title": o.get("title", "")[:150], "url": o.get("link", ""),
                "snippet": (o.get("snippet") or "")[:280]}
               for o in organic[:6] if o.get("link")]
    return {"ok": bool(results), "query": query, "results": results}


async def run_tool(name: str, args: dict) -> tuple[dict, dict]:
    """Execute a tool. Returns (tool_result_for_llm, event_for_ui)."""
    if name == "search_web":
        res = await web_search(str(args.get("query", "")))
        n = len(res.get("results", []))
        event = {"tool": name, "icon": "🔎",
                 "summary": f'Searched the web: “{args.get("query", "")}”',
                 "ok": res.get("ok", False)}
        if res.get("ok"):
            # Compact for the model: title — snippet — url per line.
            lines = [f"- {r['title']} — {r['snippet']} ({r['url']})"
                     for r in res["results"]]
            content = json.dumps({**res, "results_text": "\n".join(lines)})
        else:
            content = json.dumps(res)
        return {"ok": res.get("ok", False), "content": content}, event

    if name == "save_note":
        res = await note_create(str(args.get("title", "")),
                                str(args.get("content", "")))
        return ({"ok": res.get("ok", False), "content": json.dumps(res)},
                {"tool": name, "icon": "📝",
                 "summary": f'Saved note: “{res.get("title", "?")}”',
                 "ok": res.get("ok", False)})

    if name == "list_notes":
        res = await note_list()
        items = res.get("notes", [])
        return {"ok": True, "content": json.dumps(items)},
        {"tool": name, "icon": "📚", "summary": f"Listed {len(items)} notes",
         "ok": True}

    if name == "read_note":
        res = await note_read(str(args.get("id", "")))
        return ({"ok": res["ok"],
                 "content": json.dumps(res if res["ok"]
                                       else {"error": "note not found"})},
                {"tool": name, "icon": "📖",
                 "summary": f'Read note “{res.get("title", res.get("id"))}”',
                 "ok": res["ok"]})

    return ({"ok": False, "content": json.dumps({"error": "unknown tool"})},
            {"tool": name, "icon": "❓", "summary": f"unknown tool {name}",
             "ok": False})


# --------------------------------------------------------------------------
# The agent loop (chat -> tool calls -> search/notes -> final answer)
# --------------------------------------------------------------------------

def _notes_index() -> str:
    """Compact note index injected into the system prompt (memory).

    Single-process app: the store's cache is always warm after the first
    API call, so a synchronous cache read is safe here (no disk I/O in the
    event loop).
    """
    notes = notes_store._read().get("notes", [])  # noqa: SLF001
    if not notes:
        return ""
    lines = []
    for n in notes[:30]:
        snippet = re.sub(r"\s+", " ", n["content"])[:100]
        lines.append(f'- [{n["id"]}] {n["title"]}: {snippet}')
    return "\n".join(lines)


def build_system_prompt() -> str:
    today = datetime.now(timezone.utc).strftime("%A, %d %B %Y")
    prompt = (
        "You are Odysseus Pocket, a concise, helpful AI assistant running as "
        "an app on the user's Android phone. Keep answers tight and "
        "mobile-friendly: short paragraphs, lists over walls of text. Use "
        "markdown. Today is " + today + ".\n"
    )
    if settings.system_prompt:
        prompt += f"\nUser's personal instructions (follow closely):\n{settings.system_prompt}\n"
    if settings.tool_mode:
        prompt += (
            "\nYou have tools:\n"
            "- search_web: use it for anything time-sensitive, factual or "
            "unsure — do not guess prices, dates, scores, news.\n"
            "- save_note / list_notes / read_note: the user's personal "
            "memory. Save things they ask you to remember. Use read_note "
            "before claiming you don't know something they may have saved.\n"
            "Use several tools in one turn when needed, then answer.\n"
        )
    idx = _notes_index()
    if idx:
        prompt += f"\nCurrent saved notes (newest first):\n{idx}\n"
    return prompt


async def chat_completion(messages: list, use_tools: bool) -> dict:
    """Single OpenAI-compatible call. Returns the assistant message dict."""
    body: dict[str, Any] = {
        "model": await resolve_model(),
        "messages": messages,
        "stream": False,
    }
    if use_tools:
        body["tools"] = TOOLS_SPEC
        body["tool_choice"] = "auto"
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(CHAT_TIMEOUT_S, connect=6)) as client:
            r = await client.post(f"{settings.base_url}/chat/completions",
                                  json=body, headers=_auth_headers())
    except httpx.ConnectError:
        raise LlmError(
            f"Can't reach the model server at {settings.base_url}. "
            "Is Ollama (or your other backend) running?"
        )
    except httpx.TimeoutException:
        raise LlmError(
            f"The model took longer than {CHAT_TIMEOUT_S}s to answer. Free "
            "models on OpenRouter are often queued — try again, pick a "
            "different free model in Settings, or raise POCKET_CHAT_TIMEOUT."
        )
    if r.status_code == 400 and use_tools:
        # Some lightweight backends reject the "tools" parameter — retry once
        # without tools so chat still works everywhere.
        body.pop("tools", None)
        body.pop("tool_choice", None)
        async with httpx.AsyncClient(timeout=httpx.Timeout(CHAT_TIMEOUT_S, connect=6)) as client:
            r = await client.post(f"{settings.base_url}/chat/completions",
                                  json=body, headers=_auth_headers())
    if r.status_code >= 400:
        detail = r.text[:300]
        raise LlmError(f"Model server error {r.status_code}: {detail}")
    try:
        return r.json()["choices"][0]["message"]
    except Exception:
        raise LlmError(f"Unexpected response from model server: {r.text[:300]}")


async def agent_turn_events(chat: dict, user_message: str):
    """One full turn as a stream of events.

    Yields {"type": "tool", "event": {...}} as each tool runs, then finishes
    with {"type": "reply", ...} once the answer is ready (and persisted).
    Raises LlmError when the backend fails.
    """
    tool_events: list[dict] = []

    history = [{"role": m["role"], "content": m["content"]}
               for m in chat["messages"][-HISTORY_MESSAGE_CAP:]]
    messages = [
        {"role": "system", "content": build_system_prompt()},
        *history,
        {"role": "user", "content": user_message},  # the message being answered
    ]

    use_tools = settings.tool_mode
    reply_content = ""
    for _ in range(MAX_TOOL_ROUNDS):
        msg = await chat_completion(messages, use_tools)
        tool_calls = msg.get("tool_calls") or []
        if not tool_calls:
            reply_content = (msg.get("content") or "").strip()
            break

        # Persist the assistant's tool-call message for this request only.
        messages.append({"role": "assistant",
                         "content": msg.get("content") or "",
                         "tool_calls": tool_calls})
        for call in tool_calls:
            fn = (call.get("function") or {})
            name = fn.get("name", "")
            try:
                args = json.loads(fn.get("arguments") or "{}")
                if not isinstance(args, dict):
                    args = {}
            except Exception:
                args = {}
            result, event = await run_tool(name, args)
            tool_events.append(event)
            yield {"type": "tool", "event": event}
            messages.append({"role": "tool",
                             "tool_call_id": call.get("id", name),
                             "content": result.get("content", "") if result.get("ok")
                             else f"tool error: {result.get('content', '')}"})
    else:
        reply_content = (reply_content or "").strip() or \
            "(stopped after several tool rounds — try rephrasing)"

    if not reply_content:
        reply_content = "(the model returned an empty response)"

    ts = now_iso()
    chat["messages"].append({"role": "user", "content": user_message, "ts": ts})
    chat["messages"].append({"role": "assistant", "content": reply_content,
                             "ts": ts, "tools": tool_events})
    chat["updated_at"] = ts
    if len(chat["messages"]) == 2:  # first exchange -> title the chat
        chat["title"] = user_message.strip()[:60] or "New chat"
    yield {"type": "reply", "reply": reply_content, "tool_events": tool_events,
           "chat_id": chat["id"], "title": chat["title"]}


def resolve_chat_for_message(data: dict, body: dict):
    """Find or create the target chat for a chat request.

    Returns (chat, None) or (None, error_response).
    """
    chat_rec = None
    if body.get("chat_id"):
        for c in data["chats"]:
            if c["id"] == body["chat_id"]:
                chat_rec = c
                break
        if chat_rec is None:
            return None, JSONResponse({"detail": "chat not found"}, status_code=404)
    if chat_rec is None:
        chat_rec = {"id": new_id(), "title": "New chat", "messages": [],
                    "created_at": now_iso(), "updated_at": now_iso()}
        data["chats"].insert(0, chat_rec)
    return chat_rec, None


# --------------------------------------------------------------------------
# HTTP API — pure Starlette (no pydantic, no compiler needed on Termux)
# --------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(_app):
    await settings.refresh()
    yield


async def auth_and_security(request: Request, call_next):
    # Optional shared-secret gate for the API. Static shell stays open so the
    # PWA can load; without the token it simply can't read any data.
    if ACCESS_TOKEN and request.url.path.startswith("/api/"):
        supplied = ""
        auth = request.headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            supplied = auth[7:].strip()
        supplied = supplied or request.headers.get("x-pocket-token", "")
        if supplied != ACCESS_TOKEN:
            return JSONResponse({"detail": "bad or missing token"},
                                status_code=401)
    response = await call_next(request)
    # Light, preview-friendly hardening (no frame-blocking: the app is meant
    # to be embeddable/proxied).
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    return response


def _public_chat(c: dict, include_messages: bool = False) -> dict:
    out = {"id": c["id"], "title": c.get("title", "New chat"),
           "created_at": c.get("created_at"), "updated_at": c.get("updated_at"),
           "message_count": len(c.get("messages", []))}
    if include_messages:
        out["messages"] = c.get("messages", [])
    return out


def _public_note(n: dict) -> dict:
    return {k: n.get(k) for k in
            ("id", "title", "content", "tags", "created_at", "updated_at")}


async def health(request: Request):
    await settings.refresh()
    ids = await list_models()
    return JSONResponse({
        "ok": True,
        "app": "Odysseus Pocket",
        "backend": settings.base_url,
        "model": settings.model or (ids[0] if ids else ""),
        "models_detected": bool(ids),
        "models_error": _models_cache.get("error", ""),
        "tools_enabled": settings.tool_mode,
        "search": ("serpapi" if (SEARCH_BACKEND in ("auto", "serpapi") and SERPAPI_KEY)
                   else "duckduckgo" if SEARCH_BACKEND != "none" else "disabled"),
        "chats": len((await chats_store.load())["chats"]),
        "notes": len((await notes_store.load())["notes"]),
    })


async def models(request: Request):
    return JSONResponse({"models": await list_models()})


async def get_prefs(request: Request):
    await settings.refresh()
    return JSONResponse({
        "backend": settings.base_url,
        "has_api_key": bool(settings.api_key),
        "model": settings.model,
        "system_prompt": settings.system_prompt,
        "tool_mode": settings.tool_mode,
        "token_required": bool(ACCESS_TOKEN),
    })


async def set_prefs(request: Request):
    body = await request.json()
    prefs = (await prefs_store.load()).get("prefs", {})
    for key in ("backend", "model", "system_prompt"):
        if key in body and body[key] is not None:
            prefs[key] = str(body[key]).strip()
    if "tool_mode" in body:
        prefs["tool_mode"] = bool(body["tool_mode"])
    # Blank API key on save = keep the existing one.
    if body.get("api_key"):
        prefs["api_key"] = str(body["api_key"]).strip()
    data = await prefs_store.load()
    data["prefs"] = prefs
    await prefs_store.save(data)
    _models_cache["ts"] = 0.0  # force re-detect against the new backend
    await settings.refresh()
    return JSONResponse({"ok": True, "model": settings.model,
                         "backend": settings.base_url})


# ---- chats ----

async def get_chats(request: Request):
    data = await chats_store.load()
    chats = sorted(data["chats"], key=lambda c: c.get("updated_at", ""),
                   reverse=True)
    return JSONResponse({"chats": [_public_chat(c) for c in chats]})


async def create_chat(request: Request):
    data = await chats_store.load()
    chat = {"id": new_id(), "title": "New chat", "messages": [],
            "created_at": now_iso(), "updated_at": now_iso()}
    data["chats"].insert(0, chat)
    await chats_store.save(data)
    return JSONResponse({"chat": _public_chat(chat, include_messages=True)})


async def get_chat(request: Request):
    chat_id = request.path_params["chat_id"]
    data = await chats_store.load()
    for c in data["chats"]:
        if c["id"] == chat_id:
            return JSONResponse({"chat": _public_chat(c, include_messages=True)})
    return JSONResponse({"detail": "chat not found"}, status_code=404)


async def rename_chat(request: Request):
    chat_id = request.path_params["chat_id"]
    body = await request.json()
    data = await chats_store.load()
    for c in data["chats"]:
        if c["id"] == chat_id:
            if body.get("title"):
                c["title"] = str(body["title"]).strip()[:80]
            await chats_store.save(data)
            return JSONResponse({"chat": _public_chat(c)})
    return JSONResponse({"detail": "chat not found"}, status_code=404)


async def delete_chat(request: Request):
    chat_id = request.path_params["chat_id"]
    data = await chats_store.load()
    before = len(data["chats"])
    data["chats"] = [c for c in data["chats"] if c["id"] != chat_id]
    await chats_store.save(data)
    return JSONResponse({"ok": len(data["chats"]) < before})


async def chat(request: Request):
    """The main agent endpoint."""
    body = await request.json()
    user_message = str(body.get("message", "")).strip()
    if not user_message:
        return JSONResponse({"detail": "message is required"}, status_code=400)

    data = await chats_store.load()
    chat_rec = None
    if body.get("chat_id"):
        for c in data["chats"]:
            if c["id"] == body["chat_id"]:
                chat_rec = c
                break
        if chat_rec is None:
            return JSONResponse({"detail": "chat not found"}, status_code=404)
    if chat_rec is None:
        chat_rec = {"id": new_id(), "title": "New chat", "messages": [],
                    "created_at": now_iso(), "updated_at": now_iso()}
        data["chats"].insert(0, chat_rec)

    await settings.refresh()
    reply, tool_events = None, []
    try:
        async for ev in agent_turn_events(chat_rec, user_message):
            if ev["type"] == "tool":
                tool_events.append(ev["event"])
            else:
                reply = ev["reply"]
                tool_events = ev["tool_events"]
                await chats_store.save(data)
    except LlmError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=502)
    return JSONResponse({"chat_id": chat_rec["id"], "title": chat_rec["title"],
                         "reply": reply, "tool_events": tool_events})


async def chat_stream(request: Request):
    """Streaming variant: NDJSON events as they happen.

    Lines: {"type":"tool","event":…} per tool run, then
    {"type":"reply",…} — or {"type":"error","detail":…} on failure.
    The UI shows live agent activity instead of a blind wait.
    """
    body = await request.json()
    user_message = str(body.get("message", "")).strip()
    if not user_message:
        return JSONResponse({"detail": "message is required"}, status_code=400)

    data = await chats_store.load()
    chat_rec, err = resolve_chat_for_message(data, body)
    if err:
        return err

    await settings.refresh()

    async def gen():
        try:
            async for ev in agent_turn_events(chat_rec, user_message):
                if ev["type"] == "reply":
                    await chats_store.save(data)
                yield json.dumps(ev, ensure_ascii=False) + "\n"
        except LlmError as exc:
            yield json.dumps({"type": "error", "detail": str(exc)},
                             ensure_ascii=False) + "\n"

    return StreamingResponse(gen(), media_type="application/x-ndjson",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


# ---- notes ----

async def get_notes(request: Request):
    q = request.query_params.get("q", "")
    data = await notes_store.load()
    notes = data["notes"]
    if q:
        ql = q.lower()
        notes = [n for n in notes
                 if ql in n["title"].lower() or ql in n["content"].lower()]
    return JSONResponse({"notes": [_public_note(n) for n in notes]})


async def create_note(request: Request):
    body = await request.json()
    res = await note_create(str(body.get("title", "")),
                            str(body.get("content", "")))
    if not res.get("ok"):
        return JSONResponse({"detail": "could not save note"}, status_code=500)
    data = await notes_store.load()
    note = next(n for n in data["notes"] if n["id"] == res["id"])
    return JSONResponse({"note": _public_note(note)})


async def update_note(request: Request):
    note_id = request.path_params["note_id"]
    body = await request.json()
    data = await notes_store.load()
    for n in data["notes"]:
        if n["id"] == note_id:
            if "title" in body:
                n["title"] = str(body["title"]).strip()[:120] or n["title"]
            if "content" in body:
                n["content"] = str(body["content"])
            if "tags" in body and isinstance(body["tags"], list):
                n["tags"] = [str(t)[:30] for t in body["tags"][:10]]
            n["updated_at"] = now_iso()
            await notes_store.save(data)
            return JSONResponse({"note": _public_note(n)})
    return JSONResponse({"detail": "note not found"}, status_code=404)


async def delete_note(request: Request):
    note_id = request.path_params["note_id"]
    data = await notes_store.load()
    before = len(data["notes"])
    data["notes"] = [n for n in data["notes"] if n["id"] != note_id]
    await notes_store.save(data)
    return JSONResponse({"ok": len(data["notes"]) < before})


# --------------------------------------------------------------------------
# Static frontend (PWA)
# --------------------------------------------------------------------------

class NoCacheStaticFiles(StaticFiles):
    """Shell assets must revalidate so PWA updates land on next open."""

    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache"
        return response


async def service_worker(request: Request):
    response = FileResponse(STATIC_DIR / "sw.js", media_type="text/javascript")
    response.headers["Cache-Control"] = "no-cache"
    response.headers["Service-Worker-Allowed"] = "/"  # scope the whole origin
    return response


async def favicon(request: Request):
    return FileResponse(STATIC_DIR / "icons" / "icon-192.png",
                        media_type="image/png")


async def index(request: Request):
    return FileResponse(STATIC_DIR / "index.html", media_type="text/html")


routes = [
    Route("/api/health", health, methods=["GET"]),
    Route("/api/models", models, methods=["GET"]),
    Route("/api/prefs", get_prefs, methods=["GET"]),
    Route("/api/prefs", set_prefs, methods=["POST"]),
    Route("/api/chats", get_chats, methods=["GET"]),
    Route("/api/chats", create_chat, methods=["POST"]),
    Route("/api/chats/{chat_id}", get_chat, methods=["GET"]),
    Route("/api/chats/{chat_id}", rename_chat, methods=["PATCH"]),
    Route("/api/chats/{chat_id}", delete_chat, methods=["DELETE"]),
    Route("/api/chat", chat, methods=["POST"]),
    Route("/api/chat/stream", chat_stream, methods=["POST"]),
    Route("/api/notes", get_notes, methods=["GET"]),
    Route("/api/notes", create_note, methods=["POST"]),
    Route("/api/notes/{note_id}", update_note, methods=["PATCH"]),
    Route("/api/notes/{note_id}", delete_note, methods=["DELETE"]),
    Route("/", index, methods=["GET"]),
    Route("/sw.js", service_worker, methods=["GET"]),
    Route("/favicon.ico", favicon, methods=["GET"]),
    Mount("/static", app=NoCacheStaticFiles(directory=STATIC_DIR), name="static"),
]

app = Starlette(
    routes=routes,
    lifespan=lifespan,
    middleware=[Middleware(BaseHTTPMiddleware, dispatch=auth_and_security)],
)


def _lan_ip() -> str:
    """Best-effort LAN IPv4 for the startup banner (no packets are sent)."""
    import socket
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))  # route lookup only — nothing is delivered
        ip = s.getsockname()[0]
        s.close()
        return "" if ip.startswith("127.") else ip
    except Exception:
        return ""


if __name__ == "__main__":
    lan = _lan_ip()
    wifi_line = f"  On Wi-Fi →  http://{lan}:{PORT}\n" if lan else ""
    print(f"""
  ⛵  Odysseus Pocket
  ─────────────────────────────────────────
  UI       →  http://localhost:{PORT}
{wifi_line}  Models   →  {DEFAULT_BASE_URL}  (Ollama, LM Studio, ... any /v1 backend)
  Data     →  {DATA_DIR}
  ─────────────────────────────────────────
  Tip: install it from your phone's Chrome → ⋮ → "Add to Home screen".
""")
    uvicorn.run(app, host=HOST, port=PORT, log_level="warning")
