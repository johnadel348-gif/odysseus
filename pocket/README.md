<p align="center">
  <img src="static/icons/icon-512.png" alt="Odysseus Pocket" width="96">
</p>

<h1 align="center">Odysseus Pocket</h1>

<p align="center">
  The Android-friendly slice of <a href="../README.md">Odysseus</a> — a self-hosted AI agent
  that runs <b>entirely on your phone</b>. One Python file, three dependencies, no Docker, no build step.
</p>

---

## What it does

- 💬 **Agent chat** — streaming-quality conversations against any OpenAI-compatible backend
  (Ollama, LM Studio, llama.cpp server, llamafile, KoboldCpp, OpenAI, …), with chat history.
- 🔎 **Web search tool** — the model can search the web by itself when it needs fresh facts
  (DuckDuckGo out of the box, or [SerpApi](https://serpapi.com) with a key).
- 📝 **Notes / memory** — saved notes the agent can read and write on its own, plus a notes
  manager in the app.
- 📲 **Installable PWA** — open it once in Chrome, *Add to Home screen*, and it runs
  full-screen with the Odysseus icon like a native app.

Everything lives on the device running the server — nothing leaves it except your web
searches and model calls you point it at.

**What it deliberately drops** from the full Odysseus workspace: email, calendar, documents,
gallery, cookbook, MCP, shell tools, Docker/GPU profiles, and the desktop UI.
~1,100 Python files → **1**.

---

## Run it on Android (Termux)

1. Install [Termux](https://github.com/termux/termux-app/releases) (F-Droid build recommended),
   then:

   ```bash
   pkg update
   pkg install python git
   git clone https://github.com/odysseus-dev/odysseus.git
   cd odysseus/pocket
   ./run.sh
   ```

   `run.sh` creates a local venv, installs the three dependencies, and starts the server.

2. Open **http://localhost:8000** in Chrome.
3. Chrome menu **⋮ → Add to Home screen** → launch it like any app. Done.

> 💡 Install a model on the phone with [Ollama for Termux](https://ollama.com):
> `ollama serve` + `ollama pull llama3.2:3b` — Pocket auto-detects it.
> On a spare/older phone even a 1–3B model is usable.

### Keep it running

Android freezes background apps. Pick one:

```bash
# simplest: the server starts whenever you open Termux
echo '(pgrep -f pocket/server.py >/dev/null || nohup ~/odysseus/pocket/run.sh >~/pocket.log 2>&1 &)' >> ~/.bashrc

# or with termux-services (survives reboots while Termux is alive):
pkg install termux-services runit-scripts
mkdir -p $PREFIX/var/service/pocket
cat > $PREFIX/var/service/pocket/run <<'EOF'
#!/data/data/com.termux/files/usr/bin/sh
exec bash /data/data/com.termux/files/home/odysseus/pocket/run.sh
EOF
chmod +x $PREFIX/var/service/pocket/run
sv-enable pocket   # if available; otherwise: sv up pocket
```

Also disable battery optimization for Termux (Android Settings → Apps → Termux → Battery).

---

## Run it anywhere else

```bash
cd pocket
./run.sh                 # Linux / macOS
# or: python server.py  (after: pip install fastapi uvicorn httpx)
```

Open `http://localhost:8000`, or `http://<device-ip>:8000` from your phone's browser —
anything on the same Wi-Fi can use it.

<details>
<summary>Want a real APK instead of the PWA?</summary>

You don't need one for personal use — the PWA installs a launcher icon and runs full-screen.
If you want a distributable APK, wrap this same UI with
[Bubblewrap](https://github.com/GoogleChromeLabs/bubblewrap) (`bubblewrap init --manifest
https://your-host/static/manifest.webmanifest`) once the server is reachable over HTTPS.
</details>

---

## Point it at a model

Open **⚙ Settings** in the app:

| Setting | Example |
|---|---|
| Model server | `http://127.0.0.1:11434/v1` (Ollama on the same phone) |
| | `http://192.168.1.20:11434/v1` (Ollama on your PC — set `OLLAMA_HOST=0.0.0.0` there) |
| | `https://api.openai.com/v1` + API key |
| Model | blank = auto-detect the first available model |

Or set defaults in the environment / `.env`: see [`.env.example`](.env.example).

---

## Security

- The server is open on your LAN by design so the phone's browser can reach it.
  **Set a PIN as soon as it's reachable by others:**
  `POCKET_TOKEN=your-pin ./run.sh` — the app asks for it once in ⚙ Settings and stores it
  on that device only. All `/api/*` routes then require it.
- Chats and notes are plain JSON files in `pocket/data/` — back that folder up (e.g. sync
  it with Syncthing) and it's portable.
- Notes content is included in model prompts as "memory" — don't save secrets you
  wouldn't send to your model provider.

## Configuration (env vars, all optional)

| Variable | Default | Purpose |
|---|---|---|
| `POCKET_BASE_URL` | `http://127.0.0.1:11434/v1` | Model backend (`/v1` appended if missing) |
| `POCKET_API_KEY` | *(empty)* | Bearer key for the backend |
| `POCKET_MODEL` | *(auto-detect)* | Pin a model id |
| `POCKET_SEARCH_BACKEND` | `auto` | `duckduckgo` / `serpapi` / `none` |
| `SERPAPI_KEY` | *(empty)* | Enables SerpApi search |
| `POCKET_TOKEN` | *(empty)* | API access PIN |
| `POCKET_HOST` / `POCKET_PORT` | `0.0.0.0` / `8000` | Binding |
| `POCKET_DATA_DIR` | `pocket/data` | Where chats/notes/prefs are stored |

## Files

```
pocket/
├── server.py              ← the whole backend: API + agent loop + tools
├── run.sh                 ← one-command launcher (venv + deps + start)
├── requirements.txt       ← fastapi, uvicorn, httpx — that's all
├── .env.example           ← every setting, documented
├── static/
│   ├── index.html         ← single-screen app shell
│   ├── app.js             ← chat UI, markdown, notes, settings (no framework)
│   ├── style.css          ← mobile-first dark theme (Odysseus palette)
│   ├── manifest.webmanifest, sw.js   ← makes it installable + offline-capable
│   └── icons/             ← Odysseus brand icons
└── data/                  ← created at runtime: chats.json, notes.json, prefs.json
```

## API (for tinkering)

`POST /api/chat` `{chat_id?, message, use_tools?}` · `GET/POST /api/chats[/{id}]` ·
`GET/POST/PATCH/DELETE /api/notes[/{id}]` · `GET/POST /api/prefs` · `GET /api/health` · `GET /api/models`

Licensed like Odysseus: AGPL-3.0-or-later.
