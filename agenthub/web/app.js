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
  tasks: [],        // durable task-board state (live via SSE)
  usage: null,      // utilization snapshot for the usage panel
  messages: [],     // currently displayed
  seenIds: new Set(),
  dismissed: new Set(),  // alert message ids the user dismissed
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
    refreshUsage();
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
  } else if (data.type === "tasks") {
    // Only re-render when the task set actually changed — re-rendering on every
    // tick rebuilds the DOM and would reset the board's horizontal scroll.
    const sig = JSON.stringify((data.tasks || []).map(
      (t) => [t.id, t.status, t.claimed_by, t.title]));
    state.tasks = data.tasks;
    if (sig !== state._tasksSig) {
      state._tasksSig = sig;
      if (state.view.type === "taskboard") renderTaskBoard();
    }
    refreshUsage();
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

function agentLi(a) {
  const li = document.createElement("li");
  li.className = "agent" + (a.retired ? " retired" : "");
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
  return li;
}

function renderAgents() {
  const ul = $("#agent-list");
  ul.innerHTML = "";
  // Auto-retire (issue #11): split long-offline agents into a collapsed group.
  const active = state.agents.filter((a) => !a.retired);
  const retired = state.agents.filter((a) => a.retired);
  const online = state.agents.filter((a) => a.online).length;
  $("#agent-count").textContent = `${online}/${active.length}`;
  if (!state.agents.length) {
    ul.innerHTML = `<li class="empty" style="margin-top:10px">no agents yet</li>`;
  }
  active.forEach((a) => ul.appendChild(agentLi(a)));

  if (retired.length) {
    const head = document.createElement("li");
    head.className = "retired-head";
    head.innerHTML = `<span>${state.showRetired ? "▾" : "▸"} Retired</span>`
      + `<span class="count">${retired.length}</span>`;
    head.title = "Agents offline a long time — click to " +
      (state.showRetired ? "hide" : "show");
    head.onclick = () => { state.showRetired = !state.showRetired; renderAgents(); };
    ul.appendChild(head);
    if (state.showRetired) retired.forEach((a) => ul.appendChild(agentLi(a)));
  }
  renderDmList();
}

function renderDmList() {
  const ul = $("#dm-list");
  ul.innerHTML = "";
  // Keep the DM list focused on current agents; retired ones are hidden.
  state.agents.filter((a) => !a.retired).forEach((a) => {
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
  } else if (view.type === "taskboard") {
    $("#view-title").textContent = "📋 Task board";
    $("#view-sub").textContent = "durable dispatch state — updates live";
    composer.style.display = "none";
    state.tasks = await api(`/api/tasks`);
    renderTaskBoard();
  } else if (view.type === "usage") {
    $("#view-title").textContent = "📊 Usage & efficiency";
    $("#view-sub").textContent = "system utilization — per-agent activity + host load";
    composer.style.display = "none";
    state.usage = await api(`/api/usage`);
    renderUsage();
  } else if (view.type === "kb") {
    $("#view-title").textContent = "📚 Knowledge base";
    $("#view-sub").textContent = "shared, searchable notes / links / artifacts — consult before duplicating work";
    composer.style.display = "none";
    await refreshKb();
  } else if (view.type === "projects") {
    $("#view-title").textContent = "📁 Projects";
    $("#view-sub").textContent = "group tasks & channels under a goal — live progress rollup";
    composer.style.display = "none";
    await refreshProjects();
  }
}

/* ---------------- projects (issue #22) ---------------- */
async function refreshProjects() {
  if (state.view.type !== "projects") return;
  try { state.projects_v = await api("/api/projects"); } catch (_) { state.projects_v = []; }
  renderProjects();
}

function pjBar(pct) {
  const cls = (pct || 0) >= 100 ? "done" : "";
  return `<div class="pj-bar"><div class="pj-fill ${cls}" style="width:${Math.min(100, pct || 0)}%"></div></div>`;
}

function renderProjects() {
  const box = $("#messages");
  const projects = state.projects_v || [];
  const cards = projects.map((p) => {
    const pr = p.progress || {};
    const owner = p.owner ? `<span class="pj-owner">@${esc(p.owner)}</span>` : "";
    const ms = (p.milestones || []).map((m) =>
      `<span class="pj-ms ${m.done ? "done" : ""}">${m.done ? "☑" : "☐"} ${esc(m.name)}</span>`).join("");
    const chans = (p.channels || []).map((c) =>
      `<span class="pj-chip" data-ch="${esc(c)}">#${esc(c)}</span>`).join("");
    const tasks = (p.task_ids || []).map((t) => `<span class="pj-chip">${esc(t)}</span>`).join("");
    return `<div class="pj-card">
      <div class="pj-head">
        <span class="pj-name">📁 ${esc(p.name || p.id)}</span>${owner}
        <button class="pj-del" data-id="${esc(p.id)}" title="delete">✕</button>
      </div>
      ${p.goal ? `<div class="pj-goal">${esc(p.goal)}</div>` : ""}
      ${pjBar(pr.percent)}
      <div class="pj-stats">${pr.percent || 0}% · ${pr.done || 0}/${pr.total_tasks || 0} tasks ·
        ${pr.milestones_done || 0}/${pr.milestones_total || 0} milestones</div>
      ${ms ? `<div class="pj-mss">${ms}</div>` : ""}
      ${tasks ? `<div class="pj-row"><span class="pj-lbl">tasks</span>${tasks}</div>` : ""}
      ${chans ? `<div class="pj-row"><span class="pj-lbl">channels</span>${chans}</div>` : ""}
    </div>`;
  }).join("") || `<div class="empty">No projects yet.</div>`;

  box.innerHTML = `
    <div class="pj">
      <div class="pj-top"><button id="pj-add-btn">＋ New project</button></div>
      <div id="pj-form" class="kb-form hidden">
        <input id="pj-f-name" type="text" placeholder="Project name" />
        <input id="pj-f-goal" type="text" placeholder="Goal" />
        <input id="pj-f-owner" type="text" placeholder="Owner (agent id)" />
        <div class="kb-form-actions">
          <button id="pj-f-cancel" class="btn-secondary">Cancel</button>
          <button id="pj-f-save">Create project</button>
        </div>
        <div id="pj-f-err" class="err"></div>
      </div>
      <div class="pj-list">${cards}</div>
    </div>`;

  $("#pj-add-btn").onclick = () => $("#pj-form").classList.toggle("hidden");
  $("#pj-f-cancel").onclick = () => $("#pj-form").classList.add("hidden");
  $("#pj-f-save").onclick = async () => {
    const name = $("#pj-f-name").value.trim();
    const err = $("#pj-f-err");
    if (!name) { err.textContent = "Name is required."; return; }
    const body = JSON.stringify({
      id: name, name, goal: $("#pj-f-goal").value.trim(),
      owner: $("#pj-f-owner").value.trim(), author: state.name,
    });
    try { await api("/api/projects", { method: "POST", body }); refreshProjects(); }
    catch (e) { err.textContent = "Could not create: " + e.message; }
  };
  document.querySelectorAll(".pj-del").forEach((el) => {
    el.onclick = async () => {
      if (!confirm("Delete this project? (its tasks/channels are not deleted)")) return;
      try { await api("/api/projects/" + encodeURIComponent(el.dataset.id), { method: "DELETE" }); } catch (_) {}
      refreshProjects();
    };
  });
  document.querySelectorAll(".pj-chip[data-ch]").forEach((el) => {
    el.onclick = () => selectView({ type: "channel", id: el.dataset.ch });
  });
}

/* ---------------- knowledge base (issue #25) ---------------- */
async function refreshKb() {
  const q = (state.kbQuery || "").trim();
  const tag = state.kbTag || "";
  const params = new URLSearchParams();
  if (q) params.set("q", q);
  if (tag) params.set("tag", tag);
  try {
    state.kb = await api("/api/kb" + (params.toString() ? "?" + params : ""));
  } catch (_) { state.kb = { entries: [], tags: {} }; }
  renderKb();
}

function kbIcon(kind) {
  return kind === "link" ? "🔗" : kind === "artifact" ? "📦" : "📝";
}

function renderKb() {
  const box = $("#messages");
  const kb = state.kb || { entries: [], tags: {} };
  const tags = kb.tags || {};
  const tagChips = Object.keys(tags).map((t) => {
    const on = state.kbTag === t ? " on" : "";
    return `<span class="kb-tag${on}" data-tag="${esc(t)}">#${esc(t)} <em>${tags[t]}</em></span>`;
  }).join("");
  const entries = (kb.entries || []).map((e) => {
    const tagline = (e.tags || []).map((t) => `#${esc(t)}`).join(" ");
    const url = e.url
      ? `<a class="kb-url" href="${esc(safeUrl(e.url))}" target="_blank" rel="noopener noreferrer">${esc(e.url)}</a>`
      : "";
    const body = e.body ? `<div class="kb-body">${renderMarkdown(e.body)}</div>` : "";
    return `<div class="kb-entry" data-id="${esc(e.id)}">
      <div class="kb-head">
        <span class="kb-icon">${kbIcon(e.kind)}</span>
        <span class="kb-title">${esc(e.title)}</span>
        <span class="kb-tags">${esc(tagline)}</span>
        <button class="kb-del" data-id="${esc(e.id)}" title="delete">✕</button>
      </div>
      ${url}${body}
      <div class="kb-meta">${esc(e.author_name || e.author || "?")} · ${fmtTime(e.updated_ts)}</div>
    </div>`;
  }).join("") || `<div class="empty">No entries${state.kbQuery ? " match your search" : " yet"}.</div>`;

  box.innerHTML = `
    <div class="kb">
      <div class="kb-bar">
        <input id="kb-search" type="search" placeholder="Search the knowledge base…" value="${esc(state.kbQuery || "")}" />
        <button id="kb-add-btn">＋ New entry</button>
      </div>
      ${state.kbTag ? `<div class="kb-filter">filtering by <b>#${esc(state.kbTag)}</b> <span id="kb-clear-tag">clear ✕</span></div>` : ""}
      ${tagChips ? `<div class="kb-tagcloud">${tagChips}</div>` : ""}
      <div id="kb-form" class="kb-form hidden">
        <input id="kb-f-title" type="text" placeholder="Title" />
        <div class="kb-form-row">
          <select id="kb-f-kind"><option value="note">note</option><option value="link">link</option><option value="artifact">artifact</option></select>
          <input id="kb-f-url" type="text" placeholder="URL (for links/artifacts)" />
          <input id="kb-f-tags" type="text" placeholder="tags, comma,separated" />
        </div>
        <textarea id="kb-f-body" rows="4" placeholder="Markdown body…"></textarea>
        <div class="kb-form-actions">
          <button id="kb-f-cancel" class="btn-secondary">Cancel</button>
          <button id="kb-f-save">Save entry</button>
        </div>
        <div id="kb-f-err" class="err"></div>
      </div>
      <div class="kb-list">${entries}</div>
    </div>`;

  // search (debounced-ish: on input)
  const search = $("#kb-search");
  search.oninput = () => { state.kbQuery = search.value; clearTimeout(state._kbT);
    state._kbT = setTimeout(refreshKb, 200); };
  $("#kb-add-btn").onclick = () => $("#kb-form").classList.toggle("hidden");
  if ($("#kb-clear-tag")) $("#kb-clear-tag").onclick = () => { state.kbTag = ""; refreshKb(); };
  document.querySelectorAll(".kb-tag").forEach((el) => {
    el.onclick = () => { state.kbTag = (state.kbTag === el.dataset.tag) ? "" : el.dataset.tag; refreshKb(); };
  });
  document.querySelectorAll(".kb-del").forEach((el) => {
    el.onclick = async () => {
      if (!confirm("Delete this KB entry?")) return;
      try { await api("/api/kb/" + encodeURIComponent(el.dataset.id), { method: "DELETE" }); } catch (_) {}
      refreshKb();
    };
  });
  $("#kb-f-cancel").onclick = () => $("#kb-form").classList.add("hidden");
  $("#kb-f-save").onclick = async () => {
    const title = $("#kb-f-title").value.trim();
    const err = $("#kb-f-err");
    if (!title) { err.textContent = "Title is required."; return; }
    const body = JSON.stringify({
      title, body: $("#kb-f-body").value, kind: $("#kb-f-kind").value,
      url: $("#kb-f-url").value.trim(), tags: $("#kb-f-tags").value,
      author_name: state.name,
    });
    try {
      await api("/api/kb", { method: "POST", body });
      state.kbQuery = "";
      refreshKb();
    } catch (e) { err.textContent = "Could not save: " + e.message; }
  };
}

// Live utilization panel: totals + host load gauges + per-agent activity table.
async function refreshUsage() {
  if (state.view.type !== "usage") return;
  try { state.usage = await api(`/api/usage`); } catch (_) { return; }
  renderUsage();
}

function gauge(label, pct, detail) {
  if (pct == null) return "";
  const cls = pct >= 90 ? "hot" : pct >= 70 ? "warm" : "";
  return `<div class="ug">
    <div class="ug-head"><span>${esc(label)}</span><span>${pct}%${detail ? " · " + esc(detail) : ""}</span></div>
    <div class="ug-bar"><div class="ug-fill ${cls}" style="width:${Math.min(100, pct)}%"></div></div>
  </div>`;
}

function renderUsage() {
  const box = $("#messages");
  const u = state.usage || { totals: {}, agents: [], host: {} };
  const t = u.totals || {};
  const h = u.host || {};
  const stat = (val, lab) =>
    `<div class="ustat"><div class="ustat-n">${val}</div><div class="ustat-l">${esc(lab)}</div></div>`;
  const stats = [
    stat(`${t.online ?? 0}/${t.agents ?? 0}`, "agents online"),
    stat(t.messages ?? 0, "messages"),
    stat(t.tasks ?? 0, "tasks"),
    stat(t.tasks_done ?? 0, "tasks done"),
    stat(t.tasks_per_agent ?? 0, "tasks / agent"),
  ].join("");
  const gauges =
    gauge("CPU", h.cpu_percent, null) +
    gauge("Memory", h.mem_percent,
          h.mem_used_gb != null ? `${h.mem_used_gb}/${h.mem_total_gb} GB` : null) +
    (h.load1 != null ? `<div class="ug"><div class="ug-head"><span>Load (1m)</span><span>${h.load1}</span></div></div>` : "");
  const rows = (u.agents || []).map((a) => {
    const dot = a.online ? "online" : "";
    const act = a.activity ? ` <span class="ut-act">▸ ${esc(a.activity)}</span>` : "";
    return `<tr>
      <td><span class="pdot ${dot}"></span>${esc(a.name)}${act}</td>
      <td class="num">${a.messages}</td>
      <td class="num">${a.tasks_total}</td>
      <td class="num">${a.tasks_done}</td>
      <td class="num">${a.tasks_running}</td>
      <td>${esc(a.host || "?")}</td>
    </tr>`;
  }).join("") || `<tr><td colspan="6" class="tb-empty">no agents</td></tr>`;
  box.innerHTML = `
    <div class="usage">
      <div class="ustat-row">${stats}</div>
      <div class="usect-head">🖥 Host — ${esc(h.host || "?")}</div>
      <div class="ugauges">${gauges || '<div class="tb-empty">host metrics unavailable</div>'}</div>
      <div class="usect-head">Per-agent activity</div>
      <table class="utable">
        <thead><tr><th>Agent</th><th class="num">Msgs</th><th class="num">Tasks</th>
          <th class="num">Done</th><th class="num">Running</th><th>Host</th></tr></thead>
        <tbody>${rows}</tbody>
      </table>
      <div class="unote">${esc(u.token_tracking || "")}</div>
    </div>`;
}

// Live task board: columns by status, each card shows id / title / assignee.
function renderTaskBoard() {
  const box = $("#messages");
  const cols = [
    { key: "open", label: "Open" },
    { key: "claimed", label: "Claimed" },
    { key: "running", label: "Running" },
    { key: "done", label: "Done" },
  ];
  const other = (state.tasks || []).filter(
    (t) => !cols.some((c) => c.key === t.status));
  const colHtml = cols.map((c) => {
    const items = (state.tasks || []).filter((t) => t.status === c.key);
    const cards = items.map(taskCard).join("") ||
      `<div class="tb-empty">—</div>`;
    return `<div class="tb-col"><div class="tb-col-head">${c.label} <span class="tb-count">${items.length}</span></div>${cards}</div>`;
  }).join("");
  const extra = other.length
    ? `<div class="tb-col"><div class="tb-col-head">Other <span class="tb-count">${other.length}</span></div>${other.map(taskCard).join("")}</div>`
    : "";
  // preserve horizontal scroll across re-renders so the view doesn't snap back
  const prev = box.querySelector(".taskboard");
  const sx = prev ? prev.scrollLeft : 0;
  box.innerHTML = `<div class="taskboard">${colHtml}${extra}</div>`;
  const now = box.querySelector(".taskboard");
  if (now) now.scrollLeft = sx;
}

// Parse a task ref like "owner/repo#123" into its GitHub issue URL, else "".
function issueUrl(ref) {
  const m = /^([^/\s]+)\/([^#\s]+)#(\d+)$/.exec((ref || "").trim());
  return m ? `https://github.com/${m[1]}/${m[2]}/issues/${m[3]}` : "";
}

function taskCard(t) {
  const who = t.claimed_by ? `🤖 ${esc(t.claimed_by)}` : "unassigned";
  const ref = t.ref ? `<span class="tb-ref">${esc(t.ref)}</span>` : "";
  const title = esc(t.title || t.id);
  const url = issueUrl(t.ref);
  // Clicking the title opens its GitHub issue (when the ref points to one).
  const titleHtml = url
    ? `<a class="tb-title tb-title-link" href="${esc(url)}" target="_blank" rel="noopener">${title}</a>`
    : `<div class="tb-title">${title}</div>`;
  return `<div class="tb-card tb-${esc(t.status)}">
    ${titleHtml}
    <div class="tb-meta"><span class="tb-id">${esc(t.id)}</span>${ref}</div>
    <div class="tb-meta">${who}${t.cap ? ` · ${esc(t.cap)}` : ""}</div>
  </div>`;
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
  showAlertBanner(latestAlert(msgs));   // pin the most recent unread alert
}

// The most recent non-dismissed alert in a set of messages (or null).
function latestAlert(msgs) {
  for (let i = (msgs || []).length - 1; i >= 0; i--) {
    const m = msgs[i];
    if (m.meta && m.meta.alert && !state.dismissed.has(m.id)) return m;
  }
  return null;
}

// Pin a must-read alert as a sticky banner at the top of the message pane.
function showAlertBanner(m) {
  const box = $("#messages");
  if (!box) return;
  const existing = box.querySelector(".alert-banner");
  if (existing) existing.remove();
  if (!m) return;
  const b = document.createElement("div");
  b.className = "alert-banner";
  b.innerHTML = `<span class="alert-tag">🚨 ALERT</span>` +
    `<span>${esc(m.text)} — <em>${esc(m.author_name || m.author)}</em></span>` +
    `<button class="ab-dismiss" title="dismiss">✕</button>`;
  b.querySelector(".ab-dismiss").onclick = () => { state.dismissed.add(m.id); b.remove(); };
  box.insertBefore(b, box.firstChild);
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
  const isAlert = !!(m.meta && m.meta.alert);
  const el = document.createElement("div");
  el.className = "msg " + directed + (isAlert ? " alert" : "");
  el.innerHTML = `
    <div class="avatar">${isAlert ? "🚨" : avatar}</div>
    <div class="body">
      <div class="head">
        ${isAlert ? `<span class="alert-tag">🚨 ALERT</span>` : ""}
        <span class="author ${kind}">${esc(m.author_name || m.author)}</span>
        ${m.host ? `<span class="host">${esc(m.host)}</span>` : ""}
        <span class="time">${fmtTime(m.ts)}</span>
      </div>
      <div class="text">${renderMarkdown(m.text)}</div>
      ${img}
    </div>`;
  box.appendChild(el);
  if (scroll) scrollDown();
  // a live alert pins itself to the top of the pane so it can't be missed
  if (isAlert && scroll && !state.dismissed.has(m.id)) showAlertBanner(m);
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

$("#nav-projects").onclick = () => selectView({ type: "projects" });
$("#nav-taskboard").onclick = () => selectView({ type: "taskboard" });
$("#nav-kb").onclick = () => selectView({ type: "kb" });
$("#nav-usage").onclick = () => selectView({ type: "usage" });
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
