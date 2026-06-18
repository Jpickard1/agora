/* Agent Hub web UI.
 *
 * Talks to the FastAPI backend: REST for history + sending, SSE for live
 * updates. The token is kept in localStorage and sent as X-Hub-Token (header)
 * for fetch calls and ?token= for the EventSource stream.
 *
 * Two view modes:
 *   channel  -> read/post to a broadcast channel (#general, ...)
 *   agent    -> read an agent's inbox + send it directed instructions
 */

const LS_KEY = "agenthub.token";
const LS_NAME = "agenthub.name";

const state = {
  token: localStorage.getItem(LS_KEY) || "",
  name: localStorage.getItem(LS_NAME) || "jpic",
  view: { type: "channel", id: "general" },
  channels: [],
  agents: [],
  messages: [],     // currently displayed
  seenIds: new Set(),
  es: null,
};

const $ = (sel) => document.querySelector(sel);

/* ---------------- API helpers ---------------- */
async function api(path, opts = {}) {
  const headers = Object.assign({ "Content-Type": "application/json" }, opts.headers || {});
  if (state.token) headers["X-Hub-Token"] = state.token;
  const res = await fetch(path, Object.assign({}, opts, { headers }));
  if (res.status === 401) throw new Error("unauthorized");
  if (!res.ok) {
    let detail = "http " + res.status;
    try { const j = await res.json(); if (j && j.detail) detail = j.detail; } catch (_) { /* keep default */ }
    throw new Error(detail);
  }
  return res.status === 204 ? null : res.json();
}

/* ---------------- auth gate ---------------- */
async function tryConnect() {
  const health = await fetch("/api/health").then((r) => r.json());
  if (!health.auth_required) {
    state.token = "";
    return start();
  }
  if (state.token) {
    try {
      await api("/api/channels");
      return start();
    } catch (_) { /* fall through to gate */ }
  }
  showGate();
}

function showGate(msg) {
  $("#gate").classList.remove("hidden");
  $("#app").classList.add("hidden");
  if (msg) $("#token-err").textContent = msg;
  $("#token-input").focus();
}

$("#token-btn").onclick = async () => {
  const t = $("#token-input").value.trim();
  if (!t) return;
  try {
    await api("/api/auth/check", { method: "POST", headers: { "X-Hub-Token": t }, body: JSON.stringify({ token: t }) });
    state.token = t;
    localStorage.setItem(LS_KEY, t);
    start();
  } catch (e) {
    $("#token-err").textContent = "Invalid token.";
  }
};
$("#token-input").addEventListener("keydown", (e) => { if (e.key === "Enter") $("#token-btn").click(); });

/* ---------------- startup ---------------- */
async function start() {
  $("#gate").classList.add("hidden");
  $("#app").classList.remove("hidden");
  await refreshChannels();
  await refreshAgents();
  await selectView({ type: "channel", id: "general" });
  openStream();
}

function openStream() {
  if (state.es) state.es.close();
  const url = "/api/stream" + (state.token ? "?token=" + encodeURIComponent(state.token) : "");
  const es = new EventSource(url);
  state.es = es;
  es.onopen = () => setConn(true);
  es.onerror = () => setConn(false);
  es.onmessage = (ev) => {
    let data;
    try { data = JSON.parse(ev.data); } catch (_) { return; }
    handleEvent(data);
  };
}

function setConn(ok) {
  $("#conn-dot").className = "dot " + (ok ? "ok" : "bad");
  $("#conn-text").textContent = ok ? "live" : "reconnecting…";
}

function handleEvent(data) {
  if (data.type === "agents") {
    state.agents = data.agents;
    renderAgents();
  } else if (data.type === "message") {
    const m = data.message;
    if (state.view.type === "channel" && m.channel === state.view.id) appendMessage(m);
    if (state.view.type === "firehose") appendMessage(m);
  } else if (data.type === "inbox") {
    const m = data.message;
    if (state.view.type === "agent" && m.to === state.view.id) appendMessage(m);
  } else if (data.type === "broadcast") {
    const m = data.message;
    if (state.view.type === "broadcast" || state.view.type === "firehose") appendMessage(m);
  }
}

/* ---------------- sidebar ---------------- */
async function refreshChannels() {
  state.channels = await api("/api/channels");
  const ul = $("#channel-list");
  ul.innerHTML = "";
  state.channels.forEach((c) => {
    const li = document.createElement("li");
    li.innerHTML = `<span class="hash">#</span><span>${esc(c.name)}</span>`;
    li.onclick = () => selectView({ type: "channel", id: c.name });
    if (state.view.type === "channel" && state.view.id === c.name) li.classList.add("active");
    ul.appendChild(li);
  });
}

