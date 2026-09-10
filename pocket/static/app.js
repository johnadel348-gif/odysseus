/* Odysseus Pocket — frontend logic. No framework, no build step.
   Talks to the single-file Starlette server in ../server.py */

"use strict";

/* ───────────────────────────── state ───────────────────────────── */

const state = {
  chatId: null,
  busy: false,
  toolsOn: true,
  health: null,
};

const $ = (id) => document.getElementById(id);
const els = {
  input: $("input"), send: $("btnSend"), tools: $("btnTools"),
  modelLabel: $("modelLabel"), statusDot: $("statusDot"),
  messages: $("messages"), welcome: $("welcome"), chatMain: $("chatMain"),
  drawer: $("drawer"), drawerScrim: $("drawerScrim"), chatList: $("chatList"),
  notesSheet: $("notesSheet"), notesList: $("notesList"), noteSearch: $("noteSearch"),
  noteEditor: $("noteEditor"), noteTitle: $("noteTitle"), noteContent: $("noteContent"),
  settingsSheet: $("settingsSheet"),
  toast: $("toast"),
};

/* ───────────────────────────── helpers ───────────────────────────── */

function getToken() { return localStorage.getItem("pocket_token") || ""; }

async function api(path, options = {}) {
  const headers = { ...(options.headers || {}) };
  if (options.body !== undefined) headers["Content-Type"] = "application/json";
  const token = getToken();
  if (token) headers["Authorization"] = "Bearer " + token;
  const res = await fetch(path, { ...options, headers });
  let data = null;
  try { data = await res.json(); } catch { /* empty body */ }
  if (!res.ok) {
    const msg = (data && data.detail) ? data.detail : `request failed (${res.status})`;
    throw new Error(msg);
  }
  return data;
}

let toastTimer = null;
function toast(text, ms = 2600) {
  els.toast.textContent = text;
  els.toast.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { els.toast.hidden = true; }, ms);
}

