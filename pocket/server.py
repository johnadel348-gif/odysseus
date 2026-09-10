#!/usr/bin/env python3
"""Odysseus Pocket — a tiny, Android-friendly slice of the Odysseus AI workspace.

One file. Three dependencies (fastapi, uvicorn, httpx). No Docker, no build step.

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
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

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

_models_cache: dict = {"ids": [], "ts": 0.0}


async def list_models() -> list[str]:
    if time.time() - _models_cache["ts"] < 30 and _models_cache["ids"]:
        return _models_cache["ids"]
    try:
        async with httpx.AsyncClient(timeout=4) as client:
            r = await client.get(
                f"{settings.base_url}/models",
                headers=_auth_headers(),
            )
            r.raise_for_status()
            ids = [m.get("id", "") for m in r.json().get("data", [])]
            _models_cache["ids"] = [i for i in ids if i]
            _models_cache["ts"] = time.time()
    except Exception:
        _models_cache["ids"] = []
        _models_cache["ts"] = 0.0
    return _models_cache["ids"]


async def resolve_model() -> str:
    if settings.model:
        return settings.model
    ids = await list_models()
    if not ids:
        raise LlmError(
            f"No model configured and none detected at {settings.base_url}. "
            "Install/run Ollama (or LM Studio...) or set a model in Settings."
        )
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
        raise LlmError("The model took too long to answer. Try a smaller "
                       "model or a shorter message.")
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


async def agent_turn(chat: dict, user_message: str) -> dict:
    """One full turn: user message -> (tool rounds) -> assistant reply.

    Returns {"reply", "tool_events"}. Persists both messages into `chat`.
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
    return {"reply": reply_content, "tool_events": tool_events}


# --------------------------------------------------------------------------
# HTTP API
# --------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(_app: FastAPI):
    await settings.refresh()
    yield


app = FastAPI(title="Odysseus Pocket", lifespan=lifespan)


@app.middleware("http")
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


@app.get("/api/health")
async def health():
    await settings.refresh()
    ids = await list_models()
    return {
        "ok": True,
        "app": "Odysseus Pocket",
        "backend": settings.base_url,
        "model": settings.model or (ids[0] if ids else ""),
        "models_detected": bool(ids),
        "tools_enabled": settings.tool_mode,
        "search": ("serpapi" if (SEARCH_BACKEND in ("auto", "serpapi") and SERPAPI_KEY)
                   else "duckduckgo" if SEARCH_BACKEND != "none" else "disabled"),
        "chats": len((await chats_store.load())["chats"]),
        "notes": len((await notes_store.load())["notes"]),
    }


@app.get("/api/models")
async def models():
    return {"models": await list_models()}


@app.get("/api/prefs")
async def get_prefs():
    await settings.refresh()
    return {
        "backend": settings.base_url,
        "has_api_key": bool(settings.api_key),
        "model": settings.model,
        "system_prompt": settings.system_prompt,
        "tool_mode": settings.tool_mode,
        "token_required": bool(ACCESS_TOKEN),
    }


@app.post("/api/prefs")
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
    return {"ok": True, "model": settings.model, "backend": settings.base_url}


# ---- chats ----

@app.get("/api/chats")
async def get_chats():
    data = await chats_store.load()
    chats = sorted(data["chats"], key=lambda c: c.get("updated_at", ""),
                   reverse=True)
    return {"chats": [_public_chat(c) for c in chats]}


@app.post("/api/chats")
async def create_chat():
    data = await chats_store.load()
    chat = {"id": new_id(), "title": "New chat", "messages": [],
            "created_at": now_iso(), "updated_at": now_iso()}
    data["chats"].insert(0, chat)
    await chats_store.save(data)
    return {"chat": _public_chat(chat, include_messages=True)}


@app.get("/api/chats/{chat_id}")
async def get_chat(chat_id: str):
    data = await chats_store.load()
    for c in data["chats"]:
        if c["id"] == chat_id:
            return {"chat": _public_chat(c, include_messages=True)}
    return JSONResponse({"detail": "chat not found"}, status_code=404)


@app.patch("/api/chats/{chat_id}")
async def rename_chat(chat_id: str, request: Request):
    body = await request.json()
    data = await chats_store.load()
    for c in data["chats"]:
        if c["id"] == chat_id:
            if body.get("title"):
                c["title"] = str(body["title"]).strip()[:80]
            await chats_store.save(data)
            return {"chat": _public_chat(c)}
    return JSONResponse({"detail": "chat not found"}, status_code=404)


@app.delete("/api/chats/{chat_id}")
async def delete_chat(chat_id: str):
    data = await chats_store.load()
    before = len(data["chats"])
    data["chats"] = [c for c in data["chats"] if c["id"] != chat_id]
    await chats_store.save(data)
    return {"ok": len(data["chats"]) < before}


@app.post("/api/chat")
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
    try:
        result = await agent_turn(chat_rec, user_message)
    except LlmError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=502)
    await chats_store.save(data)
    return {"chat_id": chat_rec["id"], "title": chat_rec["title"],
            "reply": result["reply"], "tool_events": result["tool_events"]}


# ---- notes ----

@app.get("/api/notes")
async def get_notes(q: str = ""):
    data = await notes_store.load()
    notes = data["notes"]
    if q:
        ql = q.lower()
        notes = [n for n in notes
                 if ql in n["title"].lower() or ql in n["content"].lower()]
    return {"notes": [_public_note(n) for n in notes]}


@app.post("/api/notes")
async def create_note(request: Request):
    body = await request.json()
    res = await note_create(str(body.get("title", "")),
                            str(body.get("content", "")))
    if not res.get("ok"):
        return JSONResponse({"detail": "could not save note"}, status_code=500)
    data = await notes_store.load()
    note = next(n for n in data["notes"] if n["id"] == res["id"])
    return {"note": _public_note(note)}


@app.patch("/api/notes/{note_id}")
async def update_note(note_id: str, request: Request):
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
            return {"note": _public_note(n)}
    return JSONResponse({"detail": "note not found"}, status_code=404)


@app.delete("/api/notes/{note_id}")
async def delete_note(note_id: str):
    data = await notes_store.load()
    before = len(data["notes"])
    data["notes"] = [n for n in data["notes"] if n["id"] != note_id]
    await notes_store.save(data)
    return {"ok": len(data["notes"]) < before}


# --------------------------------------------------------------------------
# Static frontend (PWA)
# --------------------------------------------------------------------------

class NoCacheStaticFiles(StaticFiles):
    """Shell assets must revalidate so PWA updates land on next open."""

    def file_response(self, *args, **kwargs):  # noqa: ANN002, ANN003
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache"
        return response


app.mount("/static", NoCacheStaticFiles(directory=STATIC_DIR), name="static")


@app.get("/sw.js", include_in_schema=False)
async def service_worker():
    response = FileResponse(STATIC_DIR / "sw.js", media_type="text/javascript")
    response.headers["Cache-Control"] = "no-cache"
    response.headers["Service-Worker-Allowed"] = "/"  # scope the whole origin
    return response


@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    return FileResponse(STATIC_DIR / "icons" / "icon-192.png",
                        media_type="image/png")


@app.get("/", include_in_schema=False)
async def index():
    return FileResponse(STATIC_DIR / "index.html", media_type="text/html")


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