async function refreshAgents() {
  try { state.agents = await api("/api/agents"); } catch (_) { return; }
  renderAgents();
  renderDmList();
}

// Map an agent record to a roster status badge. Offline always wins (a crashed
// bridge can leave a stale "working" status), otherwise use the bridge-reported
// status: working | waiting | listening (default for an online agent).
function agentStatus(a) {
  if (!a.online) return { label: "offline", cls: "st-offline" };
  const s = (a.status || "").toLowerCase();
  if (s === "working") return { label: "working", cls: "st-working" };
  if (s === "waiting") return { label: "waiting", cls: "st-waiting" };
  return { label: "listening", cls: "st-listening" };
}

function renderAgents() {
  const ul = $("#agent-list");
  ul.innerHTML = "";
  const online = state.agents.filter((a) => a.online).length;
  $("#agent-count").textContent = `${online}/${state.agents.length}`;
  if (!state.agents.length) {
    ul.innerHTML = `<li class="empty" style="margin-top:10px">no agents yet</li>`;
  }
  state.agents.forEach((a) => {
    const li = document.createElement("li");
    li.className = "agent";
    const caps = (a.capabilities || []).join(", ");
    const sess = a.tmux_session || (a.extra && a.extra.tmux_session) || "";
    const st = agentStatus(a);
    li.innerHTML = `
      <div class="row1">
        <span class="pdot ${a.online ? "online" : ""}"></span>
        <span class="aname">${esc(a.name)}</span>
        <span class="status-badge ${st.cls}">${st.label}</span>
      </div>
      ${a.activity ? `<div class="ameta work">▸ ${esc(a.activity)}</div>` : ""}
      <div class="ameta">🖥 ${esc(a.host || "?")}${sess ? ` · ⧉ ${esc(sess)}` : ""}</div>
      <div class="ameta">${a.online ? "seen just now" : "seen " + rel(a.age)}</div>
      ${caps ? `<div class="caps">${esc(caps)}</div>` : ""}`;
    li.title = "Click to send a direct instruction";
    li.onclick = () => selectView({ type: "agent", id: a.id, name: a.name });
    ul.appendChild(li);
  });
  renderDmList();
}

function renderDmList() {
  const ul = $("#dm-list");
  ul.innerHTML = "";
  state.agents.forEach((a) => {
    const li = document.createElement("li");
    li.innerHTML = `<span class="pdot ${a.online ? "online" : ""}" style="margin-right:2px"></span><span>${esc(a.name)}</span>`;
    li.onclick = () => selectView({ type: "agent", id: a.id, name: a.name });
    if (state.view.type === "agent" && state.view.id === a.id) li.classList.add("active");
    ul.appendChild(li);
  });
}

/* ---------------- views ---------------- */
async function selectView(view) {
  state.view = view;
  state.seenIds = new Set();
  $("#messages").innerHTML = "";
  refreshChannels();
  renderDmList();

  const composer = $("#composer");
  composer.classList.remove("instruct");
  composer.style.display = "flex";
  if (view.type === "channel") {
    $("#view-title").textContent = "# " + view.id;
    $("#view-sub").textContent = "broadcast channel — every agent can read & post";
    $("#msg-input").placeholder = `Message #${view.id}`;
    renderMessages(await api(`/api/channels/${encodeURIComponent(view.id)}/messages?limit=200`));
  } else if (view.type === "agent") {
    $("#view-title").textContent = "🧑→🤖 " + (view.name || view.id);
    $("#view-sub").textContent = "direct instructions to " + view.id;
    $("#msg-input").placeholder = `Instruct ${view.name || view.id}…`;
    composer.classList.add("instruct");
    renderMessages(await api(`/api/agents/${encodeURIComponent(view.id)}/inbox?limit=200`));
  } else if (view.type === "firehose") {
    $("#view-title").textContent = "📡 All activity";
    $("#view-sub").textContent = "every channel + broadcast, merged — read-only";
    composer.style.display = "none";
    renderMessages(await api(`/api/firehose?limit=300`));
  } else if (view.type === "broadcast") {
    $("#view-title").textContent = "📢 Broadcast to all agents";
    $("#view-sub").textContent = "instruction delivered to every agent (now and future)";
    $("#msg-input").placeholder = "Instruct ALL agents…";
    composer.classList.add("instruct");
    renderMessages(await api(`/api/broadcast?limit=200`));
  }
}

