# Odysseus Pocket — from zero to running app

A detailed, copy-paste walkthrough for a completely fresh start on an Android
phone. (Running on Linux/macOS/Windows? Skip to [section 8](#8-run-it-on-a-computer-instead)
— it's the same app.)

---

## 0. What you need

| Requirement | Detail |
|---|---|
| Android phone | Android 7.0+ for Termux |
| Free space | ~300 MB for Termux+Python+deps · **+2 GB** if you also install a model on the phone |
| Internet | for the one-time installs (everything runs locally afterwards) |
| A "brain" for the agent | one of: a model on the phone (section 6A) · a model on your PC (6B) · a hosted API key (6C) |

> **30-second overview:** install Termux → install python+git → clone the repo →
> `./run.sh` → open Chrome at `localhost:8000` → "Add to Home screen". Done.

---

## 1. Install Termux

Use the **F-Droid** build (the Play Store version is outdated and broken):

1. In your phone's browser open <https://f-droid.org> and install the F-Droid app,
   or grab the Termux APK directly from
   <https://github.com/termux/termux-app/releases> (asset: `termux-app_*_arm64-v8a.apk`
   for most modern phones).
2. Install and open **Termux**.
3. Update its package lists (tap **Allow** when it asks for storage, press Enter if prompted):

   ```bash
   pkg update -y && pkg upgrade -y
   ```

> 💡 Optional quality-of-life: install [Termux:API](https://f-droid.org/en/packages/com.termux.api/)
> and the `termux-api` package if you later want notifications etc. Not required.

---

## 2. Install Python and Git inside Termux

```bash
pkg install -y python git
python --version    # should print 3.x
```

---

## 3. Get the code

The Pocket app lives in the `pocket/` folder of the Odysseus repo, on branch
`arena/01a08b5c-odysseus` (until it's merged into `dev`):

```bash
git clone --depth 1 -b arena/01a08b5c-odysseus https://github.com/johnadel348-gif/odysseus.git
cd odysseus/pocket
ls
#  server.py  run.sh  requirements.txt  static/  README.md ...
```

The repo is public — no account or token needed. `--depth 1` downloads only the
latest snapshot (~30 MB). Want *only* the `pocket/` files? Use a sparse clone
instead:

```bash
git clone --depth 1 --filter=blob:none --sparse -b arena/01a08b5c-odysseus https://github.com/johnadel348-gif/odysseus.git
cd odysseus && git sparse-checkout set pocket && cd pocket
```

---

## 4. First run (installs everything automatically)

```bash
./run.sh
```

What happens on this first run:

1. creates an isolated Python environment in `pocket/.venv` (Termux requires this;
   it also avoids the newer "externally managed environment" pip error),
2. installs the only three dependencies: `fastapi`, `uvicorn`, `httpx`,
3. grabs a wake-lock so Android doesn't freeze the server,
4. starts the server and prints something like:

```
  ⛵  Odysseus Pocket
  ─────────────────────────────────────────
  UI       →  http://localhost:8000
  On Wi-Fi →  http://192.168.1.42:8000
  Models   →  http://127.0.0.1:11434/v1  (Ollama, LM Studio, ... any /v1 backend)
  Data     →  /data/.../pocket/data
  ─────────────────────────────────────────
```

**Leave this Termux screen alone — it IS the app's server.** You'll see request
activity here as you use the app. (Redownloading later? `git pull` then `./run.sh` again.)

---

## 5. Open it and install it as an app

**On the same phone:**

1. Open **Chrome** → go to `http://localhost:8000`
   (if Chrome is fussy, `http://127.0.0.1:8000` always works).
2. Chat menu **⋮ → Add to Home screen → Install**.
3. Launch **Pocket** from your home screen — full-screen, own icon, like a native app.

**From another device (tablet/PC) on the same Wi-Fi:** use the
`On Wi-Fi → http://192.168.x.x:8000` URL from the banner. Nothing else to configure —
the server already listens on all interfaces.

You'll know things are healthy: the **status dot** next to the title turns **green**
once a model is detected (amber = still checking, red = no model / backend down).

---

## 6. Give the agent a model (pick ONE of A / B / C)

Open the app → **⚙ Settings** → set **Model server** → **Save** → **Test connection**.
Leave *Model* blank for auto-detect, or pin a specific one.

### A) Model on the phone itself (fully offline, needs ~2 GB, slower)

Run Ollama inside an Ubuntu proot (the reliable way to get Ollama on Android):

```bash
# in Termux (a NEW session: swipe from left edge → New session)
pkg install -y proot-distro
proot-distro install ubuntu
proot-distro login ubuntu
# — you're now inside Ubuntu on your phone —
apt update && apt install -y curl
curl -fsSL https://ollama.com/install.sh | sh
ollama serve &                 # starts on 127.0.0.1:11434
ollama pull llama3.2:3b        # ~2 GB, a good phone-sized model
```

Keep this session running, go back to your first Termux session, start Pocket,
and in Settings use the default backend `http://127.0.0.1:11434/v1` — Pocket
auto-detects the model.

*Phone hardware reality check:* 1–3B models answer in seconds; 7B+ models crawl.
Start small.

### B) Model on your computer, phone connects over Wi-Fi (recommended)

On the PC/Mac (where Ollama is installed):

```bash
OLLAMA_HOST=0.0.0.0 ollama serve     # accept LAN connections
ollama pull llama3.2:3b
# find the PC's LAN IP:  Windows: ipconfig · macOS/Linux: ifconfig or ip addr
```

In Pocket Settings set **Model server** to `http://<PC-IP>:11434/v1`
(e.g. `http://192.168.1.20:11434/v1`). The phone's browser never talks to the PC
directly — Pocket's own server does — so there are no CORS headaches.
Any `llama.cpp`/`LM Studio`/`llamafile` server works too; just use its `/v1` URL.

### C) Hosted API — no local model at all (fastest, needs internet)

In Settings use e.g.:

| Provider | Model server URL | + API key |
|---|---|---|
| OpenAI | `https://api.openai.com/v1` | sk-… |
| OpenRouter | `https://openrouter.ai/api/v1` | sk-or-… |
| Groq | `https://api.groq.com/openai/v1` | gsk_… |

Type the key in the **API key** field (it's stored server-side in `data/prefs.json`,
never in the browser).

---

## 7. Using the app

- **💬 Chat** — type, press ➤ (or Enter on a keyboard). Tap **Tools** above the
  keyboard to toggle the agent loop on/off.
- **🔎 Web search** — just ask things like "search the web for…". The model invokes
  the search itself; you'll see a 🔎 chip above the answer.
- **📝 Memory** — "remember that …" saves a note; the assistant reads your notes
  automatically every turn. Manage them under the 📝 icon in the top bar.
- **🗂 Chats** — ☰ opens history; new/delete/rename included.
- **⚙ Settings** — backend, key, model, personal instructions (system prompt),
  and the access PIN (below).

---

## 8. Run it on a computer instead

Linux/macOS: identical — `./run.sh`, then open `http://localhost:8000`.
Windows:

```powershell
cd pocket
py -m venv .venv
.venv\Scripts\pip install fastapi uvicorn httpx
.venv\Scripts\python server.py
```

Runs on anything with Python 3.9+ — a Raspberry Pi, an old laptop, a VPS.

---

## 9. Keep it running reliably on Android

Android loves to kill background apps. Three defenses:

1. **Wake lock** — `run.sh` already calls `termux-wake-lock`. You can also tap
   **Acquire wakelock** in Termux's notification.
2. **Battery** — Android Settings → Apps → Termux → Battery → **Unrestricted**.
3. **Auto-start** — start Pocket whenever Termux opens:

   ```bash
   echo '(pgrep -f pocket/server.py >/dev/null || nohup ~/odysseus/pocket/run.sh >~/pocket.log 2>&1 &)' >> ~/.bashrc
   ```

   Or as a managed service:

   ```bash
   pkg install -y termux-services
   mkdir -p $PREFIX/var/service/pocket
   cat > $PREFIX/var/service/pocket/run <<'EOF'
   #!/data/data/com.termux/files/usr/bin/sh
   exec bash /data/data/com.termux/files/home/odysseus/pocket/run.sh
   EOF
   chmod +x $PREFIX/var/service/pocket/run
   sv up pocket
   ```

---

## 10. Security & your data

- **Data stays with the server**: chats/notes/prefs are JSON files in `pocket/data/`.
  Back the folder up (copy it, or sync it with Syncthing) and you can move the
  whole app anywhere.
- **Access PIN** — if other people/devices share your Wi-Fi, set a PIN:

  ```bash
  POCKET_TOKEN=my-secret ./run.sh
  ```

  Then in the PWA: ⚙ Settings → *Server access PIN* → enter it once; it's stored
  on that device only. All `/api/*` routes reject requests without it.
- **Notes are sent to the model** as memory/context — don't store passwords in them
  if you point Pocket at a hosted (cloud) provider.
- `./run.sh` restarts are always safe; data files are written atomically.

---

## 11. Updating

```bash
cd ~/odysseus          # or wherever you cloned
git pull
cd pocket && ./run.sh  # reuses the venv; pulls any new dependencies
```

The PWA picks up new UI files on its next open (assets are served no-cache and the
service worker refreshes in the background). If the home-screen app ever looks
stale: open it, pull down to reload, or clear the PWA's storage.

---

## 12. Troubleshooting

| Symptom | Fix |
|---|---|
| `./run.sh: Permission denied` | `chmod +x run.sh && ./run.sh` |
| `pkg`/`pip` network errors | check the phone's internet; retry — Termux mirrors hiccup |
| `python: not found` | `pkg install python` |
| Status dot stays **red** | backend not running / wrong URL → ⚙ → Test connection; on-phone Ollama needs its proot session alive |
| "Can't reach the model server" | same — plus check the port (Ollama 11434, LM Studio 1234, llama.cpp 8080) |
| PC's Ollama unreachable from phone | did you start it with `OLLAMA_HOST=0.0.0.0`? Same Wi-Fi? Router "AP/client isolation" must be off |
| Very slow replies on phone | use a 1–3B model, quantized (Q4), or offload to PC / hosted API |
| Port already in use | `POCKET_PORT=8001 ./run.sh` |
| Web search chips say "search failed" | the DDG endpoint rate-limits sometimes; retry later or set `SERPAPI_KEY` + `POCKET_SEARCH_BACKEND=serpapi` |
| Other device can't open the Wi-Fi URL | server binds `0.0.0.0` already — check both devices on the same network and phone firewall/hotspot settings |
| PWA won't install | "Add to Home screen" needs the page open over `http://` on some Chrome versions — open `localhost:8000` first, install, then keep using it |
| Want a fresh start | stop the server, delete `pocket/data/` |

---

*Still stuck? Check the Termux window running the server — every request and
startup detail is logged there.*