function escHtml(s) {
  return String(s).replace(/[&<>"']/g,
    (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function timeAgo(iso) {
  if (!iso) return "";
  const s = (Date.now() - new Date(iso).getTime()) / 1000;
  if (s < 90) return "just now";
  if (s < 3600) return Math.round(s / 60) + " min ago";
  if (s < 86400) return Math.round(s / 3600) + " h ago";
  return new Date(iso).toLocaleDateString();
}

/* ───────────────────────────── markdown ───────────────────────────── */

function renderMarkdown(src) {
  const codeBlocks = [];
  let text = String(src).replace(/```(\w*)[ \t]*\n?([\s\S]*?)```/g, (_m, lang, code) => {
    codeBlocks.push({ lang, code });
    return `\u0000CB${codeBlocks.length - 1}\u0000`;
  });

  text = escHtml(text);
  // inline code first so its content is protected from other rules
  text = text.replace(/`([^`\n]+)`/g, '<code class="inline">$1</code>');
  // links: [text](https://…) and bare URLs — https only
  text = text.replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g,
    '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>');
  text = text.replace(/(^|[\s>])(https?:\/\/[^\s<]+)/g,
    '$1<a href="$2" target="_blank" rel="noopener noreferrer">$2</a>');
  // bold / italic / strikethrough
  text = text.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  text = text.replace(/(^|[\s(])\*([^*\n]+)\*/g, "$1<em>$2</em>");
  text = text.replace(/~~([^~]+)~~/g, "<del>$1</del>");

  // line-based blocks: headings, lists, quotes, hr, paragraphs
  const lines = text.split("\n");
  const out = [];
  let para = [], list = null; // list = {tag, items}

  const flushPara = () => {
    if (para.length) { out.push("<p>" + para.join("<br>") + "</p>"); para = []; }
  };
  const flushList = () => {
    if (list) { out.push(`<${list.tag}>` + list.items.map((i) => `<li>${i}</li>`).join("") + `</${list.tag}>`); list = null; }
  };

  for (const raw of lines) {
    const line = raw.trimEnd();
    if (!line.trim()) { flushPara(); flushList(); continue; }

    let m;
    if ((m = line.match(/^(#{1,4})\s+(.*)$/))) {
      flushPara(); flushList();
      const level = Math.min(m[1].length + 1, 5);
      out.push(`<h${level}>${m[2]}</h${level}>`);
      continue;
    }
    if (/^(-{3,}|\*{3,})$/.test(line.trim())) { flushPara(); flushList(); out.push("<hr>"); continue; }
    if ((m = line.match(/^&gt;\s?(.*)$/))) {   // ">" was escaped
      flushPara(); flushList();
      out.push(`<blockquote><p>${m[1]}</p></blockquote>`);
      continue;
    }
    if ((m = line.match(/^[-*•]\s+(.*)$/))) {
      flushPara();
      if (!list || list.tag !== "ul") { flushList(); list = { tag: "ul", items: [] }; }
      list.items.push(m[1]);
      continue;
    }
    if ((m = line.match(/^\d+[.)]\s+(.*)$/))) {
      flushPara();
      if (!list || list.tag !== "ol") { flushList(); list = { tag: "ol", items: [] }; }
      list.items.push(m[1]);
      continue;
    }
    flushList();
    para.push(line);
  }
  flushPara(); flushList();

  // restore fenced code blocks (escaped at restore time)
  text = out.join("\n").replace(/\u0000CB(\d+)\u0000/g, (_m, i) => {
    const b = codeBlocks[Number(i)];
    return `<div class="codeblock"><div class="codeblock-head"><span>${escHtml(b.lang || "code")}</span><button class="code-copy" data-code="${encodeURIComponent(b.code)}">Copy</button></div><pre><code>${escHtml(b.code.replace(/\n$/, ""))}</code></pre></div>`;
  });
  return text;
}

/* ───────────────────────────── chat rendering ───────────────────────────── */

function showWelcome(show) {
  els.welcome.style.display = show ? "flex" : "none";
}

function addMessageEl(role, content, toolEvents) {
  const div = document.createElement("div");
  div.className = "msg " + (role === "user" ? "user" : "assistant");

  if (role !== "user" && Array.isArray(toolEvents) && toolEvents.length) {
    const chips = document.createElement("div");
    chips.className = "tool-chips";
    for (const ev of toolEvents) {
      const chip = document.createElement("span");
      chip.className = "tool-chip";
      const t = document.createElement("span");
      t.className = "t";
      t.textContent = `${ev.icon || "🔧"} ${ev.summary || ev.tool}`;
      chip.appendChild(t);
      chips.appendChild(chip);
    }
    div.appendChild(chips);
  }

  if (role === "user") {
    div.textContent = content;
  } else {
    div.insertAdjacentHTML("beforeend", `<div class="md">${renderMarkdown(content)}</div>`);
    const actions = document.createElement("div");
    actions.className = "msg-actions";
    const copy = document.createElement("button");
    copy.className = "copy-btn";
    copy.textContent = "Copy";
    copy.addEventListener("click", () => {
      navigator.clipboard?.writeText(content).then(() => toast("Copied ✓"));
    });
    actions.appendChild(copy);
    div.appendChild(actions);
  }
  els.messages.appendChild(div);
  scrollBottom();
  return div;
}

function scrollBottom() {
  els.chatMain.scrollTop = els.chatMain.scrollHeight;
}

function showTyping() {
  const div = document.createElement("div");
  div.className = "msg assistant";
  div.id = "typingMsg";
  div.innerHTML = '<span class="typing"><i></i><i></i><i></i></span>';
  els.messages.appendChild(div);
  scrollBottom();
}

function showError(text) {
  const div = document.createElement("div");
  div.className = "error-bubble";
  div.textContent = "⚠️ " + text;
  els.messages.appendChild(div);
  scrollBottom();
}

function renderChat(chat) {
  els.messages.innerHTML = "";
  const has = chat && chat.messages && chat.messages.length;
  showWelcome(!has);
  if (has) for (const m of chat.messages) addMessageEl(m.role, m.content, m.tools);
}

/* ───────────────────────────── chat flow ───────────────────────────── */

async function openChat(id) {
  try {
    const data = await api(`/api/chats/${id}`);
    state.chatId = id;
    renderChat(data.chat);
    closeDrawer();
    refreshChatList(); // mark active
  } catch (e) { toast(e.message); }
}

function newChat() {
  state.chatId = null;
  renderChat(null);
  closeDrawer();
  els.input.focus();
}

async function send() {
  const text = els.input.value.trim();
  if (!text || state.busy) return;
  els.input.value = "";
  autoGrow();
  updateSendBtn();

  showWelcome(false);
  addMessageEl("user", text);
  state.busy = true;
  updateSendBtn();
  showTyping();

  try {
    const data = await api("/api/chat", {
      method: "POST",
      body: JSON.stringify({ chat_id: state.chatId, message: text, use_tools: state.toolsOn }),
    });
    $("typingMsg")?.remove();
    state.chatId = data.chat_id;
    addMessageEl("assistant", data.reply, data.tool_events);
    refreshChatList();
    refreshHealth(); // notes count may have changed
  } catch (e) {
    $("typingMsg")?.remove();
    showError(e.message);
  } finally {
    state.busy = false;
    updateSendBtn();
    els.input.focus();
  }
}

function updateSendBtn() {
  els.send.disabled = state.busy || !els.input.value.trim();
  els.send.classList.toggle("busy", state.busy);
}

function autoGrow() {
  els.input.style.height = "auto";
  els.input.style.height = Math.min(els.input.scrollHeight, 132) + "px";
}

/* ───────────────────────────── chats drawer ───────────────────────────── */

function openDrawer() { els.drawer.classList.add("open"); els.drawerScrim.classList.add("show"); }
function closeDrawer() { els.drawer.classList.remove("open"); els.drawerScrim.classList.remove("show"); }

async function refreshChatList() {
  try {
    const data = await api("/api/chats");
    els.chatList.innerHTML = "";
    if (!data.chats.length) {
      els.chatList.innerHTML = '<div class="empty muted">No chats yet — say something!</div>';
      return;
    }
    for (const c of data.chats) {
      const item = document.createElement("div");
      item.className = "chat-item" + (c.id === state.chatId ? " active" : "");
      const title = document.createElement("span");
      title.className = "chat-title";
      title.textContent = c.title;
      const del = document.createElement("button");
      del.className = "chat-del";
      del.textContent = "🗑";
      del.title = "Delete chat";
      del.addEventListener("click", async (ev) => {
        ev.stopPropagation();
        if (!confirm(`Delete chat “${c.title}”?`)) return;
        await api(`/api/chats/${c.id}`, { method: "DELETE" });
        if (c.id === state.chatId) newChat();
        refreshChatList();
      });
      item.append(title, del);
      item.addEventListener("click", () => openChat(c.id));
      els.chatList.appendChild(item);
    }
  } catch { /* server unreachable — ignore */ }
}

/* ───────────────────────────── notes ───────────────────────────── */

let editingNoteId = null;

function openSheet(sheet) { sheet.hidden = false; }
function closeSheet(sheet) { sheet.hidden = true; }

async function refreshNotes() {
  try {
    const q = els.noteSearch.value.trim();
    const data = await api("/api/notes" + (q ? `?q=${encodeURIComponent(q)}` : ""));
    els.notesList.innerHTML = "";
    if (!data.notes.length) {
      els.notesList.innerHTML = `<div class="empty muted">${q ? "No notes match." :
        "No notes yet.<br>Ask the assistant to remember something,<br>or tap ＋ New."}</div>`;
      return;
    }
    for (const n of data.notes) {
      const card = document.createElement("div");
      card.className = "note-card";
      card.innerHTML = `<h3></h3><p class="note-snippet"></p><div class="note-date"></div>`;
      card.querySelector("h3").textContent = n.title || "Untitled";
      card.querySelector(".note-snippet").textContent = n.content.slice(0, 140) || "…";
      card.querySelector(".note-date").textContent = timeAgo(n.updated_at);
      card.addEventListener("click", () => openNoteEditor(n.id));
      els.notesList.appendChild(card);
    }
  } catch (e) { toast(e.message); }
}

async function openNoteEditor(id) {
  try {
    const data = await api("/api/notes");
    const n = data.notes.find((x) => x.id === id);
    if (!n) return;
    editingNoteId = n.id;
    els.noteTitle.value = n.title;
    els.noteContent.value = n.content;
    closeSheet(els.notesSheet);
    openSheet(els.noteEditor);
  } catch (e) { toast(e.message); }
}

function newNote() {
  editingNoteId = null;
  els.noteTitle.value = "";
  els.noteContent.value = "";
  closeSheet(els.notesSheet);
  openSheet(els.noteEditor);
  els.noteTitle.focus();
}

async function saveNote() {
  const payload = { title: els.noteTitle.value, content: els.noteContent.value };
  try {
    if (editingNoteId) {
      await api(`/api/notes/${editingNoteId}`, { method: "PATCH", body: JSON.stringify(payload) });
    } else {
      const data = await api("/api/notes", { method: "POST", body: JSON.stringify(payload) });
      editingNoteId = data.note.id;
    }
    toast("Saved ✓");
    refreshHealth();
  } catch (e) { toast(e.message); }
}

async function deleteNote() {
  if (!editingNoteId || !confirm("Delete this note?")) return;
  try {
    await api(`/api/notes/${editingNoteId}`, { method: "DELETE" });
    closeSheet(els.noteEditor);
    openSheet(els.notesSheet);
    refreshNotes();
    refreshHealth();
    toast("Note deleted");
  } catch (e) { toast(e.message); }
}

/* ───────────────────────────── settings ───────────────────────────── */

async function openSettings() {
  try {
    const [prefs, models] = await Promise.all([api("/api/prefs"), api("/api/models")]);
    $("setBackend").value = prefs.backend;
    $("setApiKey").value = "";
    $("setModel").value = prefs.model || "";
    $("setSystem").value = prefs.system_prompt || "";
    $("setToken").value = getToken();
    $("modelOptions").innerHTML = models.models.map((m) => `<option value="${escHtml(m)}">`).join("");
    state.toolsOn = prefs.tool_mode !== false;
    renderToolsChip();
  } catch (e) { toast(e.message); }
  openSheet(els.settingsSheet);
}

async function saveSettings() {
  const payload = {
    backend: $("setBackend").value.trim(),
    api_key: $("setApiKey").value.trim(),          // blank = keep existing
    model: $("setModel").value.trim(),
    system_prompt: $("setSystem").value,
    tool_mode: state.toolsOn,
  };
  const token = $("setToken").value.trim();
  localStorage.setItem("pocket_token", token);      // client-side PIN
  try {
    const data = await api("/api/prefs", { method: "POST", body: JSON.stringify(payload) });
    closeSheet(els.settingsSheet);
    toast(`Saved — model: ${data.model || "auto"}`);
    refreshHealth();
  } catch (e) {
    if (e.message.includes("token") || e.message.includes("401")) toast("PIN rejected by server");
    else toast(e.message);
  }
}

async function testConnection() {
  const out = $("testResult");
  out.className = "test-result muted";
  out.textContent = "Testing…";
  const payload = {
    backend: $("setBackend").value.trim(),
    api_key: $("setApiKey").value.trim(),
    model: $("setModel").value.trim(),
    tool_mode: state.toolsOn,
  };
  const token = $("setToken").value.trim();
  localStorage.setItem("pocket_token", token);
  try {
    await api("/api/prefs", { method: "POST", body: JSON.stringify(payload) });
    const h = await api("/api/health");
    out.className = "test-result " + (h.models_detected ? "ok" : "err");
    out.textContent = h.models_detected
      ? `✓ Connected — model: ${h.model || "auto"}`
      : `✗ Server reachable, but no model detected at ${h.backend}. Is Ollama running?`;
    refreshHealth();
  } catch (e) {
    out.className = "test-result err";
    out.textContent = "✗ " + e.message;
  }
}

/* ───────────────────────────── health / status ───────────────────────────── */

async function refreshHealth() {
  try {
    const h = await api("/api/health");
    state.health = h;
    els.statusDot.className = "status-dot " + (h.models_detected ? "ok" : "down");
    els.statusDot.title = h.models_detected ? `Model: ${h.model}` : "No model detected — check Settings";
    els.modelLabel.textContent = h.model ? h.model : "no model — open ⚙";
  } catch {
    els.statusDot.className = "status-dot down";
    els.statusDot.title = "Server unreachable";
    els.modelLabel.textContent = "server offline";
  }
}

/* ───────────────────────────── tools toggle ───────────────────────────── */

function renderToolsChip() {
  els.tools.classList.toggle("on", state.toolsOn);
  els.tools.querySelector(".chip-state").textContent = state.toolsOn ? "on" : "off";
}

/* ───────────────────────────── PWA ───────────────────────────── */

if ("serviceWorker" in navigator) {
  window.addEventListener("load", () => {
    navigator.serviceWorker.register("/sw.js").catch(() => { /* http:// LAN IP without https is fine — SW just won't register */ });
  });
}

let installPrompt = null;
window.addEventListener("beforeinstallprompt", (e) => {
  e.preventDefault();
  installPrompt = e;
  const hint = document.querySelector(".install-hint");
  if (hint) {
    hint.innerHTML = "<b>📲 Install as an app</b> — tap here";
    hint.style.cursor = "pointer";
    hint.addEventListener("click", async () => {
      installPrompt.prompt();
      await installPrompt.userChoice;
      installPrompt = null;
      hint.innerHTML = "📲 Installed! Find “Odysseus Pocket” on your home screen.";
    }, { once: true });
  }
});

/* ───────────────────────────── wiring ───────────────────────────── */

els.input.addEventListener("input", () => { autoGrow(); updateSendBtn(); });
els.input.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); }
});
els.send.addEventListener("click", send);

$("btnMenu").addEventListener("click", () => { refreshChatList(); openDrawer(); });
els.drawerScrim.addEventListener("click", closeDrawer);
$("btnNewChat").addEventListener("click", newChat);

$("btnNotes").addEventListener("click", () => { refreshNotes(); openSheet(els.notesSheet); });
$("btnNotesClose").addEventListener("click", () => closeSheet(els.notesSheet));
$("btnNewNote").addEventListener("click", newNote);
els.noteSearch.addEventListener("input", () => refreshNotes());

$("btnNoteClose").addEventListener("click", async () => {
  closeSheet(els.noteEditor);
  openSheet(els.notesSheet);
  refreshNotes();
});
$("btnNoteSave").addEventListener("click", saveNote);
$("btnNoteDelete").addEventListener("click", deleteNote);

$("btnSettings").addEventListener("click", openSettings);
$("btnSettingsClose").addEventListener("click", () => { closeSheet(els.settingsSheet); refreshHealth(); });
$("btnSettingsSave").addEventListener("click", saveSettings);
$("btnTest").addEventListener("click", testConnection);

els.tools.addEventListener("click", () => {
  state.toolsOn = !state.toolsOn;
  renderToolsChip();
  toast(state.toolsOn ? "Tools on — the agent can search the web and use your notes"
                      : "Tools off — plain chat only");
});

// suggestion chips fill the composer
document.querySelectorAll(".suggestion").forEach((btn) => {
  btn.addEventListener("click", () => {
    els.input.value = btn.dataset.fill;
    autoGrow();
    updateSendBtn();
    els.input.focus();
  });
});

// one delegated handler for per-codeblock Copy buttons
els.messages.addEventListener("click", (e) => {
  const btn = e.target.closest(".code-copy");
  if (!btn) return;
  navigator.clipboard?.writeText(decodeURIComponent(btn.dataset.code))
    .then(() => { btn.textContent = "Copied ✓"; setTimeout(() => (btn.textContent = "Copy"), 1500); });
});

/* ───────────────────────────── boot ───────────────────────────── */

(async function boot() {
  try {
    const prefs = await api("/api/prefs");
    state.toolsOn = prefs.tool_mode !== false;
    if (prefs.token_required && !getToken()) toast("This server needs its access PIN — open ⚙ Settings");
  } catch { /* offline: UI still renders, requests will error visibly */ }
  renderToolsChip();
  refreshHealth();
  refreshChatList();
  setInterval(refreshHealth, 30000);

  // reopen the most recent chat
  try {
    const data = await api("/api/chats");
    if (data.chats.length) await openChat(data.chats[0].id);
  } catch { /* ignore */ }
})();