function renderMessages(msgs) {
  const box = $("#messages");
  box.innerHTML = "";
  state.seenIds = new Set();
  if (!msgs.length) {
    box.innerHTML = `<div class="empty">No messages yet.</div>`;
    return;
  }
  msgs.forEach((m) => appendMessage(m, false));
  scrollDown();
}

function appendMessage(m, scroll = true) {
  if (state.seenIds.has(m.id)) return;
  state.seenIds.add(m.id);
  const box = $("#messages");
  const first = box.querySelector(".empty");
  if (first) first.remove();
  const kind = m.author_kind || "agent";
  const avatar = kind === "human" ? "🧑" : kind === "system" ? "⚙️" : "🤖";
  const directed = m.to ? "directed" : "";
  const img = m.meta && m.meta.image
    ? `<img class="msg-img" src="${esc(m.meta.image)}" alt="${esc((m.meta && m.meta.filename) || "image")}" onclick="window.open(this.src,'_blank')" />`
    : "";
  const el = document.createElement("div");
  el.className = "msg " + directed;
  el.innerHTML = `
    <div class="avatar">${avatar}</div>
    <div class="body">
      <div class="head">
        <span class="author ${kind}">${esc(m.author_name || m.author)}</span>
        ${m.host ? `<span class="host">${esc(m.host)}</span>` : ""}
        <span class="time">${fmtTime(m.ts)}</span>
      </div>
      <div class="text">${renderMarkdown(m.text)}</div>
      ${img}
    </div>`;
  box.appendChild(el);
  if (scroll) scrollDown();
}

function scrollDown() {
  const box = $("#messages");
  box.scrollTop = box.scrollHeight;
}

/* ---------------- composer ---------------- */
const msgInput = $("#msg-input");

// Enter sends; Shift+Enter inserts a newline. The box stays a fixed size and
// just scrolls internally for long / multi-line messages.
msgInput.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    $("#composer").requestSubmit();
  }
});

$("#composer").addEventListener("submit", async (e) => {
  e.preventDefault();
  const text = msgInput.value.trim();
  if (!text) return;
  msgInput.value = "";
  const body = JSON.stringify({ text, author_name: state.name });
  try {
    if (state.view.type === "channel") {
      appendMessage(await api(`/api/channels/${encodeURIComponent(state.view.id)}/messages`, { method: "POST", body }));
    } else if (state.view.type === "agent") {
      appendMessage(await api(`/api/agents/${encodeURIComponent(state.view.id)}/inbox`, { method: "POST", body }));
    } else if (state.view.type === "broadcast") {
      const r = await api(`/api/broadcast`, { method: "POST", body });
      if (r && r.id) appendMessage(r);
    }
  } catch (err) {
    msgInput.value = text;    // restore on failure
  }
});

/* ---------------- image attachments ---------------- */
function fileToBase64(file) {
  return new Promise((resolve, reject) => {
    const r = new FileReader();
    r.onload = () => resolve(r.result);          // a data: URL
    r.onerror = reject;
    r.readAsDataURL(file);
  });
}

async function postMessage(body) {
  if (state.view.type === "channel") {
    return api(`/api/channels/${encodeURIComponent(state.view.id)}/messages`, { method: "POST", body });
  } else if (state.view.type === "agent") {
    return api(`/api/agents/${encodeURIComponent(state.view.id)}/inbox`, { method: "POST", body });
  } else if (state.view.type === "broadcast") {
    return api(`/api/broadcast`, { method: "POST", body });
  }
}

$("#attach-btn").onclick = () => $("#file-input").click();

$("#file-input").addEventListener("change", async (e) => {
  const file = e.target.files[0];
  if (!file) return;
  e.target.value = "";                            // allow re-selecting the same file
  if (state.view.type === "firehose") return;     // read-only view
  try {
    const dataUrl = await fileToBase64(file);
    const up = await api("/api/upload", { method: "POST",
      body: JSON.stringify({ filename: file.name, data_base64: dataUrl }) });
    const caption = msgInput.value.trim();
    msgInput.value = "";
    const body = JSON.stringify({
      text: caption || "📷 [image]", author_name: state.name,
      meta: { image: up.url, filename: file.name },
    });
    const m = await postMessage(body);
    if (m && m.id) appendMessage(m);
  } catch (err) {
    alert("Image upload failed: " + err.message);
  }
});

$("#nav-firehose").onclick = () => selectView({ type: "firehose" });
$("#nav-broadcast").onclick = () => selectView({ type: "broadcast" });

$("#add-channel").onclick = async () => {
  const name = prompt("New channel name:");
  if (!name) return;
  await api("/api/channels", { method: "POST", body: JSON.stringify({ name }) });
  await refreshChannels();
  selectView({ type: "channel", id: name.trim().toLowerCase().replace(/[^a-z0-9_.@-]+/g, "-") });
};

/* ---------------- spawn a new agent (users only) ---------------- */
const spawnModal = $("#spawn-modal");
function openSpawn() {
  $("#spawn-err").textContent = "";
  spawnModal.classList.remove("hidden");
  $("#spawn-name").focus();
}
function closeSpawn() { spawnModal.classList.add("hidden"); }

$("#add-agent").onclick = openSpawn;
$("#spawn-cancel").onclick = closeSpawn;
// click the dark backdrop (not the card) to dismiss
spawnModal.addEventListener("click", (e) => { if (e.target === spawnModal) closeSpawn(); });

$("#spawn-go").onclick = async () => {
  const name = $("#spawn-name").value.trim();
  const path = $("#spawn-path").value.trim();
  const tasks = $("#spawn-tasks").value.trim();
  const machine = $("#spawn-machine").value.trim();
  const session = $("#spawn-session").value.trim();
  const err = $("#spawn-err");
  err.textContent = "";
  if (!name || !path) { err.textContent = "Agent name and creation path are required."; return; }
  const go = $("#spawn-go");
  go.disabled = true;
  go.textContent = "Creating…";
  try {
    await api("/api/agents/spawn", { method: "POST",
      body: JSON.stringify({ name, path, tasks, machine, session }) });
    ["#spawn-name", "#spawn-path", "#spawn-tasks", "#spawn-machine", "#spawn-session"]
      .forEach((s) => { $(s).value = ""; });
    closeSpawn();
    selectView({ type: "channel", id: "general" });   // watch it announce itself
  } catch (e) {
    err.textContent = "Could not create agent: " + e.message;
  } finally {
    go.disabled = false;
    go.textContent = "Create & connect";
  }
};

/* periodic agent refresh as a backstop to the SSE presence push */
setInterval(refreshAgents, 8000);

/* ---------------- utils ---------------- */
function esc(s) {
  return String(s == null ? "" : s).replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
  ));
}

// Only http(s) links are allowed (no javascript:/data: URLs).
function safeUrl(u) {
  return /^https?:\/\//i.test(u) ? u : "#";
}

// Lightweight, safe Markdown for chat messages. Strategy: pull code spans and
// links out into placeholders FIRST (so their contents are never re-processed),
// HTML-escape everything else, then apply emphasis. Code/link HTML is built with
// esc() so nothing user-supplied can inject markup. `.text` is white-space:
// pre-wrap, so newlines render as-is — we don't add <br>.
function renderMarkdown(src) {
  src = String(src == null ? "" : src);
  const tokens = [];
  const stash = (html) => " " + (tokens.push(html) - 1) + " ";

  // fenced code blocks ```...```
  src = src.replace(/```([\s\S]*?)```/g, (_, c) =>
    stash('<pre class="code-block"><code>' + esc(c.replace(/^\n/, "").replace(/\n$/, "")) + "</code></pre>"));
  // inline code `...`
  src = src.replace(/`([^`\n]+)`/g, (_, c) =>
    stash('<code class="inline-code">' + esc(c) + "</code>"));
  // markdown links [text](url)
  src = src.replace(/\[([^\]]+)\]\(\s*(https?:\/\/[^\s)]+)\s*\)/g, (_, t, u) =>
    stash('<a href="' + esc(safeUrl(u)) + '" target="_blank" rel="noopener noreferrer">' + esc(t) + "</a>"));
  // bare URLs
  src = src.replace(/(https?:\/\/[^\s<>()]+)/g, (u) =>
    stash('<a href="' + esc(safeUrl(u)) + '" target="_blank" rel="noopener noreferrer">' + esc(u) + "</a>"));

  // escape the remaining plain text, then apply emphasis (tag-only, safe)
  src = esc(src);
  src = src.replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>");
  src = src.replace(/__([^_\n]+)__/g, "<strong>$1</strong>");
  src = src.replace(/(^|[^*])\*([^*\n]+)\*/g, "$1<em>$2</em>");

  // restore code/link HTML
  return src.replace(/ (\d+) /g, (_, i) => tokens[+i]);
}
function fmtTime(ts) {
  const d = new Date(ts * 1000);
  return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}
function rel(age) {
  age = age || 0;
  if (age < 60) return Math.round(age) + "s ago";
  if (age < 3600) return Math.round(age / 60) + "m ago";
  if (age < 86400) return Math.round(age / 3600) + "h ago";
  return Math.round(age / 86400) + "d ago";
}

tryConnect();
