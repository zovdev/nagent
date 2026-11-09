/* NAgent — thin web client.
   Dumb renderer by design: the server is the state machine.
   HTTP for clicks, EventSource (SSE) for the stream. No localStorage. */

const $ = (id) => document.getElementById(id);

/* ------------------------------------------------------------------ icons
   SF-Symbols-like inline SVG set: stroke = currentColor, no emoji. */
const ICONS = {
  logo: '<circle cx="8" cy="8" r="5.4"/><circle cx="8" cy="8" r="1.5" fill="currentColor" stroke="none"/>',
  plus: '<path d="M8 3.2v9.6M3.2 8h9.6"/>',
  gear: '<circle cx="8" cy="8" r="2.3"/><path d="M8 1.7v1.8M8 12.5v1.8M1.7 8h1.8M12.5 8h1.8M3.6 3.6l1.3 1.3M11.1 11.1l1.3 1.3M12.4 3.6l-1.3 1.3M4.9 11.1l-1.3 1.3"/>',
  paperclip: '<path d="M12.4 7.2l-5.8 5.8a3 3 0 0 1-4.2-4.2l6.2-6.2a1.9 1.9 0 0 1 2.7 2.7l-6.2 6.1a.9.9 0 0 1-1.2-1.2l5.6-5.6"/>',
  plan: '<circle cx="4.5" cy="3.6" r="1.7"/><circle cx="4.5" cy="12.4" r="1.7"/><circle cx="11.5" cy="8" r="1.7"/><path d="M4.5 5.3v5.4M6.3 4.3c2.6.7 3 2.2 3.5 3.1M6.3 11.7c2.6-.7 3-2.2 3.5-3.1"/>',
  arrowUp: '<path d="M8 12.8V3.2M3.8 7.4L8 3.2l4.2 4.2"/>',
  stop: '<rect x="4" y="4" width="8" height="8" rx="1.6"/>',
  panel: '<rect x="1.8" y="2.6" width="12.4" height="10.8" rx="2"/><path d="M9.8 2.6v10.8"/>',
  check: '<path d="M3 8.5l3.2 3.2L13 4.8"/>',
  close: '<path d="M4 4l8 8M12 4l-8 8"/>',
  chevD: '<path d="M4.5 6.5L8 10l3.5-3.5"/>',
  chevR: '<path d="M6.5 4.5L10 8l-3.5 3.5"/>',
  refresh: '<path d="M13.2 8a5.2 5.2 0 1 1-1.6-3.8"/><path d="M13.4 1.8v2.6h-2.6"/>',
  download: '<path d="M8 2.5v8M4.8 7.3L8 10.5l3.2-3.2M3 12.5h10"/>',
  warn: '<path d="M8 2.2l6 11H2z"/><path d="M8 6.5v3M8 11.4v.2"/>',
  think: '<path d="M8 1.8l1.3 3.4 3.4 1.3-3.4 1.3L8 11.2 6.7 7.8 3.3 6.5l3.4-1.3z"/><path d="M12.5 10.2l.6 1.5 1.5.6-1.5.6-.6 1.5-.6-1.5-1.5-.6 1.5-.6z"/>',
  wrench: '<path d="M13.6 3.2a3.6 3.6 0 0 1-4.9 4.8L4 12.7a1.5 1.5 0 0 1-2.1-2.1l4.7-4.7a3.6 3.6 0 0 1 4.8-4.9l-1.9 1.9-.4 1.7 1.7-.4z"/>',
  result: '<path d="M3 3h7M3 7h10M3 11h6"/>',
  image: '<rect x="2" y="3" width="12" height="10" rx="1.8"/><circle cx="5.7" cy="6.7" r="1.1"/><path d="M2.5 11.5l3.4-3.2 2.5 2.3 2.2-2 3 2.8"/>',
  video: '<rect x="1.8" y="4" width="8.4" height="8" rx="1.6"/><path d="M10.5 6.5l3.5-2v7l-3.5-2z"/>',
  audio: '<path d="M5.5 8.8V4.6a1.3 1.3 0 1 1 2.6 0v4.2a2.6 2.6 0 1 1-2.6 0z"/><path d="M8.1 6h2.4a3.9 3.9 0 0 1 0 7.8h-5"/>',
  file: '<path d="M4 1.8h5L12.5 5v9.2H4z"/><path d="M9 1.8V5h3.5"/>',
  robot: '<rect x="3" y="6" width="10" height="7" rx="2"/><path d="M8 6V3.6M6.4 3.6h3.2"/><circle cx="5.8" cy="9.3" r=".9" fill="currentColor" stroke="none"/><circle cx="10.2" cy="9.3" r=".9" fill="currentColor" stroke="none"/>',
  key: '<circle cx="5.5" cy="10.5" r="2.8"/><path d="M7.5 8.5L13 3M11 3l1.8 1.8M9.4 4.6l1.8 1.8"/>',
  clock: '<circle cx="8" cy="8" r="5.8"/><path d="M8 4.6V8l2.4 1.6"/>',
  scissors: '<circle cx="4.2" cy="4.2" r="1.9"/><circle cx="4.2" cy="11.8" r="1.9"/><path d="M5.7 5.5L13 12M13 4L5.7 10.5"/>',
  branch: '<circle cx="4.5" cy="3.6" r="1.7"/><circle cx="4.5" cy="12.4" r="1.7"/><circle cx="11.5" cy="8" r="1.7"/><path d="M4.5 5.3v5.4M6.3 4.3c2.6.7 3 2.2 3.5 3.1M6.3 11.7c2.6-.7 3-2.2 3.5-3.1"/>',
  globe: '<circle cx="8" cy="8" r="5.8"/><path d="M2.2 8h11.6M8 2.2c1.9 2 2.8 3.7 2.8 5.8s-.9 3.8-2.8 5.8c-1.9-2-2.8-3.7-2.8-5.8s.9-3.8 2.8-5.8z"/>',
};
function ico(name, cls = "") {
  const body = ICONS[name] || "";
  return `<svg class="icon${cls ? " " + cls : ""}" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${body}</svg>`;
}

const state = {
  chats: [],
  chatId: null,
  lastSeq: 0,
  es: null,            // EventSource
  generating: false,
  liveBlock: null,     // currently streaming block { kind, el, textEl }
  liveDone: [],        // finished live blocks awaiting their persisted twin
  liveTurn: null,      // THIS turn's assistant grouping — streaming always lands here
  capsGuard: null,     // pending send interrupted by the unknown-model modal
  providers: [],
  models: [],
  settings: {},
  chain: [],           // fallback chain being edited
  attachments: [],     // uploaded, not yet sent (Phase 3 §10)
  planMode: false,     // next message goes as a plan-mode turn (§5)
  undoTimer: null,     // pending deletion — server learns only after expiry (§9)
  undoEventId: null,
  // Phase 4
  macros: [],
  termBlocks: [],      // terminal entries for THIS chat (rebuilt on open)
  browserWs: null,     // screencast WebSocket
  browserHello: null,  // {width, height, url, title}
  browserFrames: 0,
  paletteIndex: 0,
  paletteItems: [],
  modelKey: null,      // selected "provider::model" (single combined picker)
};

const UNDO_SECONDS = 5;

/* ------------------------------------------------------------------ Phase 5 state */

state.auth = { user: null, mode: "setup", needsOnboarding: false };
state.lang = "ru";

/* ------------------------------------------------------------------ api */

async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    credentials: "same-origin",
    ...opts,
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  let data = null;
  try { data = await res.json(); } catch { /* empty body */ }
  if (!res.ok) {
    if (res.status === 401 && !path.startsWith("/api/auth")) showGate();
    const err = new Error(data?.reason || data?.error || `HTTP ${res.status}`);
    err.status = res.status;
    err.code = data?.error;
    throw err;
  }
  return data;
}

/* ------------------------------------------------------------------ sidebar */

async function loadChats() {
  const { chats } = await api("/api/chats");
  state.chats = chats;
  renderChatList();
}

function renderChatList() {
  const list = $("chat-list");
  list.innerHTML = "";
  for (const c of state.chats) {
    const el = div("chat-item" + (c.id === state.chatId ? " active" : ""));
    el.tabIndex = 0;
    el.textContent = c.title;
    if (c.shared && !c.mine) {
      const badge = document.createElement("span");
      badge.className = "chat-shared-badge";
      badge.textContent = t("sharedTag") + (c.shared_by ? ` · ${c.shared_by}` : "");
      el.appendChild(badge);
    } else if (c.shared) {
      const badge = document.createElement("span");
      badge.className = "chat-shared-badge";
      badge.textContent = t("sharedMine");
      el.appendChild(badge);
    }
    el.onclick = () => openChat(c.id);
    el.onkeydown = (e) => { if (e.key === " " || e.key === "Enter") { e.preventDefault(); openChat(c.id); } };
    list.appendChild(el);
  }
}

async function newChat() {
  const { chat } = await api("/api/chats", { method: "POST", body: { title: "Новый чат", model_key: currentModelKey() } });
  await loadChats();
  openChat(chat.id);
}

/* ------------------------------------------------------------------ open chat + history */

async function openChat(chatId) {
  if (state.es) state.es.close();
  state.chatId = chatId;
  state.liveBlock = null;
  state.liveDone = [];
  state.liveTurn = null;
  $("messages").innerHTML = "";
  $("empty-state")?.remove();
  renderChatList();

  const [{ chat }, hist] = await Promise.all([
    api(`/api/chats/${chatId}`),
    api(`/api/chats/${chatId}/history`),
  ]);
  $("chat-title").textContent = chat.title;
  // a chat without a model adopts the currently picked one (smart default)
  if (!chat.model_key && state.modelKey && !state.generating) {
    api(`/api/chats/${chatId}`, { method: "PATCH", body: { model_key: state.modelKey } })
      .then(() => refreshContext()).catch(() => {});
  }
  $("system-prompt").value = chat.system_prompt || "";
  $("send-think").checked = !!chat.send_think_blocks;
  $("chat-compact-threshold").value = chat.compact_threshold || "";
  syncGenParams(chat.params || {});
  syncModelSelector(chat.model_key);

  state.lastSeq = hist.last_seq;
  termReset();
  for (const ev of hist.events) renderEvent(ev, { replay: true });
  // a fresh chat with no messages gets the same welcome as "no chat selected"
  if (!hist.events.length && !document.querySelector("#messages .msg")) {
    const welcome = document.createElement("div");
    welcome.className = "empty-state";
    welcome.id = "empty-state";
    welcome.innerHTML = `<div class="empty-logo">${ico("logo")}</div>` +
      `<div class="empty-title">${escapeHtml(chat.title || t("newChat"))}</div>` +
      `<div class="empty-sub" data-i18n="emptySub">${escapeHtml(t("emptySub"))}</div>`;
    $("messages").appendChild(welcome);
  }
  refreshContinueButton();
  await refreshStatus();
  connectStream();
  refreshContext();
  refreshUsage();
  scrollDown();
}

function connectStream() {
  if (state.es) state.es.close();
  const es = new EventSource(`/api/chats/${state.chatId}/stream?since=${state.lastSeq}`);
  state.es = es;

  const kinds = ["user_message", "assistant_think", "assistant_text",
    "tool_call", "tool_result", "turn_meta", "interrupt", "error", "fallback",
    "edit", "delete", "mask", "permission_request", "permission_result", "checkpoint",
    "subagent_start", "subagent_progress", "subagent_end",
    "plan_submitted", "plan_approved", "plan_rejected",
    "compact", "summarize", "detector", "agent_notice",
    "bg_start", "bg_end", "notification"];
  for (const k of kinds) es.addEventListener(k, (e) => {
    const ev = JSON.parse(e.data);
    if (ev.seq) state.lastSeq = Math.max(state.lastSeq, ev.seq);
    finishLiveBlock();
    handleStreamEvent(ev);
    scrollDown();
  });

  es.addEventListener("live", (e) => {
    const f = JSON.parse(e.data);
    if (f.live === "bg_output") {
      termLiveOutput(f);        // background task live line (§13 terminal)
    } else if (f.live === "queue") {
      showQueueChip(f.position); // §2 team concurrency: queued turn position
    } else {
      appendLive(f);
    }
    scrollDown();
  });

  es.addEventListener("bye", () => {});
  es.onerror = () => { /* EventSource auto-reconnects; replay comes via ?since= */ };
}

function handleStreamEvent(ev) {
  switch (ev.type) {
    case "edit": applyEditEvent(ev); break;
    case "delete": applyDeleteEvent(ev); break;
    case "mask": applyMaskEvent(ev); break;
    default: renderEvent(ev);
  }
  if (["turn_meta", "interrupt", "error"].includes(ev.type)) {
    setGenerating(false);
    refreshContext();
    refreshUsage();
    if (ev.type === "turn_meta") { refreshFileTree(); refreshCheckpoints(); playSound("turn"); }
  }
  if (ev.type === "checkpoint") refreshCheckpoints();
  if (["plan_submitted", "plan_approved", "compact", "summarize"].includes(ev.type)) {
    refreshContext();
  }
}

/* ------------------------------------------------------------------ rendering */

function renderEvent(ev, opts = {}) {
  $("empty-state")?.remove();
  // §13 terminal feed: bash + background tool traffic streams into the tab
  if (ev.type === "tool_call" && ["bash", "background_run"].includes(ev.data?.name)) termOnToolCall(ev);
  if (ev.type === "tool_result" && ["bash", "background_run"].includes(ev.data?.name)) termOnToolResult(ev);

  switch (ev.type) {
    case "user_message": return addMessage("user", ev);
    case "assistant_text": flushLiveDone("text"); return addAssistantText(ev, opts);
    case "assistant_think": flushLiveDone("think"); return addThinkBlock(ev, opts);
    case "tool_call": return addToolCard(ev, opts);
    case "tool_result": return addToolResult(ev, opts);
    case "turn_meta": return addMeta(ev);
    case "fallback": return addFallbackNotice(ev);
    case "permission_request": return addPermissionCard(ev);
    case "permission_result": return resolvePermissionCard(ev);
    case "checkpoint": return addCheckpointLine(ev);
    case "interrupt": return addNotice("Генерация остановлена");
    case "error": return addNotice(ev.data.message || "ошибка", true);
    // Phase 3
    case "subagent_start": return addSubagentCard(ev);
    case "subagent_progress": return addSubagentProgress(ev);
    case "subagent_end": return finishSubagentCard(ev);
    case "plan_submitted": return addPlanCard(ev);
    case "plan_approved": return addPlanApprovedCard(ev);
    case "plan_rejected": return addNotice("⎇ План отклонён юзером");
    case "compact": return addCompactLine(ev);
    case "summarize": return addSummarizeLine(ev);
    case "detector": return addDetectorCard(ev);
    case "agent_notice": return addAgentNotice(ev);
    // Phase 4 (§13/§10)
    case "bg_start": return termOnBgStart(ev);
    case "bg_end": return termOnBgEnd(ev);
    case "notification": return onNotification(ev);
    default: return;
  }
}

/* ------------------------------------------------------------------ markdown
   A small dependency-free renderer for model output. Everything is
   HTML-escaped FIRST, then the markup is applied — raw HTML from the model
   never reaches the DOM. Supports: fenced code, inline code, bold/italic/
   strike, headings, ordered/unordered lists, quotes, hr, GFM tables,
   links + bare URLs. */
const MD_INLINE = (s) => s
  .replace(/`([^`\n]+)`/g, '<code class="md-code-inline">$1</code>')
  .replace(/\*\*([^*\n]+)\*\*/g, "<b>$1</b>")
  .replace(/(^|[\s(])\*([^*\n]+)\*/g, "$1<i>$2</i>")
  .replace(/(^|[\s(])_([^_\n]+)_/g, "$1<i>$2</i>")
  .replace(/~~([^~\n]+)~~/g, "<s>$1</s>")
  .replace(/\[([^\]\n]+)\]\((https?:\/\/[^)\s]+)\)/g,
    '<a class="md-a" href="$2" target="_blank" rel="noopener noreferrer">$1</a>')
  .replace(/(^|[\s(])(https?:\/\/[^\s<)]+)/g,
    '$1<a class="md-a" href="$2" target="_blank" rel="noopener noreferrer">$2</a>');

function renderMarkdown(text) {
  const esc = escapeHtml(String(text ?? ""));
  const lines = esc.split("\n");
  const out = [];
  let listMode = null;
  let para = [];
  const closeList = () => { if (listMode) { out.push(listMode === "ul" ? "</ul>" : "</ol>"); listMode = null; } };
  const flushPara = () => { if (para.length) { out.push(`<p class="md-p">${MD_INLINE(para.join("<br>"))}</p>`); para = []; } };
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    const fence = line.match(/^\s*```(\w*)\s*$/);
    if (fence) {
      flushPara(); closeList();
      const buf = [];
      i++;
      while (i < lines.length && !/^\s*```\s*$/.test(lines[i])) { buf.push(lines[i]); i++; }
      i++;  // closing fence (or EOF)
      out.push(`<pre class="md-code">${fence[1] ? `<span class="md-code-lang">${fence[1]}</span>` : ""}<code>${buf.join("\n")}</code></pre>`);
      continue;
    }
    if (/^\s*$/.test(line)) { flushPara(); closeList(); i++; continue; }
    const h = line.match(/^(#{1,6})\s+(.*)$/);
    if (h) {
      flushPara(); closeList();
      out.push(`<div class="md-h md-h${Math.min(h[1].length, 4)}">${MD_INLINE(h[2].replace(/\s*#+\s*$/, ""))}</div>`);
      i++; continue;
    }
    if (/^\s*(-{3,}|\*{3,}|_{3,})\s*$/.test(line)) { flushPara(); closeList(); out.push('<hr class="md-hr">'); i++; continue; }
    const q = line.match(/^\s*&gt;\s?(.*)$/);
    if (q) { flushPara(); closeList(); out.push(`<blockquote class="md-quote">${MD_INLINE(q[1])}</blockquote>`); i++; continue; }
    if (/^\s*\|.*\|\s*$/.test(line) && i + 1 < lines.length &&
        /^\s*\|[\s:|-]+\|\s*$/.test(lines[i + 1])) {
      flushPara(); closeList();
      const cells = (r) => r.trim().replace(/^\|/, "").replace(/\|$/, "").split("|").map((c) => c.trim());
      const head = cells(line);
      i += 2;
      const rows = [];
      while (i < lines.length && /^\s*\|.*\|\s*$/.test(lines[i])) { rows.push(cells(lines[i])); i++; }
      out.push('<div class="md-table-wrap"><table class="md-table"><thead><tr>'
        + head.map((c) => `<th>${MD_INLINE(c)}</th>`).join("")
        + "</tr></thead><tbody>"
        + rows.map((r) => `<tr>${r.map((c) => `<td>${MD_INLINE(c)}</td>`).join("")}</tr>`).join("")
        + "</tbody></table></div>");
      continue;
    }
    const ul = line.match(/^\s*[-*+]\s+(.*)$/);
    if (ul) { flushPara(); if (listMode !== "ul") { closeList(); out.push('<ul class="md-list">'); listMode = "ul"; } out.push(`<li>${MD_INLINE(ul[1])}</li>`); i++; continue; }
    const ol = line.match(/^\s*\d+[.)]\s+(.*)$/);
    if (ol) { flushPara(); if (listMode !== "ol") { closeList(); out.push('<ol class="md-list">'); listMode = "ol"; } out.push(`<li>${MD_INLINE(ol[1])}</li>`); i++; continue; }
    para.push(line);
    i++;
  }
  flushPara(); closeList();
  return out.join("");
}

function addMessage(role, ev) {
  resetLiveTurn();          // a user message starts a NEW assistant turn
  const el = div("msg " + role + (ev.status === "deleted" ? " deleted" : "")
    + (ev.status === "forgotten" ? " forgotten" : ""));
  el.dataset.eid = ev.id;
  el.innerHTML = `
    <div class="bubble-col">
      <div class="msg-role"></div>
      <div class="bubble"></div>
      <div class="attach-chips msg-atts"></div>
    </div>`;
  el.querySelector(".bubble").textContent = ev.data.text;
  renderAttachmentChips(el.querySelector(".msg-atts"), ev.data.attachments);
  if (role === "user") {
    addMessageActions(el, ev, { editable: true, versions: true, deletable: true });
    if (ev.edited) markEditedBadge(el);
  }
  $("messages").appendChild(el);
}

function renderAttachmentChips(container, atts) {
  if (!container) return;
  container.innerHTML = "";
  for (const att of atts || []) {
    const chip = div("attach-chip");
    const kind = { image: "image", video: "video", audio: "audio", file: "file" }[att.kind] || "file";
    chip.innerHTML = `<span>${ico(kind)}</span><span class="att-name"></span><span class="att-size">${fmtK(att.size)}B</span>`;
    chip.querySelector(".att-name").textContent = att.name || "";
    chip.title = `${att.path || ""} · ${att.mime || ""}`;
    container.appendChild(chip);
  }
  if (!atts || !atts.length) container.classList.add("hidden");
  else container.classList.remove("hidden");
}

function addAssistantText(ev, opts) {
  finishLiveBlock();
  const el = div("msg assistant");
  el.dataset.eid = ev.id;
  el.innerHTML = `
    <div class="bubble-col">
      <div class="msg-role"></div>
      <div class="bubble"></div>
    </div>`;
  const bubble = el.querySelector(".bubble");
  bubble.innerHTML = renderMarkdown(ev.data.text);
  if (ev.data.interrupted) bubble.appendChild(span("meta-line", " прервано"));
  addMessageActions(el, ev, { editable: true, versions: true, deletable: true });
  if (ev.edited) markEditedBadge(el);
  if (ev.status === "masked") el.classList.add("masked");
  if (ev.status === "forgotten") el.classList.add("forgotten");
  if (ev.status === "deleted") el.classList.add("deleted");
  $("messages").appendChild(el);
  refreshContinueButton();
}

function addThinkBlock(ev, opts) {
  const block = div("think-block");
  block.dataset.eid = ev.id;
  const head = div("think-head");
  head.tabIndex = 0;
  head.title = "клик — развернуть/свернуть";
  head.innerHTML = `<span class="think-ico">${ico("think")}</span><span class="think-label">Мышление</span>`;
  const body = div("think-body");
  body.textContent = ev.data.text;
  if (ev.data.interrupted) body.textContent += " — прервано";
  block.append(head, body);
  head.onclick = () => block.classList.toggle("open");
  head.onkeydown = (e) => { if (e.key === " " || e.key === "Enter") { e.preventDefault(); block.classList.toggle("open"); } };
  if (opts.replay && ev.data.interrupted) block.classList.add("open");
  if (ev.status === "masked") block.classList.add("masked");
  attachToAssistant(block);
}

function addToolCard(ev, opts = {}) {
  const card = div("tool-card" + (ev.status === "masked" ? " masked" : "")
    + (ev.status === "forgotten" ? " forgotten" : ""));
  card.dataset.eid = ev.id;
  const reason = ev.masked_reason === "plan-mode" ? "скрыто: план-режим (в контексте только одобренный план)"
    : "затёрто из контекста модели (timeout-retry)";
  const head = document.createElement("button");
  head.type = "button";
  head.className = "tool-head";
  head.title = "клик — свернуть/развернуть";
  head.innerHTML = `<span class="tool-ico">${ico("wrench")}</span><span class="tool-name"></span>`
    + (ev.status === "masked" ? `<span class="badge masked" title="${reason}">⌫ скрыто</span>` : "")
    + (ev.status === "forgotten" ? `<span class="badge forgotten" title="сжато компакцией — в UI осталось">⌕ забыто</span>` : "")
    + `<span class="tool-chev">▾</span>`;
  head.querySelector(".tool-name").textContent = ev.data.name;
  head.onclick = () => card.classList.toggle("collapsed");
  const pre = document.createElement("pre");
  pre.textContent = JSON.stringify(ev.data.args, null, 2);
  card.append(head, pre);
  // replayed history / huge payload: start collapsed, one click to open
  if (opts.replay || pre.textContent.length > 700) card.classList.add("collapsed");
  attachToAssistant(card);
}

function addToolResult(ev, opts = {}) {
  const r = ev.data.result || {};
  // §13: the model asked for a browser but none is installed — offer engines
  if (r.browser_missing && !opts.replay) showBrowserMissingModal();
  const isTimeout = !!r.timeout;
  const card = div("tool-card" + (ev.status === "masked" ? " masked" : "")
    + (ev.status === "forgotten" ? " forgotten" : "")
    + (isTimeout ? " timeout" : ""));
  card.dataset.eid = ev.id;
  const reason = ev.masked_reason === "plan-mode" ? "скрыто: план-режим" : "затёрто из контекста модели";
  const badge = ev.status === "masked"
    ? `<span class="badge masked" title="${reason}">⌫ скрыто</span>` : "";
  const forgot = ev.status === "forgotten"
    ? `<span class="badge forgotten" title="сжато компакцией — в UI осталось">⌕ забыто</span>` : "";
  const timeoutNote = isTimeout
    ? `<div class="timeout-note">⏱ ${escapeHtml(r.message || "таймаут")}</div>` : "";
  const sumNote = r.summarized
    ? `<div class="timeout-note">вывод суммаризирован (${fmtK(r.original_chars)} симв. → ${fmtK((r.summary || "").length)}); полный — в кеше сервера</div>` : "";
  const head = document.createElement("button");
  head.type = "button";
  head.className = "tool-head";
  head.title = "клик — свернуть/развернуть";
  head.innerHTML = `<span class="tool-ico">${ico("result")}</span><span class="tool-name"></span>${badge}${forgot}<span class="tool-chev">▾</span>`;
  head.querySelector(".tool-name").textContent = `результат · ${ev.data.name || ""}`;
  head.onclick = () => card.classList.toggle("collapsed");
  const pre = document.createElement("pre");
  const show = r.exit_code != null
    ? `exit ${r.exit_code}` + (r.stdout ? `\n${r.stdout}` : "") + (r.stderr ? `\n${r.stderr}` : "")
    : r.summary || r.preview || r;
  pre.textContent = typeof show === "string" ? show : JSON.stringify(r, null, 2);
  const foot = div("tool-foot");
  foot.innerHTML = sumNote + timeoutNote;
  card.append(head, pre, foot);
  // big dumps fold away; timeout/summarize notes stay visible via .tool-foot
  if (!isTimeout && !r.summarized && (opts.replay || pre.textContent.length > 500)) {
    card.classList.add("collapsed");
  }
  attachToAssistant(card);
}

function addMeta(ev) {
  const line = div("meta-line");
  const u = ev.data.usage || {};
  const who = ev.data.provider ? `${ev.data.provider} · ${ev.data.model || ""}` : (ev.data.model || "");
  let text = `${who} · итераций: ${ev.data.iterations || 1}`;
  // usage as the API returned it — absent means the provider didn't send it
  if (u.prompt_tokens != null) {
    text += ` · ${u.prompt_tokens}→${u.completion_tokens ?? "?"} ток.`;
  }
  const cached = u.prompt_tokens_details?.cached_tokens ?? u.prompt_cache_hit_tokens;
  if (cached) text += ` (кеш ${fmtK(cached)})`;
  line.textContent = text;
  attachToAssistant(line);
}

function addFallbackNotice(ev) {
  const d = ev.data || {};
  const from = shortModel(d.from);
  const to = shortModel(d.to);
  addNotice(`⇄ Резервная модель: ${from} → ${to} — ${d.reason || "сбой"}`);
}

/* permission ask-cards (§7): Разрешить один раз / Всегда / Отклонить */
function addPermissionCard(ev) {
  const d = ev.data || {};
  const card = div("perm-card");
  card.id = `perm-${d.approval_id}`;
  card.innerHTML = `
    <div class="perm-head">Разрешить действие? <code>${escapeHtml(d.tool || "")}</code></div>
    <div class="perm-summary"></div>
    <details class="perm-args"><summary>аргументы</summary><pre></pre></details>
    <div class="perm-actions">
      <button data-decision="once" class="btn-primary">Разрешить один раз</button>
      <button data-decision="always" class="btn-secondary">Всегда для этого паттерна</button>
      <button data-decision="deny" class="btn-danger">Отклонить</button>
    </div>`;
  card.querySelector(".perm-summary").textContent = d.summary || "";
  card.querySelector(".perm-args pre").textContent =
    JSON.stringify(d.args ?? {}, null, 2);
  card.querySelectorAll("[data-decision]").forEach((b) => {
    b.onclick = async () => {
      b.disabled = true;
      try {
        await api(`/api/approvals/${d.approval_id}/resolve`, {
          method: "POST", body: { decision: b.dataset.decision } });
      } catch (e) { setError(e.message); b.disabled = false; }
    };
  });
  attachToAssistant(card);
}

function resolvePermissionCard(ev) {
  const d = ev.data || {};
  const card = document.getElementById(`perm-${d.approval_id}`);
  if (!card) return;
  const verdict = { once: "✓ разрешено", always: "✓ всегда разрешено",
                    deny: "отклонено", expired: "истекло" }[d.decision] || d.decision;
  card.classList.add(d.decision === "deny" ? "denied" : "approved");
  const actions = card.querySelector(".perm-actions");
  if (actions) actions.innerHTML = `<span class="perm-verdict">${verdict}</span>`;
}

function addCheckpointLine(ev) {
  const d = ev.data || {};
  const line = div("cp-line");
  line.tabIndex = 0;
  line.innerHTML = `<span>⎇ чекпоинт <code>${escapeHtml(d.commit || "")}</code></span>`
    + `<span class="cp-files">${(d.files || []).length} файл(ов)</span>`;
  line.title = d.message || "";
  line.onclick = () => { openPanel("git"); refreshCheckpoints(); };
  line.onkeydown = (e) => { if (e.key === " " || e.key === "Enter") { e.preventDefault(); openPanel("git"); refreshCheckpoints(); } };
  attachToAssistant(line);
}

/* ------------------------------------------------------------------ Phase 3 rendering */

function addSubagentCard(ev) {
  const d = ev.data || {};
  const card = div("subagent-card");
  card.id = `sub-${d.task_id}`;
  card.innerHTML = `
    <div class="sub-head"><span class="sub-ico">${ico("robot")}</span>
      <span>Субагент <code>${escapeHtml(d.task_id || "")}</code> — нулевой контекст</span>
      <span class="sub-spin">…</span></div>
    <div class="sub-prompt"></div>
    <div class="sub-tools"></div>
    <div class="sub-log"></div>`;
  card.querySelector(".sub-prompt").textContent = d.prompt || "";
  card.querySelector(".sub-tools").textContent =
    `тулзы: ${(d.tools || []).join(", ") || "read-only"}`;
  attachToAssistant(card);
}

function addSubagentProgress(ev) {
  const d = ev.data || {};
  const log = document.querySelector(`#sub-${d.task_id} .sub-log`);
  if (!log) return;
  const line = div("sub-log-line");
  line.textContent = `итерация ${d.iteration}: ${(d.tools || []).join(", ")}`;
  log.appendChild(line);
}

function finishSubagentCard(ev) {
  const d = ev.data || {};
  const card = document.getElementById(`sub-${d.task_id}`);
  if (!card) return;
  card.querySelector(".sub-spin")?.remove();
  card.classList.add(d.ok ? "done" : "failed");
  const res = div("sub-result");
  res.textContent = d.result || "";
  card.appendChild(res);
}

function addPlanCard(ev) {
  const d = ev.data || {};
  const card = div("plan-card");
  card.id = `plan-${ev.id}`;
  card.innerHTML = `
    <div class="plan-head">⎇ План на одобрении <span class="badge unknown">план-режим</span></div>
    <p class="hint">Исследование шло в read-only и не попадёт в контекст модели — только одобренный план. Текст можно поправить перед апрувом.</p>
    <textarea class="plan-edit" rows="10"></textarea>
    <div class="plan-actions">
      <button class="btn-primary" data-act="approve">✓ Одобрить и приступить</button>
      <button class="btn-secondary" data-act="reject">Отклонить</button>
    </div>`;
  card.querySelector(".plan-edit").value = d.plan_text || "";
  card.querySelectorAll("[data-act]").forEach((b) => {
    b.onclick = async () => {
      b.disabled = true;
      try {
        await api(`/api/chats/${state.chatId}/plan/${ev.id}`, {
          method: "POST",
          body: { action: b.dataset.act,
                  plan_text: card.querySelector(".plan-edit").value },
        });
        card.querySelector(".plan-actions").innerHTML =
          `<span class="perm-verdict">${b.dataset.act === "approve" ? "✓ план одобрен" : "✗ отклонён"}</span>`;
      } catch (e) { setError(e.message); b.disabled = false; }
    };
  });
  attachToAssistant(card);
}

function addPlanApprovedCard(ev) {
  const d = ev.data || {};
  const card = div("plan-card approved");
  card.id = `plan-card-${ev.id}`;
  card.dataset.eid = ev.id;
  const head = div("plan-head");
  head.innerHTML = `⎇ Одобренный план <span class="badge ok">в контексте модели</span>`;
  card.appendChild(head);
  const body = div("plan-body");
  body.textContent = d.plan_text || "";
  card.appendChild(body);
  renderChecklist(card, ev);
  attachToAssistant(card);
}

function renderChecklist(card, ev) {
  card.querySelector(".checklist")?.remove();
  const cl = div("checklist");
  for (const [i, item] of (ev.data.checklist || []).entries()) {
    const row = div("check-row" + (item.done ? " done" : ""));
    row.tabIndex = 0;
    row.innerHTML = `<span class="check-box">${item.done ? "✓" : ""}</span>`
      + `<span class="check-text"></span>`;
    row.querySelector(".check-text").textContent = item.text || "";
    const toggle = () => toggleChecklist(ev.id, i, !item.done);
    row.onclick = toggle;
    row.onkeydown = (e) => { if (e.key === " " || e.key === "Enter") { e.preventDefault(); toggle(); } };
    cl.appendChild(row);
  }
  card.appendChild(cl);
}

async function toggleChecklist(eventId, index, done) {
  try {
    await api(`/api/chats/${state.chatId}/plan/${eventId}/checklist`, {
      method: "PATCH", body: { index, done } });
  } catch (e) { setError(e.message); }
}

function addCompactLine(ev) {
  const d = ev.data || {};
  const line = div("compact-line");
  const n = (d.forgotten_event_ids || []).length;
  line.innerHTML = `<div class="compact-head">⇲ Компакция: ${n} событий → выжимка`
    + `<span class="badge forgotten" title="события скрыты из контекста модели, в UI остались">⌕ забыто: ${n}</span></div>`;
  const det = document.createElement("details");
  det.className = "compact-body";
  det.innerHTML = `<summary>выжимка (модель видит её вместо середины)</summary><div class="compact-text"></div>`;
  det.querySelector(".compact-text").textContent = d.summary || "";
  line.appendChild(det);
  attachToAssistant(line);
}

function addSummarizeLine(ev) {
  const d = ev.data || {};
  const line = div("summarize-line");
  const det = document.createElement("details");
  det.innerHTML = `<summary>Суммаризация вывода ${escapeHtml(d.tool || "")} — шаг ${d.step || "?"} (${escapeHtml(d.note || "")})</summary><pre></pre>`;
  det.querySelector("pre").textContent = d.text || "";
  line.appendChild(det);
  attachToAssistant(line);
}

function addDetectorCard(ev) {
  const d = ev.data || {};
  if (d.kind === "repeat") {
    const card = div("detector-card repeat");
    card.innerHTML = `<div class="det-head">Похоже на повтор (совпадение ${(d.similarity * 100).toFixed(0)}%) — генерация остановлена</div>
      <div class="det-note">${escapeHtml(d.note || "")}</div>
      <div class="det-actions"><button class="btn-primary">Продолжить</button></div>`;
    card.querySelector("button").onclick = async () => {
      try { await api(`/api/chats/${state.chatId}/continue`, { method: "POST", body: { mode: "prefill" } }); }
      catch (e) { setError(e.message); }
    };
    attachToAssistant(card);
    return;
  }
  const line = div("detector-card warn");
  line.innerHTML = escapeHtml(d.note || "детектор лупа");
  attachToAssistant(line);
}

function addAgentNotice(ev) {
  const el = div("msg assistant");
  el.innerHTML = `<div class="bubble-col"><div class="msg-role"></div><div class="bubble sys-note"></div></div>`;
  el.querySelector(".bubble").textContent = ev.data.text || "";
  $("messages").appendChild(el);
}

/* ------------------------------------------------------------------ §9: message actions (edit / delete / RAW / versions / continue) */

function applyEditEvent(ev) {
  const el = document.querySelector(`[data-eid="${ev.target}"]`);
  if (!el) return;
  const bubble = el.querySelector(".bubble") || el.querySelector(".think-body")
    || el.querySelector("pre");
  const d = ev.data || {};
  if (bubble && d.text != null) bubble.innerHTML = renderMarkdown(d.text);
  if (bubble && d.result != null) bubble.textContent = JSON.stringify(d.result, null, 2);
  el.classList.add("edited");
  markEditedBadge(el);
}

function applyDeleteEvent(ev) {
  const el = document.querySelector(`[data-eid="${ev.target}"]`);
  if (!el) return;
  el.classList.add("deleted");
  if (state.undoEventId === ev.target) hideUndoToast();
}

function applyMaskEvent(ev) {
  const targets = (ev.data || {}).targets || [];
  for (const tid of targets) {
    const el = document.querySelector(`[data-eid="${tid}"]`);
    if (el) el.classList.add("masked");
  }
}

function markEditedBadge(el) {
  if (el.querySelector(".badge.edited")) return;
  const badge = span("badge edited", "правлено");
  const head = el.querySelector(".msg-role") || el.querySelector(".tool-name");
  if (head) head.appendChild(badge);
}

function addMessageActions(container, ev, opts = {}) {
  const bar = div("msg-actions");
  const mk = (txt, title, fn) => {
    const b = document.createElement("button");
    b.className = "msg-action";
    // SVG markup must go through innerHTML — textContent would print the
    // raw "<svg …>" string in the button
    if (txt.startsWith("<svg")) b.innerHTML = txt;
    else b.textContent = txt;
    b.title = title;
    b.setAttribute("aria-label", title);
    b.onclick = fn;
    return b;
  };
  if (opts.editable) {
    bar.appendChild(mk(ico("result"), "Редактировать (правка уйдёт в контекст модели, оригинал — в историю правок)",
      () => startInlineEdit(container, ev)));
  }
  if (opts.versions) {
    bar.appendChild(mk(ico("clock"), "История правок", () => showVersions(ev.id)));
  }
  bar.appendChild(mk("⌗", "RAW — весь поток как есть", () => showRaw()));
  if (opts.deletable) {
    bar.appendChild(mk("⌫", "Удалить (5 сек на отмену — сервер узнает только после таймера)",
      () => requestDelete(ev)));
  }
  container.appendChild(bar);
}

function startInlineEdit(container, ev) {
  const bubble = container.querySelector(".bubble") || container.querySelector(".think-body");
  if (!bubble || container.querySelector(".edit-area")) return;
  const ta = document.createElement("textarea");
  ta.className = "edit-area";
  ta.value = ev.data.text || "";
  bubble.classList.add("hidden");
  bubble.after(ta);
  const actions = div("edit-actions");
  const save = document.createElement("button");
  save.className = "btn-primary";
  save.textContent = "Сохранить";
  const cancel = document.createElement("button");
  cancel.className = "btn-secondary";
  cancel.textContent = "Отмена";
  actions.append(save, cancel);
  ta.after(actions);
  const close = () => { ta.remove(); actions.remove(); bubble.classList.remove("hidden"); };
  cancel.onclick = close;
  save.onclick = async () => {
    try {
      await api(`/api/chats/${state.chatId}/events/${ev.id}`, {
        method: "PATCH", body: { data: { ...ev.data, text: ta.value } } });
      close();
    } catch (e) { setError(e.message); }
  };
  ta.focus();
}

async function showVersions(eventId) {
  try {
    const v = await api(`/api/chats/${state.chatId}/events/${eventId}/versions`);
    const list = $("versions-list");
    list.innerHTML = "";
    for (const [i, ver] of (v.versions || []).entries()) {
      const row = div("version-row" + (i === v.versions.length - 1 ? " current" : ""));
      const when = ver.ts ? new Date(ver.ts * 1000).toLocaleTimeString() : "—";
      row.innerHTML = `<div class="ver-head">${ver.original ? "оригинал" : "правка " + i} · ${when}${i === v.versions.length - 1 ? " · текущая" : ""}</div><pre></pre>`;
      row.querySelector("pre").textContent =
        typeof ver.data?.text === "string" ? ver.data.text : JSON.stringify(ver.data, null, 2);
      list.appendChild(row);
    }
    $("versions-modal").classList.remove("hidden");
  } catch (e) { setError(e.message); }
}

async function showRaw() {
  try {
    const { events } = await api(`/api/chats/${state.chatId}/raw`);
    $("raw-content").textContent = JSON.stringify(events, null, 2);
    $("raw-modal").classList.remove("hidden");
  } catch (e) { setError(e.message); }
}

function requestDelete(ev) {
  showUndoToast(ev);
}

function showUndoToast(ev) {
  state.undoEventId = ev.id;
  let left = UNDO_SECONDS;
  const toast = $("undo-toast");
  const text = $("undo-toast-text");
  toast.classList.remove("hidden");
  text.textContent = `удаление через ${left}…`;
  clearInterval(state.undoTimer);
  state.undoTimer = setInterval(() => {
    left -= 1;
    if (left <= 0) { fireDelete(ev); return; }
    text.textContent = `удаление через ${left}…`;
  }, 1000);
}

function hideUndoToast() {
  clearInterval(state.undoTimer);
  state.undoTimer = null;
  state.undoEventId = null;
  $("undo-toast").classList.add("hidden");
}

async function fireDelete(ev) {
  const eventId = state.undoEventId || ev.id;
  hideUndoToast();
  try {
    const res = await api(`/api/chats/${state.chatId}/events/${eventId}`, { method: "DELETE" });
    if (res.checkpoint) offerCheckpointRevert(res.checkpoint);
  } catch (e) { setError(e.message); }
}

function offerCheckpointRevert(cp) {
  setError("");
  const toast = $("undo-toast");
  toast.classList.remove("hidden");
  $("undo-toast-text").innerHTML =
    `ход менял файлы (${escapeHtml((cp.files || []).join(", "))}) — ` +
    `<a href="#" id="undo-revert-link">откатить их через merge-request?</a>`;
  clearInterval(state.undoTimer);
  $("undo-toast-text").querySelector("#undo-revert-link").onclick = (e) => {
    e.preventDefault();
    hideUndoToast();
    openRevertByCheckpoint(cp.checkpoint_id);
  };
  setTimeout(hideUndoToast, 15000);
}

async function openRevertByCheckpoint(checkpointId) {
  try {
    const mr = await api(`/api/checkpoints/${checkpointId}/revert`, { method: "POST" });
    mrCheckpoint = { id: checkpointId, commit_sha: mr.commit || "", message: mr.message || "" };
    $("mr-modal").classList.remove("hidden");
    `Чекпоинт хода — реверс-патч ниже (удаление сообщения на контекст не влияет: файлы — отдельная ось).`
    const conflicts = mr.conflicts || [];
    $("mr-conflicts").classList.toggle("hidden", !conflicts.length);
    $("mr-conflicts").textContent = conflicts.length
      ? `Конфликт с поздними правками: ${conflicts.join(", ")}` : "";
    $("btn-mr-force").classList.toggle("hidden", !conflicts.length);
    $("btn-mr-apply").classList.toggle("hidden", !!conflicts.length);
    $("mr-diff").textContent = mr.revert_patch || "[пусто]";
  } catch (e) { setError(e.message); }
}

async function continueGeneration() {
  if (!state.chatId || state.generating) return;
  try {
    await api(`/api/chats/${state.chatId}/continue`, { method: "POST", body: { mode: "prefill" } });
    setGenerating(true);
  } catch (e) { setError(e.message); }
}

function refreshContinueButton() {
  document.querySelectorAll(".continue-btn").forEach((b) => b.remove());
  if (!state.chatId || state.generating) return;
  const msgs = $("messages").querySelectorAll(".msg.assistant.grouping");
  const last = msgs[msgs.length - 1];
  if (!last) return;
  const btn = document.createElement("button");
  btn.className = "continue-btn";
  btn.title = "Продолжить генерацию (assistant-prefill)";
  btn.textContent = "↻ продолжить";
  btn.onclick = continueGeneration;
  last.querySelector(".bubble-col").appendChild(btn);
}

function shortModel(key) {
  if (!key) return "—";
  const [pid, mid] = key.split("::");
  const p = state.providers.find((x) => x.id === pid);
  return p ? `${p.name}/${mid}` : mid || key;
}

function addNotice(text, isErr) {
  const el = div("msg assistant");
  el.innerHTML = `<div class="bubble-col"><div class="bubble ${isErr ? "error-bubble" : ""}"></div></div>`;
  el.querySelector(".bubble").textContent = text;
  $("messages").appendChild(el);
}

/* live streaming frames (transient: {live: 'think'|'text', delta}) */
function liveTurn() {
  /* The CURRENT generation gets its own assistant grouping, created after
     the user message — never appended into the PREVIOUS turn (that was the
     "think block above my message" bug). */
  if (state.liveTurn && state.liveTurn.isConnected) return state.liveTurn;
  const el = div("msg assistant grouping");
  el.innerHTML = `<div class="bubble-col"></div>`;
  $("messages").appendChild(el);
  state.liveTurn = el;
  return el;
}

function resetLiveTurn() {
  /* a new user message / new generation starts a fresh turn container */
  state.liveTurn = null;
}

function appendLive(frame) {
  const isThink = frame.live === "think";
  if (!state.liveBlock || state.liveBlock.kind !== (isThink ? "think" : "text")) {
    finishLiveBlock();
    if (isThink) {
      const block = div("think-block open streaming");
      block.innerHTML = `<div class="think-head" title="клик — развернуть/свернуть"><span class="think-ico">${ico("think")}</span><span class="think-label">Мышление</span></div><div class="think-body"></div>`;
      liveTurn().querySelector(".bubble-col").appendChild(block);
      state.liveBlock = { kind: "think", el: block, textEl: block.querySelector(".think-body") };
    } else {
      const el = div("msg assistant");
      el.innerHTML = `<div class="bubble-col"><div class="msg-role"></div><div class="bubble"><span class="text"></span><span class="cursor"></span></div></div>`;
      $("messages").appendChild(el);
      state.liveBlock = { kind: "text", el, textEl: el.querySelector(".text"), buf: "" };
    }
  }
  if (state.liveBlock.kind === "text") {
    // markdown is re-rendered as the text grows, throttled to ~12 fps
    state.liveBlock.buf += frame.delta;
    const b = state.liveBlock;
    const now = performance.now();
    if (!b.lastRender || now - b.lastRender > 80) {
      b.textEl.innerHTML = renderMarkdown(b.buf);
      b.lastRender = now;
    }
  } else {
    state.liveBlock.textEl.textContent += frame.delta;
  }
}

function finishLiveBlock() {
  if (!state.liveBlock) return;
  const { kind, el } = state.liveBlock;
  if (kind === "think") {
    el.classList.remove("streaming", "open");
  } else {
    el.querySelector(".cursor")?.remove();
    if (state.liveBlock.buf != null) {
      state.liveBlock.textEl.innerHTML = renderMarkdown(state.liveBlock.buf);
    }
  }
  state.liveDone.push({ kind, el });
  state.liveBlock = null;
}

function flushLiveDone(kind) {
  state.liveDone = state.liveDone.filter(({ kind: k, el }) => {
    if (k === kind) { el.remove(); return false; }
    return true;
  });
}

function attachToAssistant(node) {
  /* During a live generation everything belongs to the fresh turn
     container; on replay (history rebuild) consecutive assistant events
     group into the last assistant message, as before. */
  if (state.generating || (state.liveTurn && state.liveTurn.isConnected)) {
    liveTurn().querySelector(".bubble-col").appendChild(node);
    return;
  }
  const msgs = $("messages").querySelectorAll(".msg.assistant");
  const last = msgs[msgs.length - 1];
  if (last && last.classList.contains("grouping")) {
    last.querySelector(".bubble-col").appendChild(node);
  } else {
    const el = div("msg assistant grouping");
    el.innerHTML = `<div class="bubble-col"></div>`;
    el.querySelector(".bubble-col").appendChild(node);
    $("messages").appendChild(el);
  }
}

/* ------------------------------------------------------------------ context meter + usage */

async function refreshContext() {
  if (!state.chatId) return;
  try {
    const c = await api(`/api/chats/${state.chatId}/context`);
    const label = $("ctx-label");
    const fill = $("ctx-fill");
    // REAL usage from the API only — no local estimation, ever
    if (c.prompt_tokens == null) {
      label.textContent = "контекст — после первого хода";
      fill.style.width = "0%";
      fill.className = "ctx-fill";
      return;
    }
    if (!c.context_length) {
      label.textContent = `${fmtK(c.prompt_tokens)} ток. (окно неизвестно)`;
      fill.style.width = "0%";
      fill.className = "ctx-fill";
      return;
    }
    const pct = c.percent ?? 0;
    label.textContent = `${pct}% · ${fmtK(c.prompt_tokens)}/${fmtK(c.context_length)}`;
    fill.style.width = `${Math.min(pct, 100)}%`;
    fill.className = "ctx-fill" + (pct >= 90 ? " danger" : pct >= 70 ? " warn" : "");
  } catch { /* chat deleted */ }
}

async function refreshUsage() {
  if (!state.chatId) return;
  try {
    const u = await api(`/api/chats/${state.chatId}/usage`);
    $("usage-line").textContent = u.turns_with_usage
      ? `${fmtK(u.total_tokens)} ток. за сессию`
      : "";
  } catch { /* chat deleted */ }
}

function fmtK(n) {
  if (n == null) return "—";
  if (n >= 1e6) return (n / 1e6).toFixed(1) + "M";
  if (n >= 1000) return (n / 1000).toFixed(1) + "k";
  return String(n);
}

/* ------------------------------------------------------------------ composer */

function modelNeedsCaps(m) {
  if (!m) return false;
  const caps = m.capabilities || {};
  if (caps.embedding) return false;          // declared by the API itself
  return !m.context_length && !Object.keys(caps).length;
}

async function send() {
  const input = $("input");
  const text = input.value.trim();
  const atts = state.attachments.map((a) => a.id);
  if ((!text && !atts.length) || state.generating) return;
  if (!state.chatId) {
    // the empty state promises «напишите сообщение, чтобы начать» — the
    // first send creates the chat instead of silently dropping the text
    try {
      const { chat } = await api("/api/chats", { method: "POST",
        body: { title: text.slice(0, 40) || "Новый чат",
                model_key: currentModelKey() } });
      await loadChats();
      await openChat(chat.id);
    } catch (e) { setError(e.message); return; }
  }
  const model = state.models.find((m) => m.id === state.modelKey);
  if (model && modelNeedsCaps(model)) {
    // nothing known about this model — ask BEFORE the first token
    state.capsGuard = { text, atts, plan: state.planMode };
    openCapsModal(model);
    return;
  }
  await doSend(text, atts, state.planMode);
}

async function doSend(text, atts, plan) {
  const input = $("input");
  input.value = "";
  autoGrow();
  const body = { text };
  if (atts && atts.length) body.attachments = atts;
  if (plan) body.plan = true;
  try {
    await api(`/api/chats/${state.chatId}/messages`, { method: "POST", body });
    state.attachments = [];
    renderAttachChips();
    setPlanMode(false);
    setError("");
    setGenerating(true);
  } catch (e) {
    setError(e.code === "locked" ? "Генерация уже идёт — дождитесь или остановите" :
      e.code === "no_model" ? "Выберите провайдера и модель (справа снизу)" : e.message);
  }
}

/* ------------------------------------------------------------------ unknown-model modal */

let capsModel = null;

function openCapsModal(m) {
  capsModel = m;
  $("caps-title").textContent = "Возможности модели неизвестны";
  $("caps-model-line").textContent = `${m.model_id}`;
  const caps = m.capabilities || {};
  $("caps-toggles").querySelectorAll("[data-cap]").forEach((cb) => {
    cb.checked = !!caps[cb.dataset.cap];
  });
  $("caps-context").value = m.context_length || "";
  $("caps-probe-result").textContent = "";
  $("caps-modal").classList.remove("hidden");
}

function closeCapsModal() {
  $("caps-modal").classList.add("hidden");
  capsModel = null;
  state.capsGuard = null;
}

async function capsProbe() {
  if (!capsModel) return;
  const btn = $("btn-caps-probe");
  btn.disabled = true;
  $("caps-probe-result").textContent = "пробный запрос идёт…";
  try {
    const res = await api("/api/models/probe", {
      method: "POST", body: { model_key: capsModel.id } });
    if (res.context_length) {
      $("caps-context").value = res.context_length;
      $("caps-probe-result").textContent =
        `API назвал длину контекста: ${res.context_length.toLocaleString("ru-RU")} ток.`;
      capsModel = res.model || capsModel;
    } else {
      $("caps-probe-result").textContent =
        `API не раскрыл длину контекста. Ответ: ${res.reply || "—"}`;
    }
  } catch (e) {
    $("caps-probe-result").textContent = "Проб не удался: " + e.message;
  } finally {
    btn.disabled = false;
  }
}

async function capsSaveAndSend() {
  if (!capsModel) return;
  const caps = {};
  $("caps-toggles").querySelectorAll("[data-cap]").forEach((cb) => {
    if (cb.checked) caps[cb.dataset.cap] = true;
  });
  const override = {};
  if (Object.keys(caps).length) override.capabilities = caps;
  const ctx = parseInt($("caps-context").value, 10);
  if (ctx > 0) override.context_length = ctx;
  const guard = state.capsGuard;
  try {
    if (Object.keys(override).length) {
      await api("/api/models/override", {
        method: "PATCH", body: { model_key: capsModel.id, override } });
      const { models } = await api("/api/models");
      state.models = models;
      renderModelPicker();
      refreshContext();
    }
  } catch (e) { setError(e.message); }
  closeCapsModal();
  if (guard) await doSend(guard.text, guard.atts, guard.plan);
}

function capsSkip() {
  const guard = state.capsGuard;
  $("caps-modal").classList.add("hidden");
  capsModel = null;
  state.capsGuard = null;
  if (guard) doSend(guard.text, guard.atts, guard.plan);
}

/* attachments (§10) */

function pickFiles() {
  $("file-input").click();
}

async function onFilesPicked(e) {
  const files = [...(e.target.files || [])];
  e.target.value = "";
  if (!files.length || !state.chatId) return;
  const fd = new FormData();
  for (const f of files) fd.append("file", f, f.name);
  try {
    const res = await fetch(`/api/chats/${state.chatId}/attachments`, {
      method: "POST", body: fd });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data?.error || `HTTP ${res.status}`);
    state.attachments.push(...(data.attachments || []));
    renderAttachChips();
  } catch (err) { setError("Вложение: " + err.message); }
}

function renderAttachChips() {
  const box = $("attach-chips");
  box.innerHTML = "";
  if (!state.attachments.length) { box.classList.add("hidden"); return; }
  box.classList.remove("hidden");
  state.attachments.forEach((att, i) => {
    const chip = div("attach-chip");
    const kind = { image: "image", video: "video", audio: "audio", file: "file" }[att.kind] || "file";
    chip.innerHTML = `<span>${ico(kind)}</span><span class="att-name"></span><span class="att-size">${fmtK(att.size)}B</span><button class="chip-x" title="убрать">${ico("close")}</button>`;
    chip.querySelector(".att-name").textContent = att.name || "";
    chip.querySelector(".chip-x").onclick = () => {
      state.attachments.splice(i, 1);
      renderAttachChips();
    };
    box.appendChild(chip);
  });
}

/* plan mode toggle (§5) */

function setPlanMode(on) {
  state.planMode = on;
  $("btn-plan").classList.toggle("active", on);
  $("input").placeholder = on
    ? "План-режим: агент исследует read-only и предложит план на одобрение…"
    : "Сообщение… (Enter — отправить, Shift+Enter — перенос)";
}

async function stopGeneration() {
  if (!state.chatId) return;
  await api(`/api/chats/${state.chatId}/stop`, { method: "POST" });
}

async function refreshStatus() {
  if (!state.chatId) return;
  try {
    const st = await api(`/api/chats/${state.chatId}/status`);
    setGenerating(st.generating);
  } catch { /* server restart — stream reconnect will resync */ }
}

function setGenerating(on) {
  state.generating = on;
  $("btn-stop").classList.toggle("hidden", !on);
  $("btn-send").disabled = on;
  const s = $("chat-status");
  s.textContent = on ? "генерация" : "";
  s.classList.toggle("generating", on);
  if (!on) {
    finishLiveBlock();
    // drop the live turn container if nothing persisted into it
    const col = state.liveTurn?.querySelector(".bubble-col");
    if (state.liveTurn && state.liveTurn.isConnected && col && !col.children.length) {
      state.liveTurn.remove();
    }
    state.liveTurn = null;
    refreshContinueButton();
  }
}

function setError(msg) { $("error-line").textContent = msg || ""; }

function scrollDown() {
  const m = $("messages");
  m.scrollTop = m.scrollHeight;
}

/* ------------------------------------------------------------------ model selector
   ONE control: provider+model grouped in a single popover (§1 GUI-first). */

async function loadProvidersAndModels() {
  const [{ providers }, { models }] = await Promise.all([
    api("/api/providers"), api("/api/models"),
  ]);
  state.providers = providers;
  state.models = models;
  renderModelPicker();
  // smart default: exactly one provider with a catalog and nothing picked
  // yet -> select its first model (the user can always change it)
  if (!state.modelKey && providers.length === 1 && models.length) {
    const first = models.find((m) => m.provider_id === providers[0].id);
    if (first) selectModel(first.id, { silent: true });
  }
}

function providerHealth(p) {
  if (p.health_status === "online") return "online";
  if (p.health_status) return "offline";
  return "";
}

function renderModelPicker() {
  const pop = $("model-pop");
  if (!pop) return;
  const name = $("mc-name");
  const dot = $("mc-dot");
  const model = state.models.find((m) => m.id === state.modelKey);
  const prov = model ? state.providers.find((p) => p.id === model.provider_id) : null;
  name.textContent = model ? (model.title || model.model_id) : "модель не выбрана";
  dot.className = "mc-dot " + (prov ? providerHealth(prov) : "");
  pop.innerHTML = "";
  if (!state.providers.length) {
    pop.innerHTML = `<div class="mp-empty">Провайдеров нет — добавьте в настройках</div>`;
    return;
  }
  for (const p of state.providers) {
    const models = state.models.filter((m) => m.provider_id === p.id);
    const sect = div("mp-prov");
    sect.innerHTML = `<div class="mp-prow"><span class="mp-pdot ${providerHealth(p)}"></span>` +
      `<span class="mp-pname"></span></div><div class="mp-phost"></div>`;
    sect.querySelector(".mp-pname").textContent = p.name;
    if (p.host && p.host !== p.name) sect.querySelector(".mp-phost").textContent = p.host;
    if (!models.length) {
      const empty = div("mp-empty");
      empty.textContent = "моделей нет — обновите каталог в настройках";
      sect.appendChild(empty);
    }
    for (const m of models) {
      const row = document.createElement("button");
      row.type = "button";
      row.className = "mp-model" + (m.id === state.modelKey ? " selected" : "");
      row.setAttribute("role", "option");
      row.title = m.model_id;
      const caps = m.capabilities || {};
      const badges = [];
      if (caps.embedding) {
        badges.push(`<span class="mp-badge emb">embedding</span>`);
      } else if (!m.context_length && !Object.keys(caps).length) {
        badges.push(`<span class="mp-badge unknown" title="Возможности неизвестны — нажмите, чтобы указать">неизвестна</span>`);
      }
      // ONE name: title only when it differs from the id (no top+bottom duplicates)
      const sameName = !m.title || m.title === m.model_id;
      row.innerHTML = `<span class="mp-mcol"><span class="mp-mname"></span>` +
        (sameName ? `` : `<span class="mp-mid"></span>`) +
        `</span><span class="mp-badges">${badges.join("")}</span><span class="mp-check">✓</span>`;
      row.querySelector(".mp-mname").textContent = sameName ? m.model_id : m.title;
      if (!sameName) row.querySelector(".mp-mid").textContent = m.model_id;
      const unknownBadge = row.querySelector(".mp-badge.unknown");
      if (unknownBadge) unknownBadge.onclick = (e) => { e.stopPropagation(); openCapsModal(m); };
      row.onclick = () => selectModel(m.id);
      sect.appendChild(row);
    }
    pop.appendChild(sect);
  }
}

function openModelPop() {
  $("model-pop").classList.remove("hidden");
  $("model-chip").setAttribute("aria-expanded", "true");
}

function closeModelPop() {
  $("model-pop")?.classList.add("hidden");
  $("model-chip")?.setAttribute("aria-expanded", "false");
}

function selectModel(id, opts = {}) {
  state.modelKey = id;
  renderModelPicker();
  if (!opts.silent) return onModelChange();
}

function currentModelKey() {
  return state.modelKey;
}

function syncModelSelector(modelKey) {
  if (!modelKey) return;
  state.modelKey = modelKey;
  renderModelPicker();
}

async function onModelChange() {
  const key = currentModelKey();
  if (!key || !state.chatId) return;
  await api(`/api/chats/${state.chatId}`, { method: "PATCH", body: { model_key: key } });
  refreshContext();
}

/* ------------------------------------------------------------------ settings modal: tabs */

function openSettings() {
  $("settings-modal").classList.remove("hidden");
  renderProviderList();
  renderCatalog();
  renderMcpList();
  loadReliability();
  loadPermissions();
  loadContextSettings();
  loadWebSettings();
  loadSoundsSettings();
  renderMacroList();
  loadBrowserEngines();
}

function initPaneSplit() {
  const split = $("pane-split");
  if (!split) return;
  const pane = $("pane-files");
  const container = $("pview-files");
  let dragging = false;
  split.addEventListener("mousedown", (e) => {
    dragging = true;
    split.classList.add("dragging");
    document.body.classList.add("split-dragging");
    e.preventDefault();
  });
  document.addEventListener("mousemove", (e) => {
    if (!dragging) return;
    const rect = container.getBoundingClientRect();
    const pct = ((e.clientY - rect.top) / rect.height) * 100;
    const clamped = Math.min(Math.max(pct, 18), 82);
    pane.style.flex = `0 0 ${clamped}%`;
  });
  document.addEventListener("mouseup", () => {
    if (!dragging) return;
    dragging = false;
    split.classList.remove("dragging");
    document.body.classList.remove("split-dragging");
  });
  split.addEventListener("dblclick", () => { pane.style.flex = ""; });
}

/* Draggable vertical dividers: sidebar (left) and work panel (right).
   Pointer Events + setPointerCapture; double click restores default. */
function initVSplits() {
  const wire = (splitId, el, min, max, invert) => {
    const split = $(splitId);
    if (!split || !el) return;
    let dragging = false;
    split.addEventListener("pointerdown", (e) => {
      dragging = true;
      split.setPointerCapture(e.pointerId);
      split.classList.add("dragging");
      document.body.classList.add("vsplit-dragging");
      e.preventDefault();
    });
    split.addEventListener("pointermove", (e) => {
      if (!dragging) return;
      const rect = el.parentElement.getBoundingClientRect();
      const px = invert ? rect.right - e.clientX : e.clientX - rect.left;
      const maxPx = typeof max === "function" ? max() : max;
      el.style.flex = "0 0 auto";
      el.style.width = `${Math.min(Math.max(Math.round(px), min), maxPx)}px`;
      el.style.minWidth = `${min}px`;
    });
    const done = () => {
      if (!dragging) return;
      dragging = false;
      split.classList.remove("dragging");
      document.body.classList.remove("vsplit-dragging");
    };
    split.addEventListener("pointerup", done);
    split.addEventListener("pointercancel", done);
    split.addEventListener("dblclick", () => {
      el.style.width = "";
      el.style.minWidth = "";
      el.style.flex = "";
    });
  };
  // sidebar: grows to the right; cap so the chat column keeps room
  wire("vsplit-left", document.querySelector(".sidebar"), 200, 420, false);
  // work panel: grows to the left, up to 70% of the window
  wire("vsplit-right", $("work-panel"), 300,
    () => Math.round(window.innerWidth * 0.7), true);
}

/* §9: drop-zone file pickers — replaces the raw OS browse inputs */
function initFileDrop(inputId, hint) {
  const input = $(inputId);
  if (!input) return;
  const wrap = div("file-drop");
  wrap.tabIndex = 0;
  wrap.setAttribute("role", "button");
  wrap.setAttribute("aria-label", hint);
  const icoEl = document.createElement("span");
  icoEl.className = "fd-ico";
  icoEl.innerHTML = ico("file");
  const label = document.createElement("span");
  label.className = "fd-text";
  wrap.append(icoEl, label);
  input.classList.add("fd-native");
  input.after(wrap);
  const render = () => {
    const f = input.files && input.files[0];
    label.innerHTML = f
      ? `<b>${escapeHtml(f.name)}</b><span class="fd-size">${fmtK(f.size)}B</span>`
      : `<span class="fd-hint">${escapeHtml(hint)}</span>`;
    wrap.classList.toggle("has-file", !!f);
  };
  wrap.onclick = () => input.click();
  wrap.onkeydown = (e) => {
    if (e.key === "Enter" || e.key === " ") { e.preventDefault(); input.click(); }
  };
  input.addEventListener("change", render);
  for (const ev of ["dragover", "dragenter"]) {
    wrap.addEventListener(ev, (e) => { e.preventDefault(); wrap.classList.add("drag"); });
  }
  for (const ev of ["dragleave", "drop"]) {
    wrap.addEventListener(ev, (e) => { e.preventDefault(); wrap.classList.remove("drag"); });
  }
  wrap.addEventListener("drop", (e) => {
    if (e.dataTransfer.files && e.dataTransfer.files.length) {
      input.files = e.dataTransfer.files;
      render();
    }
  });
  render();
}

function closeSettings() { $("settings-modal").classList.add("hidden"); }

function switchTab(name) {
  document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t.dataset.tab === name));
  document.querySelectorAll(".tab-panel").forEach((p) => p.classList.toggle("active", p.id === `sec-${name}`));
}

/* ------------------------------------------------------------------ providers tab */

function healthBadge(p) {
  const st = p.health_status;
  if (!st) return `<span class="badge unknown">—</span>`;
  if (st === "online") return `<span class="badge ok">online${p.health_latency != null ? ` · ${Math.round(p.health_latency)}мс` : ""}</span>`;
  return `<span class="badge err" title="${escapeHtml(p.last_error || "")}">ошибка</span>`;
}

async function renderProviderList() {
  const { providers } = await api("/api/providers");
  state.providers = providers;
  const list = $("provider-list");
  list.innerHTML = "";
  for (const p of providers) {
    const row = div("provider-row");
    row.innerHTML = `<div><div class="prov-name">${escapeHtml(p.name)} ${healthBadge(p)}</div>
      <div class="host">${escapeHtml(p.host)}</div>
      ${p.proxy_url ? `<div class="host prov-proxy">прокси: ${escapeHtml(p.proxy_url)}</div>` : ""}
      ${p.last_error ? `<div class="prov-error">${escapeHtml(p.last_error)}</div>` : ""}</div>`;
    const actions = div("prov-actions");
    const px = document.createElement("button");
    px.className = "btn-secondary";
    px.textContent = "Прокси";
    px.title = "Прокси этого провайдера — приоритетнее model_api и global";
    px.onclick = () => editProviderProxy(p, row);
    const refresh = document.createElement("button");
    refresh.className = "btn-secondary";
    refresh.textContent = "Ресинк";
    refresh.onclick = async () => {
      refresh.disabled = true;
      try { await api(`/api/providers/${p.id}/models`); } catch (e) { setError(e.message); }
      await loadProvidersAndModels();
      await renderProviderList();
      await renderCatalog();
    };
    const del = document.createElement("button");
    del.className = "btn-danger-sm";
    del.textContent = "Удалить";
    del.onclick = async () => {
      await api(`/api/providers/${p.id}`, { method: "DELETE" });
      await loadProvidersAndModels();
      await renderProviderList();
      await renderCatalog();
      await renderChain();
    };
    actions.append(px, refresh, del);
    row.appendChild(actions);
    list.appendChild(row);
  }
}

function editProviderProxy(p, row) {
  const existing = row.querySelector(".prov-proxy-edit");
  if (existing) { existing.remove(); return; }
  const wrap = div("prov-proxy-edit");
  const inp = document.createElement("input");
  inp.type = "text";
  inp.placeholder = "http/https/socks4/socks5://user:pass@host:port — пусто = без прокси";
  inp.value = p.proxy_url || "";
  const save = document.createElement("button");
  save.className = "btn-primary";
  save.textContent = "OK";
  const cancel = document.createElement("button");
  cancel.className = "btn-secondary";
  cancel.textContent = "Отмена";
  wrap.append(inp, save, cancel);
  row.appendChild(wrap);
  const close = () => wrap.remove();
  cancel.onclick = close;
  const submit = async () => {
    try {
      await api(`/api/providers/${p.id}`, { method: "PATCH",
        body: { proxy_url: inp.value.trim() } });
      await renderProviderList();
    } catch (e) { setError("Прокси: " + e.message); }
  };
  save.onclick = submit;
  inp.addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); submit(); }
    if (e.key === "Escape") close();
  });
  inp.focus();
}

async function onAddProvider(e) {
  e.preventDefault();
  setError("");
  try {
    const body = {
      name: $("pf-name").value.trim() || null,
      host: $("pf-host").value.trim(),
      api_key: $("pf-key").value.trim() || null,
      proxy_url: $("pf-proxy").value.trim() || null,
    };
    const { models, error } = await api("/api/providers", { method: "POST", body });
    $("pf-host").value = ""; $("pf-key").value = "";
    $("pf-name").value = ""; $("pf-proxy").value = "";
    await loadProvidersAndModels();
    await renderProviderList();
    await renderCatalog();
    await renderChain();
    setError("");
    if (error) setError("Подключено, но каталог не получен: " + error);
    if (models.length && !currentModelKey()) {
      const provId = state.providers[state.providers.length - 1]?.id;
      const first = provId ? state.models.find((m) => m.provider_id === provId) : null;
      if (first) await selectModel(first.id);
    }
  } catch (err) {
    setError("Провайдер: " + err.message);
  }
}

async function runDiagnostics() {
  const btn = $("btn-diagnostics");
  btn.disabled = true;
  btn.textContent = "Проверяю…";
  try {
    await api("/api/diagnostics");
    await renderProviderList();
    await renderMcpList();
  } catch (e) { setError(e.message); }
  btn.disabled = false;
  btn.textContent = "Проверить всё";
}

/* ------------------------------------------------------------------ catalog tab */

async function renderCatalog() {
  const { models } = await api("/api/models");
  state.models = models;
  const wrap = $("catalog-list");
  wrap.innerHTML = "";
  if (!models.length) {
    wrap.innerHTML = `<p class="hint">Моделей пока нет — подключите провайдера во вкладке «Провайдеры».</p>`;
    return;
  }
  for (const p of state.providers) {
    const pm = models.filter((m) => m.provider_id === p.id);
    if (!pm.length) continue;
    const group = div("catalog-group");
    const head = div("catalog-group-head");
    head.tabIndex = 0;
    head.innerHTML = `<span class="chev">▶</span><span>${escapeHtml(p.name)}</span>
      <span class="count">${pm.length} мод.</span>`;
    const body = div("catalog-group-body");
    head.onclick = () => group.classList.toggle("open");
    head.onkeydown = (e) => { if (e.key === " " || e.key === "Enter") { e.preventDefault(); group.classList.toggle("open"); } };
    for (const m of pm) body.appendChild(catalogRow(m));
    group.append(head, body);
    wrap.appendChild(group);
  }
}

function catalogRow(m) {
  const caps = m.capabilities || {};
  // show ONLY capabilities that are actually known to be on (API or override);
  // everything else stays in the toggle grid below — no clutter rows
  const truePill = (key) => caps[key] === true
    ? `<span class="pill">${key}</span>` : "";
  const isEmb = !!caps.embedding;
  const pills = isEmb
    ? `<span class="pill emb">embedding</span>`
    : ["tools", "vision", "thinking", "video", "audio"].map(truePill).join("");
  const capsHint = pills ? "" : `<span class="pill muted" title="возможности не заявлены — включите тумблерами">возможности не указаны</span>`;
  // the title defaults to model_id — echoing the id again doubles the name
  const showId = !!(m.model_id && m.model_id !== (m.title || m.model_id));
  const row = div("catalog-row");
  row.innerHTML = `
    <div class="cat-title">${escapeHtml(m.title || m.model_id)}
      ${m.override ? `<span class="badge override" title="есть ручной оверрайд">override</span>` : ""}
      ${isEmb ? `<span class="badge override" title="API заявил тип embedding">embedding</span>` : ""}
    </div>
    <div class="cat-badges">${showId ? `<span class="cat-id">${escapeHtml(m.model_id)}</span>` : ""}${pills}${capsHint}</div>
    <div class="cat-grid">
      <label>контекст, токенов <input data-k="context_length" type="number" min="1024" step="1024" value="${m.context_length ?? ""}" placeholder="неизвестно"></label>
    </div>
    <div class="cat-caps-grid">
      ${["tools", "vision", "thinking", "video", "audio", "embedding"].map((k) => `
        <label class="toggle-row small"><span>${k}</span>
          <input type="checkbox" data-cap="${k}" class="toggle" ${caps[k] ? "checked" : ""}></label>`).join("")}
    </div>
    <details class="cat-raw" ${m.raw ? "" : "hidden"}>
      <summary>raw из /v1/models — всё, что отдал API</summary>
      <pre></pre>
    </details>
    <div class="cat-actions"></div>`;
  if (m.raw) {
    try { row.querySelector(".cat-raw pre").textContent = JSON.stringify(JSON.parse(m.raw), null, 2); }
    catch { row.querySelector(".cat-raw pre").textContent = m.raw; }
  }
  const actions = row.querySelector(".cat-actions");

  const probe = document.createElement("button");
  probe.className = "btn-secondary";
  probe.textContent = "Проверить контекст через API";
  const probeOut = document.createElement("span");
  probeOut.className = "hint";
  probe.onclick = async () => {
    probe.disabled = true;
    probe.textContent = "пробный запрос…";
    try {
      const res = await api("/api/models/probe", { method: "POST", body: { model_key: m.id } });
      if (res.context_length) {
        row.querySelector("[data-k=context_length]").value = res.context_length;
        probeOut.textContent = `API назвал: ${res.context_length.toLocaleString("ru-RU")} ток.`;
      } else {
        probeOut.textContent = `API не раскрыл: ${(res.reply || "—").slice(0, 160)}`;
      }
    } catch (e) { probeOut.textContent = "Проб не удался: " + e.message; }
    probe.disabled = false;
    probe.textContent = "Проверить контекст через API";
  };
  actions.append(probe, probeOut);

  const save = document.createElement("button");
  save.className = "btn-primary";
  save.textContent = "Сохранить";
  save.onclick = async () => {
    const override = {};
    const ctx = row.querySelector("[data-k=context_length]").value;
    if (ctx !== "") override.context_length = parseInt(ctx, 10);
    const capsOn = {};
    row.querySelectorAll("[data-cap]").forEach((cb) => {
      if (cb.checked) capsOn[cb.dataset.cap] = true;
    });
    if (Object.keys(capsOn).length) override.capabilities = capsOn;
    try {
      await api("/api/models/override", { method: "PATCH", body: { model_key: m.id, override } });
      await renderCatalog();
      await renderChain();
      renderModelPicker();
      refreshContext();
    } catch (e) { setError(e.message); }
  };

  if (m.override) {
    const reset = document.createElement("button");
    reset.className = "btn-danger-sm";
    reset.textContent = "Сбросить оверрайд";
    reset.onclick = async () => {
      await api(`/api/models/override?model_key=${encodeURIComponent(m.id)}`, { method: "DELETE" });
      await renderCatalog();
      renderModelPicker();
      refreshContext();
    };
    actions.append(reset, save);
  } else {
    actions.append(save);
  }
  return row;
}

/* ------------------------------------------------------------------ MCP tab */

async function renderMcpList() {
  const { servers } = await api("/api/mcp");
  const wrap = $("mcp-list");
  wrap.innerHTML = "";
  if (!servers.length) {
    wrap.innerHTML = `<p class="hint">MCP-серверов нет. Киньте URL — discovery вытащит тулзы, промпты и ресурсы сам.</p>`;
    return;
  }
  for (const s of servers) {
    const card = div("mcp-card");
    const st = s.health_status;
    const badge = st === "online"
      ? `<span class="badge ok">online${s.health_latency != null ? ` · ${Math.round(s.health_latency)}мс` : ""}</span>`
      : st === "error" ? `<span class="badge err" title="${escapeHtml(s.last_error || "")}">ошибка</span>`
      : `<span class="badge unknown">—</span>`;
    card.innerHTML = `<div class="mcp-head"><span class="mcp-name">${escapeHtml(s.name)} ${badge}</span>
        <span class="host">${escapeHtml(s.url)}</span></div>
      <div class="mcp-sub">тулз: ${(s.tools || []).length} · промптов: ${s.prompts_count ?? 0} · ресурсов: ${s.resources_count ?? 0}</div>`;
    const tools = div("mcp-tools");
    for (const t of s.tools || []) {
      const el = div("mcp-tool");
      // the name the model calls; native name shown too when a collision
      // forced a suffix
      const shown = t.id !== t.tool_name ? `${t.id} ← ${t.tool_name}` : t.id;
      el.innerHTML = `<code>${escapeHtml(shown)}</code><span>${escapeHtml(t.description || "")}</span>`;
      tools.appendChild(el);
    }
    card.appendChild(tools);
    const actions = div("prov-actions");
    const refresh = document.createElement("button");
    refresh.className = "btn-secondary";
    refresh.textContent = "Обновить";
    refresh.onclick = async () => {
      try { await api(`/api/mcp/${s.id}/refresh`, { method: "POST" }); await renderMcpList(); }
      catch (e) { setError(e.message); }
    };
    const del = document.createElement("button");
    del.className = "btn-danger-sm";
    del.textContent = "Удалить";
    del.onclick = async () => { await api(`/api/mcp/${s.id}`, { method: "DELETE" }); await renderMcpList(); };
    actions.append(refresh, del);
    card.appendChild(actions);
    wrap.appendChild(card);
  }
}

async function onAddMcp(e) {
  e.preventDefault();
  setError("");
  try {
    await api("/api/mcp", { method: "POST", body: { url: $("mcp-url").value.trim() } });
    $("mcp-url").value = "";
    await renderMcpList();
  } catch (err) {
    setError("MCP: " + err.message);
  }
}

/* ------------------------------------------------------------------ reliability tab */

async function loadReliability() {
  const s = await api("/api/settings");
  state.settings = s;
  try { state.chain = JSON.parse(s.fallback_chain || "[]"); } catch { state.chain = []; }
  $("retry-attempts").value = s.retry_max_attempts || 2;
  $("retry-backoff").value = s.retry_backoff || "exp:1.5";
  renderChain();
}

function renderChain() {
  const list = $("chain-list");
  if (!list) return;
  list.innerHTML = "";
  state.chain.forEach((key, i) => {
    const row = div("chain-row");
    const sel = document.createElement("select");
    sel.innerHTML = `<option value="">— выберите модель —</option>` +
      state.models.map((m) => {
        const p = state.providers.find((x) => x.id === m.provider_id);
        return `<option value="${m.id}" ${m.id === key ? "selected" : ""}>${escapeHtml((p ? p.name + " — " : "") + (m.title || m.model_id))}</option>`;
      }).join("");
    sel.onchange = () => { state.chain[i] = sel.value; };
    row.appendChild(sel);
    const mk = (txt, fn) => {
      const b = document.createElement("button");
      b.className = "btn-danger-sm";
      b.textContent = txt;
      b.onclick = fn;
      return b;
    };
    row.append(
      mk("↑", () => { if (i > 0) { [state.chain[i - 1], state.chain[i]] = [state.chain[i], state.chain[i - 1]]; renderChain(); } }),
      mk("↓", () => { if (i < state.chain.length - 1) { [state.chain[i + 1], state.chain[i]] = [state.chain[i], state.chain[i + 1]]; renderChain(); } }),
      mk("—", () => { state.chain.splice(i, 1); renderChain(); }));
    list.appendChild(row);
  });
  if (!state.chain.length) {
    list.innerHTML = `<p class="hint">Резервов нет — при сбое ход закончится ошибкой.</p>`;
  }
}

async function saveReliability() {
  try {
    await api("/api/settings", {
      method: "PATCH",
      body: {
        fallback_chain: state.chain.filter(Boolean),
        retry_max_attempts: parseInt($("retry-attempts").value, 10) || 2,
        retry_backoff: $("retry-backoff").value,
      },
    });
    setError("");
    $("btn-save-reliability").textContent = "Сохранено ✓";
    setTimeout(() => { $("btn-save-reliability").textContent = "Сохранить"; }, 1500);
  } catch (e) { setError(e.message); }
}

/* ------------------------------------------------------------------ context tab (§11 Phase 3) */

async function loadContextSettings() {
  const s = await api("/api/settings");
  state.settings = s;
  $("compact-threshold").value = s.compact_threshold || "80%";
  $("loop-limit").value = s.loop_iterations_limit || 24;
  $("compact-head").value = s.compact_keep_head ?? 2;
  $("compact-tail").value = s.compact_keep_last ?? 8;
  $("compact-auto").checked = !(s.compact_auto in { "0": 1, false: 1, off: 1 });
  $("two-step").checked = !(s.two_step_summarize in { "0": 1, false: 1, off: 1 });
  $("failure-streak").value = s.tool_failure_streak || 3;
  $("default-system").value = s.default_system_prompt || "";
  // model pickers reuse the already loaded catalog
  const fill = (selId, key) => {
    const sel = $(selId);
    sel.innerHTML = `<option value="">— как в чате —</option>` +
      state.models.map((m) => {
        const p = state.providers.find((x) => x.id === m.provider_id);
        return `<option value="${m.id}" ${m.id === key ? "selected" : ""}>${escapeHtml((p ? p.name + " — " : "") + (m.title || m.model_id))}</option>`;
      }).join("");
  };
  fill("compact-model", s.compact_model_key || "");
  fill("summarizer-model", s.summarizer_model_key || "");
}

async function saveContextSettings() {
  try {
    await api("/api/settings", {
      method: "PATCH",
      body: {
        compact_threshold: $("compact-threshold").value.trim() || "80%",
        loop_iterations_limit: parseInt($("loop-limit").value, 10) || 24,
        compact_keep_head: parseInt($("compact-head").value, 10) || 0,
        compact_keep_last: parseInt($("compact-tail").value, 10) || 8,
        compact_auto: $("compact-auto").checked ? "1" : "0",
        two_step_summarize: $("two-step").checked ? "1" : "0",
        tool_failure_streak: parseInt($("failure-streak").value, 10) || 3,
        default_system_prompt: $("default-system").value,
        compact_model_key: $("compact-model").value || "",
        summarizer_model_key: $("summarizer-model").value || "",
      },
    });
    setError("");
    $("btn-save-context").textContent = "Сохранено ✓";
    setTimeout(() => { $("btn-save-context").textContent = "Сохранить"; }, 1500);
  } catch (e) { setError(e.message); }
}

async function compactNow() {
  if (!state.chatId) return;
  try {
    const res = await api(`/api/chats/${state.chatId}/compact`, { method: "POST" });
    setError(res.compact ? "" : (res.note || "нечего сжимать"));
  } catch (e) { setError(e.message); }
}

async function clearContext() {
  if (!state.chatId) return;
  try {
    await api(`/api/chats/${state.chatId}/clear`, { method: "POST" });
    setError("");
  } catch (e) { setError(e.message); }
}

/* ------------------------------------------------------------------ chat tab */

function syncGenParams(params) {
  $("gen-temp").value = params.temperature ?? "";
  $("gen-topp").value = params.top_p ?? "";
  $("gen-max").value = params.max_tokens ?? "";
  $("gen-effort").value = params.reasoning_effort || "";
}

async function saveChatSettings() {
  if (!state.chatId) return;
  const params = {};
  if ($("gen-temp").value !== "") params.temperature = parseFloat($("gen-temp").value);
  if ($("gen-topp").value !== "") params.top_p = parseFloat($("gen-topp").value);
  if ($("gen-max").value !== "") params.max_tokens = parseInt($("gen-max").value, 10);
  if ($("gen-effort").value) params.reasoning_effort = $("gen-effort").value;

  await api(`/api/chats/${state.chatId}`, {
    method: "PATCH",
    body: {
      system_prompt: $("system-prompt").value || null,
      send_think_blocks: $("send-think").checked,
      compact_threshold: $("chat-compact-threshold").value.trim() || null,
      params,
    },
  });
  setError("");
  $("btn-save-system").textContent = "Сохранено ✓";
  setTimeout(() => { $("btn-save-system").textContent = "Сохранить"; }, 1500);
}

/* ------------------------------------------------------------------ right working zone (§13): files + git */

/* Codex-style views: files/git share the side layout; terminal and browser
   take the WHOLE main area (work-panel gets .full). */
function openPanel(tab) {
  const p = $("work-panel");
  p.classList.remove("hidden");
  const full = tab === "terminal" || tab === "browser";
  const wasFull = p.classList.contains("full");
  if (full && !wasFull) {
    // a side-mode drag leaves an inline width; in full mode that width wins
    // over inset:0 and pins the absolute panel to the LEFT — park it
    p.dataset.sideW = p.style.width || "";
    p.dataset.sideMin = p.style.minWidth || "";
    p.dataset.sideFlex = p.style.flex || "";
    p.style.width = ""; p.style.minWidth = ""; p.style.flex = "";
  } else if (!full && wasFull) {
    p.style.width = p.dataset.sideW || "";
    p.style.minWidth = p.dataset.sideMin || "";
    p.style.flex = p.dataset.sideFlex || "";
    delete p.dataset.sideW; delete p.dataset.sideMin; delete p.dataset.sideFlex;
  }
  p.classList.toggle("full", full);
  $("vsplit-right").classList.toggle("hidden", full || p.classList.contains("hidden"));
  document.querySelectorAll(".panel-tab").forEach((t) =>
    t.classList.toggle("active", t.dataset.ptab === tab));
  document.querySelectorAll(".panel-view").forEach((v) =>
    v.classList.toggle("active", v.id === `pview-${tab}`));
  if (tab === "files") { refreshFileTree(); refreshCheckpoints(); }
  if (tab === "terminal") termOnOpen();
  if (tab === "browser") browserOnOpen();
  if (tab !== "browser") browserDisconnect();
}

function togglePanel() {
  const p = $("work-panel");
  if (p.classList.contains("hidden")) openPanel("files");
  else {
    p.classList.add("hidden");
    p.classList.remove("full");
    $("vsplit-right").classList.add("hidden");
    browserDisconnect();
  }
}

async function refreshFileTree() {
  const tree = $("file-tree");
  if (!tree || $("work-panel").classList.contains("hidden")) return;
  if (!tree.dataset.loaded) {
    await loadDir("");
    tree.dataset.loaded = "1";
  }
}

async function loadDir(path) {
  const tree = $("file-tree");
  try {
    const q = path ? `?path=${encodeURIComponent(path)}` : "";
    const { entries } = await api(`/api/workspace/tree${q}`);
    renderDir(tree, path, entries);
  } catch (e) {
    tree.innerHTML = `<p class="hint">${escapeHtml(e.message)}</p>`;
  }
}

function renderDir(container, dirPath, entries) {
  let parent = container;
  if (dirPath) {
    parent = document.getElementById(`dir-${btoa(dirPath).replace(/=/g, "")}`);
    if (!parent) return;
    parent.innerHTML = "";
  } else {
    container.innerHTML = "";
  }
  for (const e of entries) {
    const row = div("file-row" + (e.type === "dir" ? " dir" : ""));
    row.tabIndex = 0;
    row.innerHTML = `<span class="file-ico">${e.type === "dir" ? "▸" : e.type === "link" ? "↗" : "·"}</span>`
      + `<span class="file-name">${escapeHtml(e.name)}</span>`
      + (e.type === "file" ? `<span class="file-size">${fmtK(e.size)}B</span>` : "");
    if (e.type === "dir") {
      const kids = div("file-kids hidden");
      kids.id = `dir-${btoa(e.path).replace(/=/g, "")}`;
      const toggle = () => {
        kids.classList.toggle("hidden");
        if (!kids.dataset.loaded) { loadDir(e.path); kids.dataset.loaded = "1"; }
        row.querySelector(".file-ico").textContent = kids.classList.contains("hidden") ? "▸" : "▾";
      };
      row.onclick = toggle;
      row.onkeydown = (ev) => { if (ev.key === " " || ev.key === "Enter") { ev.preventDefault(); toggle(); } };
      parent.append(row, kids);
    } else {
      row.onclick = () => openFile(e.path);
      row.onkeydown = (ev) => { if (ev.key === " " || ev.key === "Enter") { ev.preventDefault(); openFile(e.path); } };
      parent.append(row);
    }
  }
  if (!entries.length) {
    parent.innerHTML = `<p class="hint">пусто</p>`;
  }
}

async function openFile(path) {
  try {
    const { content, binary, size } = await api(`/api/workspace/file?path=${encodeURIComponent(path)}`);
    const v = $("file-viewer");
    v.classList.remove("hidden");
    $("file-viewer-path").textContent = path;
    $("file-viewer-content").textContent = binary
      ? `[бинарный файл, ${fmtK(size)} байт]`
      : content || "[пусто]";
  } catch (e) { setError(e.message); }
}

/* checkpoints + revert MR (§14) */

async function refreshCheckpoints() {
  const list = $("cp-list");
  if (!list || !state.chatId || $("work-panel").classList.contains("hidden")) return;
  try {
    const { checkpoints } = await api(`/api/checkpoints?chat=${state.chatId}`);
    list.innerHTML = "";
    if (!checkpoints.length) {
      list.innerHTML = `<p class="hint">Ходов с файловыми изменениями ещё не было.</p>`;
      return;
    }
    for (const cp of [...checkpoints].reverse()) {
      const row = div("cp-row");
      row.tabIndex = 0;
      row.innerHTML = `<div class="cp-sha"><code>${escapeHtml((cp.commit_sha || "").slice(0, 8))}</code>`
        + `<span class="cp-date">${new Date((cp.ts || 0) * 1000).toLocaleTimeString()}</span></div>`
        + `<div class="cp-msg">${escapeHtml(cp.message || "")}</div>`
        + `<div class="cp-files">${(cp.files || []).map(escapeHtml).join(", ") || "—"}</div>`;
      const actions = div("cp-actions");
      const diffBtn = document.createElement("button");
      diffBtn.className = "btn-secondary";
      diffBtn.textContent = "Дифф";
      diffBtn.onclick = () => showCheckpointDiff(cp);
      const revertBtn = document.createElement("button");
      revertBtn.className = "btn-danger-sm";
      revertBtn.textContent = "Откатить…";
      revertBtn.onclick = () => showRevertMR(cp);
      actions.append(diffBtn, revertBtn);
      row.appendChild(actions);
      list.appendChild(row);
    }
  } catch { /* chat without checkpoints */ }
}

async function showCheckpointDiff(cp) {
  try {
    const { diff } = await api(`/api/checkpoints/${cp.id}/diff`);
    document.querySelectorAll(".panel-tab").forEach((t) =>
      t.classList.toggle("active", t.dataset.ptab === "files"));
    document.querySelectorAll(".panel-view").forEach((x) =>
      x.classList.toggle("active", x.id === "pview-files"));
    const v = $("file-viewer");
    v.classList.remove("hidden");
    $("file-viewer-path").textContent = `⎇ ${(cp.commit_sha || "").slice(0, 10)}`;
    $("file-viewer-content").textContent = diff || "[пустой дифф]";
  } catch (e) { setError(e.message); }
}

let mrCheckpoint = null;

async function showRevertMR(cp) {
  mrCheckpoint = cp;
  try {
    const mr = await api(`/api/checkpoints/${cp.id}/revert`, { method: "POST" });
    $("mr-modal").classList.remove("hidden");
    $("mr-info").innerHTML = `Чекпоинт <code>${escapeHtml((cp.commit_sha || "").slice(0, 10))}</code>`
      + ` — «${escapeHtml(cp.message || "")}». Ниже — реверс-патч: что исчезнет из файлов при апруве.`;
    const conflicts = mr.conflicts || [];
    const cbox = $("mr-conflicts");
    cbox.classList.toggle("hidden", !conflicts.length);
    cbox.innerHTML = conflicts.length
      ? `Конфликт с поздними правками: ${conflicts.map(escapeHtml).join(", ")}`
        + ` — «Применить с перезаписью» вернёт эти файлы к состоянию до чекпоинта.`
      : "";
    $("btn-mr-force").classList.toggle("hidden", !conflicts.length);
    $("btn-mr-apply").classList.toggle("hidden", !!conflicts.length);
    $("mr-diff").textContent = mr.revert_patch || mr.note || "[пусто]";
  } catch (e) { setError(e.message); }
}

async function applyRevert(force) {
  if (!mrCheckpoint) return;
  try {
    await api(`/api/checkpoints/${mrCheckpoint.id}/revert/apply`, {
      method: "POST", body: { force: !!force } });
    $("mr-modal").classList.add("hidden");
    await refreshCheckpoints();
    $("file-tree").dataset.loaded = "";
    if (!$("work-panel").classList.contains("hidden")) await loadDir("");
    setError("");
  } catch (e) { setError(e.message); }
}

async function createManualCheckpoint() {
  try {
    const res = await api("/api/checkpoints", {
      method: "POST", body: { chat_id: state.chatId, message: "ручной чекпоинт" } });
    await refreshCheckpoints();
    setError(res.checkpoint ? "" : (res.note || ""));
  } catch (e) { setError(e.message); }
}

/* ------------------------------------------------------------------ browser engines (§13)
   Playwright-native: list = engine executables, install = its installer. */

let enginesPoll = null;

async function loadBrowserEngines() {
  const wrap = $("browser-engines");
  if (!wrap) return {};
  let data = {};
  try { data = await api("/api/browser/engines"); }
  catch (e) { wrap.innerHTML = `<p class="hint">${escapeHtml(e.message)}</p>`; return {}; }
  renderEngineList(wrap, data, { settings: true });
  const note = $("browser-engines-note");
  if (note) {
    note.textContent = data.playwright === false
      ? "Playwright не установлен: pip install playwright"
      : (data.selected ? `Активный движок: ${data.selected}` : "Ничего не установлено — выберите движок и нажмите «Установить».");
  }
  return data;
}

function renderEngineList(wrap, data, opts = {}) {
  wrap.innerHTML = "";
  if (!data.engines?.length) {
    wrap.innerHTML = `<p class="hint">Список пуст.</p>`;
    return;
  }
  const busy = data.engines.some((e) => e.installing);
  for (const e of data.engines) {
    const row = div("engine-row" + (e.selected ? " selected" : ""));
    const name = document.createElement("span");
    name.className = "engine-name";
    name.textContent = e.name;
    const sub = document.createElement("span");
    sub.className = "engine-sub";
    sub.innerHTML = e.installed
      ? (e.selected ? "установлен · используется" : "установлен")
      : (e.installing ? "установка идёт…" : "не установлен");
    row.append(name, sub);
    const btns = document.createElement("span");
    btns.style.display = "inline-flex";
    btns.style.gap = "6px";
    if (e.installing) {
      const st = document.createElement("span");
      st.className = "badge ok";
      st.textContent = "…";
      btns.appendChild(st);
    } else if (!e.installed) {
      const b = document.createElement("button");
      b.className = "btn-primary";
      b.textContent = "Установить";
      b.onclick = async () => {
        b.disabled = true;
        b.textContent = "Запуск…";
        try { await api("/api/browser/install", { method: "POST", body: { engine: e.id } }); }
        catch (err) { setError(err.message); b.disabled = false; b.textContent = "Установить"; return; }
        startEnginesPoll(opts);
      };
      btns.appendChild(b);
    } else if (!e.selected) {
      const b = document.createElement("button");
      b.className = "btn-secondary";
      b.textContent = "Использовать";
      b.onclick = async () => {
        try {
          await api("/api/browser/select", { method: "POST", body: { engine: e.id } });
          await loadBrowserEngines();
          if (opts.onSelect) opts.onSelect();
        } catch (err) { setError(err.message); }
      };
      btns.appendChild(b);
    } else {
      const b = document.createElement("span");
      b.className = "badge ok";
      b.textContent = "активен";
      btns.appendChild(b);
    }
    row.appendChild(btns);
    if (e.log?.length && (e.installing || !e.installed)) {
      const log = div("engine-log");
      log.textContent = e.log.join("\n");
      row.appendChild(log);
    }
    wrap.appendChild(row);
  }
  if (busy) startEnginesPoll(opts);
}

function startEnginesPoll(opts) {
  if (enginesPoll) return;
  enginesPoll = setInterval(async () => {
    let data = null;
    try { data = await api("/api/browser/engines"); }
    catch { return; }
    const busy = data.engines?.some((e) => e.installing);
    const wrap = opts.modal ? $("browser-modal-engines") : $("browser-engines");
    if (wrap) renderEngineList(wrap, data, opts);
    if (!busy) {
      clearInterval(enginesPoll);
      enginesPoll = null;
      if (!opts.modal) loadBrowserEngines();
      if (opts.onDone) opts.onDone(data);
    }
  }, 1500);
}

function showBrowserMissingModal() {
  const modal = $("browser-modal");
  if (!modal || !modal.classList.contains("hidden")) return;
  modal.classList.remove("hidden");
  api("/api/browser/engines").then((data) => {
    renderEngineList($("browser-modal-engines"), data, {
      modal: true,
      onDone: () => $("browser-modal-log").textContent = "Готово — можно повторить запрос браузера.",
    });
  }).catch((e) => {
    $("browser-modal-engines").innerHTML = `<p class="hint">${escapeHtml(e.message)}</p>`;
  });
}

/* ------------------------------------------------------------------ permissions tab */

async function loadPermissions() {
  const { rules, mode } = await api("/api/permissions/rules");
  const s = await api("/api/settings");
  $("perm-ask").checked = mode !== "allow_all";
  $("timeout-default").value = s.tool_timeout_default || 30;
  $("timeout-max").value = s.tool_timeout_max || 600;
  $("isolation-mode").value = s.isolation || "off";
  renderRuleList(rules);
}

function renderRuleList(rules) {
  const list = $("rule-list");
  list.innerHTML = "";
  if (!rules.length) {
    list.innerHTML = `<p class="hint">Правил нет: ask-режим спрашивает по факту, allow_all разрешает всё кроме разрушительного bash.</p>`;
    return;
  }
  for (const r of rules) {
    const row = div("rule-row");
    row.innerHTML = `<span class="badge ${r.effect === "deny" ? "err" : "ok"}">${r.effect}</span>`
      + `<code>${escapeHtml(r.tool)}</code>`
      + `<code class="rule-pattern">${escapeHtml(r.pattern || "*")}</code>`
      + (r.note ? `<span class="rule-note">${escapeHtml(r.note)}</span>` : "");
    const del = document.createElement("button");
    del.className = "btn-danger-sm";
    del.textContent = "—";
    del.title = "удалить правило";
    del.onclick = async () => { await api(`/api/permissions/rules/${r.id}`, { method: "DELETE" }); loadPermissions(); };
    row.appendChild(del);
    list.appendChild(row);
  }
}

async function onAddRule(e) {
  e.preventDefault();
  try {
    await api("/api/permissions/rules", {
      method: "POST",
      body: {
        effect: $("rule-effect").value,
        tool: $("rule-tool").value.trim() || "*",
        pattern: $("rule-pattern").value.trim(),
      },
    });
    $("rule-tool").value = "";
    $("rule-pattern").value = "";
    await loadPermissions();
  } catch (err) { setError("Правило: " + err.message); }
}

async function savePermissions() {
  try {
    await api("/api/settings", {
      method: "PATCH",
      body: {
        permission_mode: $("perm-ask").checked ? "ask" : "allow_all",
        tool_timeout_default: parseInt($("timeout-default").value, 10) || 30,
        tool_timeout_max: parseInt($("timeout-max").value, 10) || 600,
        isolation: $("isolation-mode").value,
      },
    });
    setError("");
    $("btn-save-permissions").textContent = "Сохранено ✓";
    setTimeout(() => { $("btn-save-permissions").textContent = "Сохранить"; }, 1500);
  } catch (e) { setError(e.message); }
}

/* ================================================================== Phase 4 — terminal (§13) */

function termReset() {
  state.termBlocks = [];
  const log = $("term-log");
  if (log) log.innerHTML = `<div class="term-empty">Пока пусто: bash-команды агента и фоновые задачи появятся здесь с живым выводом.</div>`;
}

function termOnOpen() {
  // rebuild from the buffered events (already captured during history replay)
  const log = $("term-log");
  if (!log) return;
  if (!state.termBlocks.length) return;
  log.innerHTML = "";
  for (const b of state.termBlocks) log.appendChild(b.el);
  log.scrollTop = log.scrollHeight;
}

function termBlockEl(key, cmd, label) {
  const wrap = div("term-block");
  wrap.dataset.key = key;
  const head = div("term-head-line");
  head.innerHTML = `<span class="term-prompt">${escapeHtml(label)}</span><span class="term-cmd"></span><span class="term-status">…</span>`;
  head.querySelector(".term-cmd").textContent = cmd;
  const out = document.createElement("pre");
  out.className = "term-out";
  wrap.append(head, out);
  return { el: wrap, out, status: head.querySelector(".term-status") };
}

function termPush(key, cmd, label) {
  const log = $("term-log");
  const block = termBlockEl(key, cmd, label);
  state.termBlocks.push(block);
  if (log) {
    const empty = log.querySelector(".term-empty");
    if (empty) empty.remove();
    log.appendChild(block.el);
    log.scrollTop = log.scrollHeight;
  }
  return block;
}

function termFind(key) {
  return state.termBlocks.find((b) => b.el.dataset.key === key);
}

function termOnToolCall(ev) {
  // bash tool call -> a terminal entry keyed by the call_id
  if (ev.data?.name === "bash") {
    termPush(`bash:${ev.data.call_id}`, ev.data.args?.command || "", "$");
  }
}

function termOnToolResult(ev) {
  const block = termFind(`bash:${ev.data?.call_id}`);
  if (!block) return;
  const r = ev.data?.result || {};
  block.status.textContent = r.error ? "ошибка" : `exit ${r.exit_code ?? "?"}`;
  block.status.classList.add(r.error ? "err" : "ok");
  block.out.textContent =
    [r.stdout, r.stderr].filter(Boolean).join("\n").trim() ||
    r.error || "(пусто)";
  const log = $("term-log");
  if (log) log.scrollTop = log.scrollHeight;
}

function termOnBgStart(ev) {
  const d = ev.data || {};
  const block = termPush(`bg:${d.task_id}`, d.command || "", `bg ${d.task_id}`);
  block.status.textContent = "running";
  block.status.classList.add("run");
  const cancel = document.createElement("button");
  cancel.className = "btn-danger-sm term-cancel";
  cancel.textContent = "стоп";
  cancel.title = "остановить фоновую задачу";
  cancel.onclick = async () => {
    try { await api(`/api/background/${encodeURIComponent(d.task_id)}/cancel`, { method: "POST" }); }
    catch (e) { setError(e.message); }
  };
  block.el.querySelector(".term-head-line").appendChild(cancel);
  openPanelIfVisible("terminal");
}

function termOnBgEnd(ev) {
  const d = ev.data || {};
  const block = termFind(`bg:${d.task_id}`);
  if (!block) {
    // task started before this page opened (replay) — synthesize the entry
    termPush(`bg:${d.task_id}`, d.command || "", `bg ${d.task_id}`);
  }
  const b = termFind(`bg:${d.task_id}`);
  b.status.textContent = d.killed ? "остановлена" : `exit ${d.exit_code ?? "?"}`;
  b.status.classList.remove("run");
  b.status.classList.add(d.killed || d.exit_code ? "err" : "ok");
  b.out.textContent = (d.tail || "").trim() || "(пусто)";
  b.el.querySelector(".term-cancel")?.remove();
}

function termLiveOutput(frame) {
  // transient SSE live frame {live:'bg_output', task_id, delta}
  let block = termFind(`bg:${frame.task_id}`);
  if (!block) block = termPush(`bg:${frame.task_id}`, "", `⚙ ${frame.task_id}`);
  block.out.textContent += frame.delta || "";
  const log = $("term-log");
  if (log) log.scrollTop = log.scrollHeight;
}

function openPanelIfVisible(tab) {
  const p = $("work-panel");
  if (p.classList.contains("hidden")) return;
  const active = document.querySelector(".panel-view.active");
  if (active && active.id === `pview-${tab}`) return; // already watching
}

/* ================================================================== Phase 4 — browser (§13) */

async function browserOnOpen() {
  $("browser-note").textContent = "";
  try {
    const st = await api("/api/browser/status");
    if (!st.playwright) {
      $("browser-overlay").textContent = "playwright не установлен";
      $("browser-overlay").classList.add("show");
      $("browser-note").textContent = st.hint || "";
      return;
    }
  } catch { /* server unreachable — WS will fail loudly */ }
  browserConnect();
}

function browserDisconnect() {
  if (state.browserWs) {
    try { state.browserWs.close(); } catch { /* already closed */ }
    state.browserWs = null;
  }
  const ov = $("browser-overlay");
  if (ov) { ov.textContent = "отключено"; ov.classList.remove("show"); }
}

function browserConnect() {
  browserDisconnect();
  if (!state.chatId) {
    // screencast is bound to a chat session — without one there is nothing to stream
    const ov = $("browser-overlay");
    ov.textContent = "нет активного чата — создайте чат, и браузер появится здесь";
    ov.classList.add("show");
    return;
  }
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/ws/browser/${state.chatId}`);
  state.browserWs = ws;
  const ov = $("browser-overlay");
  ov.textContent = "подключение…";
  ov.classList.add("show");
  state.browserFrames = 0;

  ws.binaryType = "arraybuffer";
  ws.onopen = () => { ov.classList.remove("show"); };
  ws.onmessage = (m) => {
    if (typeof m.data === "string") {
      try {
        const hello = JSON.parse(m.data);
        if (hello.type === "hello") {
          state.browserHello = hello;
          const canvas = $("browser-canvas");
          canvas.width = hello.width;
          canvas.height = hello.height;
          $("browser-url").value = (hello.url && hello.url !== "about:blank")
            ? hello.url : "";
        } else if (hello.type === "nav-error") {
          const note = $("browser-note");
          note.textContent = hello.message || "не открылось";
          setTimeout(() => { note.textContent = ""; }, 5000);
        } else if (hello.type === "error") {
          ov.textContent = hello.message || "ошибка браузера";
          ov.classList.add("show");
        }
      } catch { /* not JSON */ }
      return;
    }
    // binary JPEG frame
    state.browserFrames += 1;
    browserDrawFrame(m.data);
  };
  ws.onclose = () => {
    if (state.browserWs === ws) {
      state.browserWs = null;
      ov.textContent = "сессия закрыта";
      ov.classList.add("show");
    }
  };
  ws.onerror = () => { ov.textContent = "нет соединения"; ov.classList.add("show"); };
}

let browserFpsTimer = null;
function browserDrawFrame(buf) {
  const canvas = $("browser-canvas");
  const ctx = canvas.getContext("2d");
  const blob = new Blob([buf], { type: "image/jpeg" });
  createImageBitmap(blob).then((bmp) => {
    ctx.drawImage(bmp, 0, 0, canvas.width, canvas.height);
    bmp.close();
  }).catch(() => { /* torn frame — next one is coming */ });
  if (!browserFpsTimer) {
    const t0 = performance.now();
    browserFpsTimer = setTimeout(() => {
      const fps = Math.round(state.browserFrames / Math.max((performance.now() - t0) / 1000, 0.001));
      $("browser-fps").textContent = state.browserFrames ? `${fps} fps` : "";
      state.browserFrames = 0;
      browserFpsTimer = null;
    }, 2000);
  }
}

async function browserGo(rawUrl) {
  const ws = state.browserWs;
  if (!ws || ws.readyState !== 1) {
    setError(state.chatId
      ? "Браузер ещё подключается — подождите пару секунд"
      : "Сначала создайте чат — браузер привязан к сессии чата");
    return;
  }
  let url = (rawUrl || "").trim();
  // a bare host gets https:// — page.goto() refuses scheme-less URLs and
  // the session would silently fall back to about:blank
  if (url && !/^[a-z][a-z0-9+.-]*:/i.test(url)) url = "https://" + url;
  if (!url) return;
  ws.send(JSON.stringify({ type: "goto", url }));
}

function browserNav(action) {
  const ws = state.browserWs;
  if (!ws || ws.readyState !== 1) return;
  ws.send(JSON.stringify({ type: action }));
}

function browserCanvasXY(e) {
  // object-fit: contain letterboxing — map the click onto the real canvas area
  const canvas = $("browser-canvas");
  const rect = canvas.getBoundingClientRect();
  const scale = Math.min(rect.width / canvas.width, rect.height / canvas.height);
  const cw = canvas.width * scale, ch = canvas.height * scale;
  const ox = rect.left + (rect.width - cw) / 2;
  const oy = rect.top + (rect.height - ch) / 2;
  return {
    x: Math.min(Math.max(Math.round((e.clientX - ox) / scale), 0), canvas.width - 1),
    y: Math.min(Math.max(Math.round((e.clientY - oy) / scale), 0), canvas.height - 1),
  };
}

function browserForward(e, type) {
  const ws = state.browserWs;
  if (!ws || ws.readyState !== 1 || !$("browser-input").checked) return;
  const { x, y } = browserCanvasXY(e);
  ws.send(JSON.stringify({ type, x, y }));
}

function browserKey(e) {
  const ws = state.browserWs;
  if (!ws || ws.readyState !== 1 || !$("browser-input").checked) return;
  if (["INPUT", "TEXTAREA", "SELECT"].includes(document.activeElement?.tagName)) return;
  const map = { ArrowUp: "ArrowUp", ArrowDown: "ArrowDown", ArrowLeft: "ArrowLeft",
                ArrowRight: "ArrowRight", Enter: "Enter", Backspace: "Backspace",
                Tab: "Tab", Escape: "Escape" };
  if (map[e.key]) { e.preventDefault(); ws.send(JSON.stringify({ type: "key", key: e.key })); }
  else if (e.key.length === 1) { e.preventDefault(); ws.send(JSON.stringify({ type: "type", text: e.key })); }
}

async function browserCloseSession() {
  if (!state.chatId) return;
  try { await api("/api/browser/close", { method: "POST", body: { chat_id: state.chatId } }); }
  catch (e) { setError(e.message); }
  browserDisconnect();
  const canvas = $("browser-canvas");
  canvas.getContext("2d").clearRect(0, 0, canvas.width, canvas.height);
}

/* ================================================================== Phase 4 — notifications + sounds (§10) */

function onNotification(ev) {
  const d = ev.data || {};
  showToast(d.text || "уведомление", d.kind);
  if (d.sound === "task_done") playSound("task");
}

function showToast(text, kind) {
  const box = $("toasts");
  if (!box) return;
  const t = div("toast" + (kind ? ` ${kind}` : ""));
  t.textContent = text;
  box.appendChild(t);
  setTimeout(() => { t.classList.add("gone"); setTimeout(() => t.remove(), 400); }, 6000);
}

const _audioCtx = { ctx: null };
function playSound(kind) {
  const s = state.settings || {};
  if (s.sounds_enabled in { "0": 1, false: 1, off: 1 }) return;
  const enabledKey = kind === "turn" ? "sound_turn_done" : "sound_task_done";
  if (s[enabledKey] in { "0": 1, false: 1, off: 1 }) return;
  const fileKey = kind === "turn" ? "sound_file_turn" : "sound_file_task";
  const file = s[fileKey];
  if (file) {
    try { new Audio(`/api/sounds/${encodeURIComponent(file)}`).play().catch(() => {}); }
    catch { /* autoplay blocked */ }
    return;
  }
  playBlip(kind === "turn" ? 660 : 880);
}

function playBlip(freq) {
  try {
    if (!_audioCtx.ctx) _audioCtx.ctx = new (window.AudioContext || window.webkitAudioContext)();
    const ctx = _audioCtx.ctx;
    const osc = ctx.createOscillator();
    const gain = ctx.createGain();
    osc.type = "sine";
    osc.frequency.value = freq;
    gain.gain.setValueAtTime(0.0001, ctx.currentTime);
    gain.gain.exponentialRampToValueAtTime(0.12, ctx.currentTime + 0.02);
    gain.gain.exponentialRampToValueAtTime(0.0001, ctx.currentTime + 0.25);
    osc.connect(gain).connect(ctx.destination);
    osc.start();
    osc.stop(ctx.currentTime + 0.3);
  } catch { /* no audio — silent */ }
}

/* ================================================================== Phase 4 — command palette (§6/§16) */

function paletteActions() {
  const acts = [
    { section: "Действия", label: "＋ Новый чат", run: newChat },
    { section: "Действия", label: "Настройки", run: openSettings },
    { section: "Действия", label: "▤ Рабочая зона: Файлы", run: () => openPanel("files") },
    { section: "Действия", label: "▤ Рабочая зона: Git", run: () => openPanel("git") },
    { section: "Действия", label: "▤ Рабочая зона: Терминал", run: () => openPanel("terminal") },
    { section: "Действия", label: "▤ Рабочая зона: Браузер", run: () => openPanel("browser") },
    { section: "Действия", label: "⇲ Compact — сжать контекст", run: compactNow },
    { section: "Действия", label: "Очистить контекст", run: clearContext },
    { section: "Действия", label: "⎇ План-режим (следующее сообщение)", run: () => setPlanMode(true) },
    { section: "Действия", label: "↻ Продолжить генерацию", run: continueGeneration },
    { section: "Действия", label: "⌗ RAW — весь поток как есть", run: showRaw },
    { section: "Действия", label: "⑂ Форк чата", run: () => forkChat() },
  ];
  if (state.chatId) {
    acts.push({ section: "Действия", label: "Остановить генерацию", run: stopGeneration });
  }
  for (const m of state.macros) {
    acts.push({
      section: "Макросы",
      label: `⌘ ${m.name}${m.hotkey ? ` (${m.hotkey})` : ""}`,
      run: () => runMacro(m),
    });
  }
  for (const c of state.chats.slice(0, 20)) {
    acts.push({ section: "Чаты", label: c.title, run: () => openChat(c.id) });
  }
  return acts;
}

async function forkChat() {
  if (!state.chatId) return;
  try {
    const { chat } = await api(`/api/chats/${state.chatId}/fork`, { method: "POST", body: {} });
    await loadChats();
    openChat(chat.id);
  } catch (e) { setError(e.message); }
}

function openPalette() {
  $("palette").classList.remove("hidden");
  $("palette-input").value = "";
  renderPalette("");
  $("palette-input").focus();
}

function closePalette() {
  $("palette").classList.add("hidden");
  $("palette-input").blur();
}

function renderPalette(q) {
  const all = paletteActions();
  const query = (q || "").trim().toLowerCase();
  const items = query
    ? all.filter((a) => a.label.toLowerCase().includes(query)
        || a.section.toLowerCase().includes(query))
    : all;
  state.paletteItems = items;
  state.paletteIndex = Math.min(state.paletteIndex, Math.max(items.length - 1, 0));
  const list = $("palette-list");
  list.innerHTML = "";
  let lastSection = "";
  items.forEach((item, i) => {
    if (item.section !== lastSection) {
      lastSection = item.section;
      const h = div("palette-section");
      h.textContent = item.section;
      list.appendChild(h);
    }
    const row = div("palette-item" + (i === state.paletteIndex ? " active" : ""));
    row.tabIndex = -1;
    row.textContent = item.label;
    row.onclick = () => { closePalette(); item.run(); };
    list.appendChild(row);
  });
  if (!items.length) {
    list.innerHTML = `<div class="palette-empty">ничего не найдено</div>`;
  }
}

function paletteKey(e) {
  const items = state.paletteItems;
  if (e.key === "ArrowDown") {
    e.preventDefault();
    state.paletteIndex = Math.min(state.paletteIndex + 1, items.length - 1);
    renderPalette($("palette-input").value);
  } else if (e.key === "ArrowUp") {
    e.preventDefault();
    state.paletteIndex = Math.max(state.paletteIndex - 1, 0);
    renderPalette($("palette-input").value);
  } else if (e.key === "Enter") {
    e.preventDefault();
    const item = items[state.paletteIndex];
    if (item) { closePalette(); item.run(); }
  } else if (e.key === "Escape") {
    e.preventDefault();
    closePalette();
  }
}

/* ================================================================== Phase 4 — macros (§6) */

async function loadMacros() {
  try {
    const { macros } = await api("/api/macros");
    state.macros = macros;
  } catch { state.macros = []; }
}

function renderMacroList() {
  const list = $("macro-list");
  if (!list) return;
  list.innerHTML = "";
  if (!state.macros.length) {
    list.innerHTML = `<p class="hint">Макросов нет. Появятся в палитре Ctrl+K и по хоткею.</p>`;
    return;
  }
  for (const m of state.macros) {
    const row = div("macro-row");
    row.innerHTML = `<div class="macro-name">${escapeHtml(m.name)}</div>
      <div class="macro-meta">${m.mode === "run" ? "выполнить сразу" : "вставить в ввод"}${m.hotkey ? ` · ${escapeHtml(m.hotkey)}` : ""}</div>
      <pre class="macro-prompt"></pre>`;
    row.querySelector(".macro-prompt").textContent = m.prompt;
    const del = document.createElement("button");
    del.className = "btn-danger-sm";
    del.textContent = "—";
    del.title = "удалить макрос";
    del.onclick = async () => {
      await api(`/api/macros/${m.id}`, { method: "DELETE" });
      await loadMacros();
      renderMacroList();
    };
    row.appendChild(del);
    list.appendChild(row);
  }
}

async function onAddMacro(e) {
  e.preventDefault();
  const name = $("mf-name").value.trim();
  const prompt = $("mf-prompt").value.trim();
  if (!name || !prompt) return;
  try {
    await api("/api/macros", { method: "POST", body: {
      name, prompt, mode: $("mf-mode").value, hotkey: $("mf-hotkey").value.trim() || null } });
    $("mf-name").value = ""; $("mf-prompt").value = ""; $("mf-hotkey").value = "";
    await loadMacros();
    renderMacroList();
  } catch (err) { setError("Макрос: " + err.message); }
}

function runMacro(m) {
  if (!m || !state.chatId) return;
  if (m.mode === "run") {
    api(`/api/chats/${state.chatId}/messages`, { method: "POST", body: { text: m.prompt } })
      .then(() => { setGenerating(true); setError(""); })
      .catch((e) => setError(e.message));
  } else {
    $("input").value = m.prompt;
    autoGrow();
    $("input").focus();
  }
}

function matchHotkey(e) {
  for (const m of state.macros) {
    if (!m.hotkey) continue;
    const parts = m.hotkey.toLowerCase().split("+").map((s) => s.trim());
    const key = parts[parts.length - 1];
    const needCtrl = parts.includes("ctrl") || parts.includes("cmd") || parts.includes("meta");
    const needShift = parts.includes("shift");
    const needAlt = parts.includes("alt");
    const pressed = e.key.toLowerCase();
    const keyOk = key.length === 1 ? pressed === key
      : pressed === key || e.code.toLowerCase() === `digit${key}`;
    if (keyOk && needCtrl === (e.ctrlKey || e.metaKey)
        && needShift === e.shiftKey && needAlt === e.altKey) {
      e.preventDefault();
      runMacro(m);
      return true;
    }
  }
  return false;
}

/* ================================================================== Phase 4 — sounds settings (§10) */

async function loadSoundsSettings() {
  const s = await api("/api/settings");
  state.settings = s;
  $("sounds-enabled").checked = !(s.sounds_enabled in { "0": 1, false: 1, off: 1 });
  $("sound-turn").checked = !(s.sound_turn_done in { "0": 1, false: 1, off: 1 });
  $("sound-task").checked = !(s.sound_task_done in { "0": 1, false: 1, off: 1 });
  try {
    const { sounds } = await api("/api/sounds");
    for (const selId of ["sound-file-turn", "sound-file-task"]) {
      const sel = $(selId);
      const current = selId === "sound-file-turn" ? s.sound_file_turn : s.sound_file_task;
      sel.innerHTML = `<option value="">— встроенный —</option>` +
        sounds.map((f) => `<option value="${escapeHtml(f.name)}" ${f.name === current ? "selected" : ""}>${escapeHtml(f.name)}</option>`).join("");
    }
  } catch { /* sounds dir empty */ }
}

async function saveSoundsSettings() {
  try {
    await api("/api/settings", { method: "PATCH", body: {
      sounds_enabled: $("sounds-enabled").checked ? "1" : "0",
      sound_turn_done: $("sound-turn").checked ? "1" : "0",
      sound_task_done: $("sound-task").checked ? "1" : "0",
      sound_file_turn: $("sound-file-turn").value || "",
      sound_file_task: $("sound-file-task").value || "",
    } });
    state.settings = await api("/api/settings");
    setError("");
  } catch (e) { setError(e.message); }
}

async function onSoundUpload(e) {
  e.preventDefault();
  const input = $("sound-file");
  const files = [...(input.files || [])];
  if (!files.length) return;
  const fd = new FormData();
  for (const f of files) fd.append("file", f, f.name);
  try {
    const res = await fetch("/api/sounds", { method: "POST", body: fd });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data?.error || `HTTP ${res.status}`);
    input.value = "";
    await loadSoundsSettings();
  } catch (err) { setError("Звук: " + err.message); }
}

/* ================================================================== Phase 4 — web settings (§12/§17) */

async function loadWebSettings() {
  const s = await api("/api/settings");
  state.settings = s;
  $("search-backend").value = s.search_backend || "off";
  $("searxng-url").value = s.search_searxng_url || "";
  $("brave-key").value = s.search_brave_key || "";
  $("cache-ttl").value = s.web_cache_ttl_hours || 24;
  $("cache-cap").value = s.web_cache_max_mb || 50;
  let matrix = {};
  try { matrix = JSON.parse(s.proxy_matrix || "{}"); } catch { matrix = {}; }
  $("proxy-global").value = matrix.global?.url || "";
  $("proxy-model").value = matrix.model_api?.url || "";
  $("proxy-web").value = matrix.web?.url || "";
  $("proxy-websearch").value = matrix.websearch?.url || "";
  $("proxy-webfetch").value = matrix.webfetch?.url || "";
  $("proxy-browser").value = matrix.browser?.url || "";
  renderProxyNote();
}

function renderProxyNote() {
  const note = $("proxy-note");
  if (!note) return;
  const describe = { global: "global", model_api: "model_api", web: "web",
    websearch: "websearch", webfetch: "webfetch", browser: "browser" };
  const parts = [];
  for (const [route, id] of Object.entries(describe)) {
    const val = $(`proxy-${id}`).value.trim();
    if (val) parts.push(`${route}: ${val.replace(/\/\/[^:/@]+:[^@]+@/, "//***@")}`);
  }
  note.textContent = parts.length ? `Активны: ${parts.join(" · ")}` : "Прокси не настроены — прямые соединения.";
}

async function saveProxyMatrix() {
  try {
    const matrix = {};
    const put = (route, val) => { const v = val.trim(); if (v) matrix[route] = { url: v }; };
    put("global", $("proxy-global").value);
    put("model_api", $("proxy-model").value);
    put("web", $("proxy-web").value);
    put("websearch", $("proxy-websearch").value);
    put("webfetch", $("proxy-webfetch").value);
    put("browser", $("proxy-browser").value);
    await api("/api/settings", { method: "PATCH", body: {
      proxy_matrix: matrix } });
    setError("");
    renderProxyNote();
    const btn = $("btn-save-proxy");
    if (btn) {
      btn.textContent = "Сохранено ✓";
      setTimeout(() => { btn.textContent = "Сохранить"; }, 1500);
    }
  } catch (e) { setError(e.message); }
}

async function saveWebSettings() {
  try {
    await api("/api/settings", { method: "PATCH", body: {
      search_backend: $("search-backend").value,
      search_searxng_url: $("searxng-url").value.trim(),
      search_brave_key: $("brave-key").value.trim(),
      web_cache_ttl_hours: parseInt($("cache-ttl").value, 10) || 24,
      web_cache_max_mb: parseInt($("cache-cap").value, 10) || 50,
    } });
    state.settings = await api("/api/settings");
    renderProxyNote();
    setError("");
    $("btn-save-web").textContent = "Сохранено ✓";
    setTimeout(() => { $("btn-save-web").textContent = "Сохранить"; }, 1500);
  } catch (e) { setError(e.message); }
}

async function runWebtoolsDiagnostics() {
  const box = $("webtools-report");
  box.innerHTML = `<p class="hint">проверяю…</p>`;
  try {
    const st = await api("/api/webtools");
    const rows = [
      `поиск: ${st.search_backend}` +
        (st.search_backend === "searxng" ? ` (${st.searxng_url || "URL не задан"})` : "") +
        (st.search_backend === "brave" ? ` (ключ ${st.brave_key_set ? "задан" : "не задан"})` : ""),
      `кеш: ${st.cache.entries} записей, ${st.cache.mb} МБ, попаданий ${st.cache.hits}`,
      `браузер: ${st.browser.playwright ? "playwright доступен" : "playwright НЕ установлен"}` +
        (st.browser.sessions.length ? ` · сессий: ${st.browser.sessions.length}` : ""),
      `прокси: ${Object.entries(st.proxy).filter(([, r]) => r.active)
        .map(([route, r]) => `${route}→${r.scheme}${r.source !== route ? ` (из ${r.source})` : ""}`)
        .join(", ") || "нет — прямые соединения"}`,
    ];
    box.innerHTML = rows.map((r) => `<p class="hint">• ${escapeHtml(r)}</p>`).join("");
  } catch (e) { box.innerHTML = `<p class="hint">ошибка: ${escapeHtml(e.message)}</p>`; }
}

async function clearWebCache() {
  try {
    const res = await api("/api/webtools/cache/clear", { method: "POST" });
    setError(`кеш очищен (${res.cache.entries} записей осталось)`);
  } catch (e) { setError(e.message); }
}

/* ================================================================== Phase 4 — session search (§12) */

let searchDebounce = null;
function onSessionSearch(e) {
  const q = e.target.value.trim();
  clearTimeout(searchDebounce);
  if (q.length < 2) { $("search-results").classList.add("hidden"); return; }
  searchDebounce = setTimeout(() => runSessionSearch(q), 250);
}

async function runSessionSearch(q) {
  try {
    const { results } = await api(`/api/search?q=${encodeURIComponent(q)}`);
    const box = $("search-results");
    box.innerHTML = "";
    if (!results.length) {
      box.innerHTML = `<div class="search-empty">ничего не найдено</div>`;
    }
    for (const r of results.slice(0, 12)) {
      const row = div("search-row");
      row.tabIndex = 0;
      row.innerHTML = `<div class="search-title"></div><div class="search-snippet"></div>`;
      row.querySelector(".search-title").textContent =
        `${r.title} · ${r.type === "user_message" ? "вы" : "AI"}`;
      row.querySelector(".search-snippet").textContent = r.snippet || "";
      const open = () => { box.classList.add("hidden"); openChat(r.chat_id); };
      row.onclick = open;
      row.onkeydown = (ev) => { if (ev.key === " " || ev.key === "Enter") { ev.preventDefault(); open(); } };
      box.appendChild(row);
    }
    box.classList.remove("hidden");
  } catch { /* search unavailable */ }
}

/* ------------------------------------------------------------------ utils */

function div(cls) { const d = document.createElement("div"); d.className = cls; return d; }
function span(cls, text) { const s = document.createElement("span"); s.className = cls; s.textContent = text; return s; }
function escapeHtml(s) { return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])); }

function autoGrow() {
  const t = $("input");
  t.style.height = "auto";
  t.style.height = Math.min(t.scrollHeight, 160) + "px";
}

/* ------------------------------------------------------------------ wire up */

function init() {
  $("btn-new-chat").onclick = newChat;
  $("btn-settings").onclick = openSettings;
  $("btn-close-settings").onclick = closeSettings;
  $("settings-modal").addEventListener("click", (e) => {
    if (e.target === $("settings-modal")) closeSettings();
  });
  document.querySelectorAll(".tab").forEach((t) => {
    t.onclick = () => switchTab(t.dataset.tab);
  });
  $("provider-form").addEventListener("submit", onAddProvider);
  $("btn-diagnostics").onclick = runDiagnostics;
  $("mcp-form").addEventListener("submit", onAddMcp);
  $("btn-chain-add").onclick = () => { state.chain.push(""); renderChain(); };
  $("btn-save-reliability").onclick = saveReliability;
  $("btn-save-system").onclick = saveChatSettings;
  $("send-think").onchange = saveChatSettings;

  // phase 2: permissions tab + right working zone
  $("rule-form").addEventListener("submit", onAddRule);
  $("btn-save-permissions").onclick = savePermissions;
  $("btn-panel-toggle").onclick = togglePanel;
  document.querySelectorAll(".panel-tab").forEach((t) => {
    t.onclick = () => openPanel(t.dataset.ptab);
  });
  $("btn-file-close").onclick = () => $("file-viewer").classList.add("hidden");
  $("btn-tree-refresh").onclick = async () => {
    $("file-tree").dataset.loaded = "";
    await loadDir("");
    $("file-tree").dataset.loaded = "1";
  };
  $("btn-cp-refresh").onclick = refreshCheckpoints;
  $("btn-cp-manual").onclick = createManualCheckpoint;
  $("btn-mr-close").onclick = () => $("mr-modal").classList.add("hidden");
  $("mr-modal").addEventListener("click", (e) => {
    if (e.target === $("mr-modal")) $("mr-modal").classList.add("hidden");
  });
  $("btn-mr-apply").onclick = () => applyRevert(false);
  $("btn-mr-force").onclick = () => applyRevert(true);
  $("btn-mr-cancel").onclick = () => $("mr-modal").classList.add("hidden");

  // phase 3: attachments / plan mode / context control
  $("btn-attach").onclick = pickFiles;
  $("file-input").addEventListener("change", onFilesPicked);
  $("btn-plan").onclick = () => setPlanMode(!state.planMode);
  $("btn-undo").onclick = hideUndoToast;
  $("btn-raw-close").onclick = () => $("raw-modal").classList.add("hidden");
  $("raw-modal").addEventListener("click", (e) => {
    if (e.target === $("raw-modal")) $("raw-modal").classList.add("hidden");
  });
  $("btn-versions-close").onclick = () => $("versions-modal").classList.add("hidden");
  $("versions-modal").addEventListener("click", (e) => {
    if (e.target === $("versions-modal")) $("versions-modal").classList.add("hidden");
  });
  $("btn-save-context").onclick = saveContextSettings;
  $("btn-compact-now").onclick = compactNow;
  $("btn-clear-context").onclick = clearContext;

  $("btn-send").onclick = send;
  $("btn-stop").onclick = stopGeneration;
  $("input").addEventListener("input", autoGrow);
  $("input").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); }
  });

  $("model-chip").onclick = (e) => {
    e.stopPropagation();
    if ($("model-pop").classList.contains("hidden")) openModelPop();
    else closeModelPop();
  };
  // unknown-model modal + browser engines
  $("btn-caps-close").onclick = closeCapsModal;
  $("caps-modal").addEventListener("click", (e) => {
    if (e.target === $("caps-modal")) closeCapsModal();
  });
  $("btn-caps-probe").onclick = capsProbe;
  $("btn-caps-save").onclick = capsSaveAndSend;
  $("btn-caps-skip").onclick = capsSkip;
  $("btn-browser-modal-close").onclick = () => {
    $("browser-modal").classList.add("hidden");
    if (enginesPoll) { clearInterval(enginesPoll); enginesPoll = null; }
  };
  $("btn-engines-refresh").onclick = loadBrowserEngines;

  // files+git splitter (drag to resize) + sidebar/work-panel dividers
  initPaneSplit();
  initVSplits();

  // Phase 4: command palette / macros / web / terminal / browser / search
  $("palette-input").addEventListener("input", (e) => renderPalette(e.target.value));
  $("palette-input").addEventListener("keydown", paletteKey);
  $("palette").addEventListener("click", (e) => {
    if (e.target === $("palette")) closePalette();
  });
  $("macro-form").addEventListener("submit", onAddMacro);
  $("sound-upload-form").addEventListener("submit", onSoundUpload);
  $("btn-save-macros").onclick = saveSoundsSettings;
  $("btn-save-web").onclick = saveWebSettings;
  $("btn-webtools-test").onclick = runWebtoolsDiagnostics;
  $("btn-cache-clear").onclick = clearWebCache;
  for (const id of ["proxy-global", "proxy-model", "proxy-web", "proxy-websearch",
    "proxy-webfetch", "proxy-browser"]) {
    $(id).addEventListener("input", renderProxyNote);
  }
  if ($("btn-save-proxy")) $("btn-save-proxy").onclick = saveProxyMatrix;
  initFileDrop("import-file", "Перетащите экспорт .json / .md сюда — или кликните, чтобы выбрать файл");
  initFileDrop("skill-file", "SKILL.md — перетащите или кликните, чтобы выбрать");
  initFileDrop("sound-file", ".mp3 / .wav / .ogg — перетащите или кликните");
  initFileDrop("theme-file", "Тему .css — перетащите или кликните");
  $("btn-term-clear").onclick = () => {
    state.termBlocks.forEach((b) => b.el.remove());
    termReset();
  };
  $("btn-panel-close").onclick = togglePanel;
  $("btn-browser-back").onclick = () => browserNav("back");
  $("btn-browser-fwd").onclick = () => browserNav("forward");
  $("btn-browser-reload").onclick = () => browserNav("reload");
  $("btn-browser-go").onclick = () => browserGo($("browser-url").value.trim());
  $("browser-url").addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); browserGo(e.target.value.trim()); }
  });
  $("btn-browser-close").onclick = browserCloseSession;
  const canvas = $("browser-canvas");
  canvas.addEventListener("click", (e) => browserForward(e, "click"));
  canvas.addEventListener("dblclick", (e) => browserForward(e, "dblclick"));
  canvas.addEventListener("wheel", (e) => {
    if (state.browserWs && $("browser-input").checked) {
      e.preventDefault();
      state.browserWs.send(JSON.stringify({ type: "scroll", dx: Math.round(e.deltaX), dy: Math.round(e.deltaY) }));
    }
  }, { passive: false });
  $("session-search").addEventListener("input", onSessionSearch);
  document.addEventListener("click", (e) => {
    if (!e.target.closest(".sidebar-search")) $("search-results")?.classList.add("hidden");
    if (!e.target.closest(".model-picker")) closeModelPop();
  });

  // command palette: Ctrl+K (Win) / Cmd+K (Mac) — the slash-command killer (§6)
  document.addEventListener("keydown", (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "k") {
      e.preventDefault();
      if ($("palette").classList.contains("hidden")) openPalette();
      else closePalette();
      return;
    }
    if (matchHotkey(e)) return;
    if ($("palette").classList.contains("hidden")) browserKey(e);
    if (e.key === "Escape") {
      closeModelPop();
      closePalette();
      $("raw-modal").classList.add("hidden");
      $("versions-modal").classList.add("hidden");
      $("caps-modal").classList.add("hidden");
      $("browser-modal").classList.add("hidden");
      $("search-results").classList.add("hidden");
    }
  });

  loadProvidersAndModels().then(loadChats).then(loadMacros).then(() => {
    if (state.chats.length) openChat(state.chats[0].id);
  });
  api("/api/settings").then((s) => { state.settings = s; }).catch(() => {});
}

/* ================================================================ Phase 5
   Modes & deployment: i18n (§16), login gate (§2), onboarding wizard (§16),
   user chip, skills (§6), hooks (§7), appearance/themes (§16), logs (§16),
   export/import round-trip (§16), shared badges + queue chips (§2). */

/* ------------------------------------------------------------------ i18n */

const I18N = {
  ru: {
    settings: "Настройки", selectChat: "Выберите чат",
    workZone: "Рабочая зона (файлы · гит · терминал · браузер)",
    newChat: "Новый чат", searchChats: "Поиск по чатам…",
    searchChatsT: "Полнотекстовый поиск по сессиям",
    emptyTitle: "Новый чат",
    emptySub: "Напишите сообщение, чтобы начать. Модель выбирается справа снизу.",
    attach: "Прикрепить файл",
    planMode: "План-режим: агент сначала предложит план на одобрение",
    plan: "План", inputPh: "Сообщение… (Enter — отправить, Shift+Enter — перенос)",
    send: "Отправить", stop: "Остановить генерацию", chatContext: "Контекст чата",
    context: "контекст —", provider: "Провайдер", model: "Модель",
    tabFiles: "Файлы", tabGit: "Git", tabTerminal: "Терминал", tabBrowser: "Браузер",
    tProviders: "Провайдеры", tCatalog: "Каталог", tMcp: "MCP", tRights: "Права",
    tReliability: "Надёжность", tContext: "Контекст", tWeb: "Веб",
    tMacros: "Макросы и звуки", tSkills: "Скилы", tHooks: "Хуки",
    tAppearance: "Внешний вид", tLogs: "Логи", tExport: "Экспорт", tChat: "Чат",
    skillsH: "Скилы", tBrowser: "Браузер", tProxy: "Прокси",
    skillsHint: "SkillSearch-паттерн: модель видит названия и краткие описания, тело подгружается тулзой skill_load. Формат SKILL.md — frontmatter (name, description) + markdown-тело.",
    skillName: "Имя скила", skillDesc: "Краткое описание (для skill_search)",
    skillBody: "Markdown-тело: инструкции для модели…", save: "Сохранить",
    importSkillMd: "Импортировать SKILL.md", hooksH: "Хуки",
    hooksHint: "Событие → команда. Команда получает JSON на stdin, запускается в рабочей папке с таймаутом. PreToolUse и UserPromptSubmit могут блокировать: stdout {\"decision\":\"block\",\"reason\":\"…\"}.",
    hookCmd: "Команда (например: python fmt.py)", add: "Добавить",
    appearanceH: "Внешний вид", lang: "Язык", theme: "Тема",
    themeAuto: "авто (по системе)", themeLight: "светлая", themeDark: "тёмная",
    customCss: "Кастомный CSS", none: "— нет —", uploadCss: "Загрузить CSS",
    exampleCss: "Пример со комментариями", logsH: "Логи сервера",
    minLevel: "Мин. уровень", refresh: "Обновить", exportForSupport: "Экспорт для саппорта",
    clear: "Очистить", exportH: "Экспорт / импорт",
    exportHint: "JSON — полный фиделити: все события, правки, мутации; импорт восстанавливает роллаут один в один. MD — читаемый транскрипт нашего формата: переносится в другой чат, мутации не переносятся.",
    expJson: "Экспорт чата — JSON", expMd: "Экспорт чата — Markdown",
    impH: "Импорт", impPrefix: "Префикс заголовка (необязательно)",
    importChat: "Импортировать", login: "Войти",
    adminLink: "Администрация → /admin",
    onbTitle: "Добро пожаловать — настроим сервер", onbHead: "Первый запуск",
    sharedTag: "общий", sharedMine: "общий · ваш",
    queuePos: (n) => `в очереди (позиция ${n}) — ждём слот генерации`,
    gateSub: "войдите, чтобы продолжить", gateUser: "Имя пользователя",
    gatePass: "Пароль", gateTeam: "режим «команда»: общий сервер, свои сессии",
    gateAccounts: "режим «аккаунты»: изолированные папки и ключи",
    role: { admin: "админ", member: "участник" },
    logout: "выйти", mode: "режим", error: "ошибка", ok: "готово",
  },
  en: {
    settings: "Settings", selectChat: "Select a chat",
    workZone: "Working zone (files · git · terminal · browser)",
    newChat: "New chat", searchChats: "Search chats…",
    searchChatsT: "Full-text search over your sessions",
    emptyTitle: "New chat",
    emptySub: "Write a message to start. The model is picked bottom-right.",
    attach: "Attach a file",
    planMode: "Plan mode: the agent proposes a plan for approval first",
    plan: "Plan", inputPh: "Message… (Enter — send, Shift+Enter — newline)",
    send: "Send", stop: "Stop generation", chatContext: "Chat context",
    context: "context —", provider: "Provider", model: "Model",
    tabFiles: "Files", tabGit: "Git", tabTerminal: "Terminal", tabBrowser: "Browser",
    tProviders: "Providers", tCatalog: "Catalog", tMcp: "MCP", tRights: "Permissions",
    tReliability: "Reliability", tContext: "Context", tWeb: "Web",
    tMacros: "Macros & sounds", tSkills: "Skills", tHooks: "Hooks",
    tAppearance: "Appearance", tLogs: "Logs", tExport: "Export", tChat: "Chat",
    skillsH: "Skills", tBrowser: "Browser", tProxy: "Proxy",
    skillsHint: "SkillSearch pattern: the model sees names + short descriptions, the body loads via skill_load. SKILL.md format — frontmatter (name, description) + markdown body.",
    skillName: "Skill name", skillDesc: "Short description (for skill_search)",
    skillBody: "Markdown body: instructions for the model…", save: "Save",
    importSkillMd: "Import SKILL.md", hooksH: "Hooks",
    hooksHint: "Event → command. The command receives JSON on stdin, runs in the workspace with a timeout. PreToolUse and UserPromptSubmit may block: stdout {\"decision\":\"block\",\"reason\":\"…\"}.",
    hookCmd: "Command (e.g. python fmt.py)", add: "Add",
    appearanceH: "Appearance", lang: "Language", theme: "Theme",
    themeAuto: "auto (system)", themeLight: "light", themeDark: "dark",
    customCss: "Custom CSS", none: "— none —", uploadCss: "Upload CSS",
    exampleCss: "Commented example", logsH: "Server logs",
    minLevel: "Min level", refresh: "Refresh", exportForSupport: "Export for support",
    clear: "Clear", exportH: "Export / import",
    exportHint: "JSON — full fidelity: every event, edit, mutation; import restores the rollout one-to-one. MD — a readable transcript of our own format: moves to another chat, mutations do not transfer.",
    expJson: "Export chat — JSON", expMd: "Export chat — Markdown",
    impH: "Import", impPrefix: "Title prefix (optional)",
    importChat: "Import", login: "Log in",
    adminLink: "Administration → /admin",
    onbTitle: "Welcome — let's set the server up", onbHead: "First run",
    sharedTag: "shared", sharedMine: "shared · yours",
    queuePos: (n) => `queued (position ${n}) — waiting for a generation slot`,
    gateSub: "log in to continue", gateUser: "Username",
    gatePass: "Password", gateTeam: "team mode: shared server, own sessions",
    gateAccounts: "accounts mode: isolated folders and keys",
    role: { admin: "admin", member: "member" },
    logout: "log out", mode: "mode", error: "error", ok: "done",
  },
};

function t(key) {
  const v = (I18N[state.lang] || I18N.ru)[key];
  return typeof v === "function" ? v : (v ?? key);
}

function applyI18n() {
  document.documentElement.lang = state.lang === "en" ? "en" : "ru";
  document.querySelectorAll("[data-i18n]").forEach((el) => { el.textContent = t(el.dataset.i18n); });
  document.querySelectorAll("[data-i18n-ph]").forEach((el) => { el.placeholder = t(el.dataset.i18nPh); });
  document.querySelectorAll("[data-i18n-title]").forEach((el) => { el.title = t(el.dataset.i18nTitle); });
}

function applyTheme(theme, customCss) {
  document.documentElement.dataset.theme = theme || "auto";
  const old = document.getElementById("custom-css-link");
  if (old) old.remove();
  if (customCss) {
    const link = document.createElement("link");
    link.id = "custom-css-link";
    link.rel = "stylesheet";
    link.href = `/themes/${encodeURIComponent(customCss)}`;
    document.head.appendChild(link);
  }
}

/* ------------------------------------------------------------------ §2 queue chip */

function showQueueChip(position) {
  let chip = document.getElementById("queue-chip-live");
  if (!chip) {
    chip = document.createElement("div");
    chip.id = "queue-chip-live";
    chip.className = "queue-chip";
    $("messages").appendChild(chip);
  }
  chip.innerHTML = `<span class="spin">${ico("refresh")}</span><span>${escapeHtml(t("queuePos")(position))}</span>`;
}

function hideQueueChip() {
  document.getElementById("queue-chip-live")?.remove();
}

/* ------------------------------------------------------------------ §2 login gate */

function showGate() {
  const mode = state.auth.mode;
  if (mode === "accounts" || mode === "team") {
    $("gate-overlay").classList.remove("hidden");
    $("gate-sub").textContent = mode === "accounts" ? t("gateAccounts") : t("gateTeam");
  }
}

async function doLogin() {
  const username = $("gate-user").value.trim();
  const password = $("gate-pass").value;
  $("gate-error").textContent = "";
  try {
    await api("/api/auth/login", { method: "POST", body: { username, password } });
    location.reload();
  } catch (e) {
    $("gate-error").textContent = e.message;
  }
}

/* ------------------------------------------------------------------ user chip */

function renderUserChip() {
  const chip = $("user-chip");
  const u = state.auth.user;
  if (!u) { chip.classList.add("hidden"); return; }
  chip.classList.remove("hidden");
  chip.innerHTML = "";
  const av = document.createElement("div");
  av.className = "avatar";
  av.textContent = u.username.slice(0, 1).toUpperCase();
  const who = document.createElement("div");
  who.className = "who";
  who.innerHTML = `${escapeHtml(u.username)}<small>${escapeHtml(t("role")[u.role] || u.role)}</small>`;
  chip.appendChild(av);
  chip.appendChild(who);
  const out = document.createElement("a");
  out.href = "#";
  out.textContent = t("logout");
  out.onclick = async (e) => {
    e.preventDefault();
    await api("/api/auth/logout", { method: "POST" }).catch(() => {});
    location.reload();
  };
  chip.appendChild(out);
  if (u.role === "admin") {
    const adm = document.createElement("a");
    adm.href = "/admin";
    adm.textContent = "/admin";
    adm.style.marginRight = "6px";
    chip.appendChild(adm);
  }
}

/* ------------------------------------------------------------------ §16 onboarding wizard */

const ONB = { step: 1, mode: "solo", workspace: "" };

function onbShow(step) {
  ONB.step = step;
  $("onb-overlay").classList.remove("hidden");
  document.querySelectorAll(".onb-step").forEach((s) => {
    const n = +s.dataset.step;
    s.classList.toggle("active", n === step);
    s.classList.toggle("done", n < step);
  });
  const b = $("onb-body");
  if (step === 1) {
    b.innerHTML = `
      <div class="onb-ring">${ico("logo")}</div>
      <div class="onb-h">${escapeHtml(t("onbTitle"))}</div>
      <div class="onb-sub">Первый зарегистрированный становится владельцем сервера (админом). Пароль нужен только для /admin — соло-режим не спрашивает входа.</div>
      <input id="onb-user" type="text" placeholder="${escapeHtml(t("gateUser"))}">
      <input id="onb-pass" type="password" placeholder="${escapeHtml(t("gatePass"))}">
      <div class="gate-error" id="onb-error"></div>
      <div class="onb-actions">
        <span></span>
        <button class="btn-primary" id="onb-next-1">${escapeHtml(t("add"))} →</button>
      </div>`;
    $("onb-next-1").onclick = async () => {
      try {
        await api("/api/onboarding/register", {
          method: "POST",
          body: { username: $("onb-user").value.trim(), password: $("onb-pass").value },
        });
        onbShow(2);
      } catch (e) { $("onb-error").textContent = e.message; }
    };
  } else if (step === 2) {
    b.innerHTML = `
      <div class="onb-h">Режим сервера</div>
      <div class="onb-sub">Одна кодовая база — три профиля запуска; сменить можно позже на /admin</div>
      <button class="onb-choice ${ONB.mode === "solo" ? "selected" : ""}" data-mode="solo"><span class="onb-radio"></span>
        <div class="onb-txt"><b>Соло</b><span>без входа; один юзер; общий workspace и ключи в main.db</span></div></button>
      <button class="onb-choice ${ONB.mode === "team" ? "selected" : ""}" data-mode="team"><span class="onb-radio"></span>
        <div class="onb-txt"><b>Команда</b><span>вход по аккаунтам; общие провайдеры и shared-проекты; лимиты конкурентности</span></div></button>
      <button class="onb-choice ${ONB.mode === "accounts" ? "selected" : ""}" data-mode="accounts"><span class="onb-radio"></span>
        <div class="onb-txt"><b>Аккаунты</b><span>личные чаты/ключи/skills в &lt;user&gt;.db, изолированная папка, ключи шифруются паролем</span></div></button>
      <div class="onb-actions">
        <button class="btn-secondary" id="onb-back-2">← Назад</button>
        <button class="btn-primary" id="onb-next-2">Далее →</button>
      </div>`;
    b.querySelectorAll(".onb-choice").forEach((c) => {
      c.onclick = () => {
        ONB.mode = c.dataset.mode;
        b.querySelectorAll(".onb-choice").forEach((x) => x.classList.toggle("selected", x === c));
      };
    });
    $("onb-back-2").onclick = () => onbShow(1);
    $("onb-next-2").onclick = () => onbShow(3);
  } else if (step === 3) {
    b.innerHTML = `
      <div class="onb-h">Провайдер</div>
      <div class="onb-sub">host + ключ; каталог моделей подтянется сам (/v1/models — чистый passthrough). Можно пропустить и добавить позже в настройках. mock://demo — встроенный мок для пробы.</div>
      <input id="onb-host" type="text" placeholder="host, host/v1 или mock://demo">
      <input id="onb-key" type="password" placeholder="API-ключ (необязательно для локальных)">
      <div class="gate-error" id="onb-provider-note"></div>
      <div class="onb-actions">
        <button class="btn-secondary" id="onb-back-3">← Назад</button>
        <button class="btn-secondary" id="onb-skip-3">Пропустить</button>
        <button class="btn-primary" id="onb-next-3">Подключить →</button>
      </div>`;
    $("onb-back-3").onclick = () => onbShow(2);
    $("onb-skip-3").onclick = () => onbShow(4);
    $("onb-next-3").onclick = async () => {
      const host = $("onb-host").value.trim();
      if (!host) { $("onb-provider-note").textContent = "введите host или нажмите «Пропустить»"; return; }
      $("onb-provider-note").textContent = "подключение…";
      try {
        const r = await api("/api/providers", {
          method: "POST", body: { host, api_key: $("onb-key").value } });
        $("onb-provider-note").textContent =
          r.error ? `подключено с ошибкой каталога: ${r.error}` : `моделей в каталоге: ${(r.models || []).length}`;
        await loadProvidersAndModels().catch(() => {});
        setTimeout(() => onbShow(4), 700);
      } catch (e) { $("onb-provider-note").textContent = e.message; }
    };
  } else if (step === 4) {
    b.innerHTML = `
      <div class="onb-h">Рабочая папка</div>
      <div class="onb-sub">папка, в которой живёт агент (файлы, git-чекпоинты, вложения). Пустая — стандартная data/workspace.</div>
      <input id="onb-ws" type="text" placeholder="/path/to/project (необязательно)">
      <div class="gate-error" id="onb-error-4"></div>
      <div class="onb-actions">
        <button class="btn-secondary" id="onb-back-4">← Назад</button>
        <button class="btn-primary" id="onb-finish">Готово — запустить сервер</button>
      </div>`;
    $("onb-back-4").onclick = () => onbShow(3);
    $("onb-finish").onclick = async () => {
      try {
        await api("/api/onboarding/finish", {
          method: "POST",
          body: { mode: ONB.mode, workspace: $("onb-ws").value.trim() },
        });
        location.reload();
      } catch (e) { $("onb-error-4").textContent = e.message; }
    };
  }
}

/* ------------------------------------------------------------------ skills tab (§6) */

async function loadSkills() {
  const { skills } = await api("/api/skills").catch(() => ({ skills: [] }));
  const list = $("skill-list");
  list.innerHTML = "";
  for (const s of skills) {
    const item = div("skill-item");
    const head = div("skill-head");
    head.innerHTML = `<b>${escapeHtml(s.name)}</b>`;
    const del = document.createElement("button");
    del.className = "btn-danger-sm";
    del.textContent = "—";
    del.onclick = async () => { await api(`/api/skills/${encodeURIComponent(s.name)}`, { method: "DELETE" }); loadSkills(); };
    head.appendChild(del);
    item.appendChild(head);
    const desc = div("skill-desc");
    desc.textContent = s.description || "—";
    item.appendChild(desc);
    const exp = document.createElement("a");
    exp.className = "btn-secondary";
    exp.textContent = "⇩ SKILL.md";
    exp.href = `/api/skills/${encodeURIComponent(s.name)}/export`;
    item.appendChild(exp);
    list.appendChild(item);
  }
  if (!skills.length) list.innerHTML = `<p class="hint">Скилов пока нет. Модель зовёт skill_search → skill_load.</p>`;
}

async function onSkillSubmit(e) {
  e.preventDefault();
  const name = $("sf-name").value.trim();
  if (!name) return;
  try {
    await api("/api/skills", { method: "POST", body: {
      name, description: $("sf-desc").value.trim(), body: $("sf-body").value } });
    $("sf-name").value = $("sf-desc").value = $("sf-body").value = "";
    loadSkills();
  } catch (err) { showToast(err.message, "error"); }
}

async function onSkillImport(e) {
  e.preventDefault();
  const f = $("skill-file").files[0];
  if (!f) return;
  const text = await f.text();
  try {
    const r = await api("/api/skills/import", { method: "POST", body: { text } });
    showToast(`${t("ok")}: ${r.skill.name}`, "ok");
    loadSkills();
  } catch (err) { showToast(err.message, "error"); }
}

/* ------------------------------------------------------------------ hooks tab (§7) */

async function loadHooks() {
  const { hooks } = await api("/api/hooks").catch(() => ({ hooks: [] }));
  const list = $("hook-list");
  list.innerHTML = "";
  for (const h of hooks) {
    const item = div("hook-item");
    const head = div("hook-head");
    const lbl = document.createElement("b");
    lbl.textContent = h.event;
    head.appendChild(lbl);
    const tog = document.createElement("button");
    tog.className = "btn-secondary";
    tog.textContent = h.enabled ? "вкл" : "выкл";
    tog.onclick = async () => {
      await api(`/api/hooks/${h.id}`, { method: "PATCH", body: { enabled: !h.enabled } });
      loadHooks();
    };
    head.appendChild(tog);
    const test = document.createElement("button");
    test.className = "btn-secondary";
    test.textContent = "проверить";
    test.onclick = async () => runHookTest(h, item);
    head.appendChild(test);
    const del = document.createElement("button");
    del.className = "btn-danger-sm";
    del.textContent = "—";
    del.onclick = async () => { await api(`/api/hooks/${h.id}`, { method: "DELETE" }); loadHooks(); };
    head.appendChild(del);
    item.appendChild(head);
    const cmd = div("mono");
    cmd.textContent = `${h.command}   (timeout ${h.timeout}с)`;
    item.appendChild(cmd);
    list.appendChild(item);
  }
  if (!hooks.length) list.innerHTML = `<p class="hint">Хуков нет. Пример секрета: PreToolUse → <code>python -c "import sys,json; d=json.load(sys.stdin); import sys; sys.exit(0) if 'secret' not in str(d.get('args',{})) else (print(json.dumps({'decision':'block','reason':'секрет в аргументах'})) or sys.exit(0))"</code></p>`;
}

async function runHookTest(h, item) {
  let out = item.querySelector(".hook-verdict");
  if (!out) {
    out = document.createElement("pre");
    out.className = "hook-verdict";
    item.appendChild(out);
  }
  out.textContent = "…";
  try {
    const r = await api("/api/hooks/test", { method: "POST",
      body: { event: h.event, command: h.command, timeout: h.timeout } });
    out.textContent = r.timeout
      ? "таймаут — команда не успела"
      : `exit ${r.exit_code}\n${r.stdout || "(пусто)"}`;
  } catch (e) { out.textContent = e.message; }
}

async function onHookSubmit(e) {
  e.preventDefault();
  const command = $("hf-command").value.trim();
  if (!command) return;
  try {
    await api("/api/hooks", { method: "POST", body: {
      event: $("hf-event").value, command,
      timeout: parseFloat($("hf-timeout").value) || 10 } });
    $("hf-command").value = "";
    loadHooks();
  } catch (err) { showToast(err.message, "error"); }
}

/* ------------------------------------------------------------------ appearance tab (§16) */

async function loadAppearance() {
  const s = state.settings || {};
  $("ui-lang").value = state.lang;
  $("theme-select").value = s.theme || "auto";
  const { files } = await api("/api/themes").catch(() => ({ files: [] }));
  const sel = $("custom-css-select");
  const cur = s.custom_css || "";
  sel.innerHTML = `<option value="">${escapeHtml(t("none"))}</option>` +
    files.map((f) => `<option value="${escapeHtml(f.name)}">${escapeHtml(f.name)} (${(f.bytes / 1024).toFixed(1)} КБ)</option>`).join("");
  sel.value = cur;
  $("theme-files").textContent = files.length
    ? `Загружено тем: ${files.length}. Кастомный CSS грузится после встроенного — переменные из example.css побеждают.`
    : "Тем не загружено. Скачайте пример, поправьте и загрузите.";
}

async function onThemeUpload(e) {
  e.preventDefault();
  const f = $("theme-file").files[0];
  if (!f) return;
  const fd = new FormData();
  fd.append("file", f);
  const res = await fetch("/api/themes/upload", { method: "POST", body: fd });
  if (!res.ok) { showToast((await res.json().catch(() => ({}))).error || "upload failed", "error"); return; }
  loadAppearance();
}

async function saveAppearance() {
  const lang = $("ui-lang").value;
  const theme = $("theme-select").value;
  const custom = $("custom-css-select").value;
  try {
    await api("/api/themes/select", { method: "POST", body: { theme, custom_css: custom } });
    await api("/api/settings", { method: "PATCH", body: { ui_lang: lang } });
    state.lang = lang;
    state.settings.theme = theme;
    state.settings.custom_css = custom;
    applyI18n();
    applyTheme(theme, custom);
    showToast(t("ok"), "ok");
  } catch (e) { showToast(e.message, "error"); }
}

/* ------------------------------------------------------------------ logs tab (§16) */

async function loadLogs() {
  try {
    const { entries } = await api(`/api/logs?level=${$("log-level").value}&limit=300`);
    const view = $("log-view");
    view.innerHTML = "";
    for (const e of [...entries].reverse()) {
      const row = div("log-item");
      const d = new Date(e.ts * 1000);
      const time = `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}:${String(d.getSeconds()).padStart(2, "0")}`;
      row.innerHTML = `<span class="lvl ${e.level}">${e.level}</span>` +
        `<span class="src" title="${escapeHtml(e.source)}">${escapeHtml(e.source)}</span>` +
        `<span class="msg">${escapeHtml(String(e.message))}</span>`;
      row.title = time;
      view.appendChild(row);
    }
    if (!entries.length) view.innerHTML = `<p class="hint">(пусто)</p>`;
  } catch (e) { $("log-view").innerHTML = `<p class="hint">${escapeHtml(e.message)}</p>`; }
}

/* ------------------------------------------------------------------ export / import (§16) */

function exportChatUrl(fmt) {
  if (!state.chatId) { showToast(t("selectChat"), "error"); return null; }
  return `/api/chats/${state.chatId}/export?format=${fmt}`;
}

async function onImportSubmit(e) {
  e.preventDefault();
  const f = $("import-file").files[0];
  if (!f) return;
  const text = await f.text();
  const fmt = f.name.toLowerCase().endsWith(".json") ? "json" : "md";
  $("import-result").textContent = "…";
  try {
    const r = await api("/api/chats/import", { method: "POST", body: {
      format: fmt, content: text, title_prefix: $("import-prefix").value.trim() } });
    $("import-result").textContent = `импортирован: ${r.chat.title}`;
    await loadChats();
    openChat(r.chat.id);
  } catch (err) { $("import-result").textContent = err.message; }
}

/* ------------------------------------------------------------------ Phase 5 boot */

async function bootPhase5() {
  try {
    const me = await api("/api/auth/me");
    state.auth = me;
  } catch { /* старый сервер без /api/auth/me */ }
  try {
    const s = await api("/api/settings");
    state.settings = s;
    if (s.ui_lang) state.lang = s.ui_lang;
    applyTheme(s.theme, s.custom_css);
  } catch { /* ignore */ }
  applyI18n();
  renderUserChip();
  if (state.auth.needs_onboarding) {
    onbShow(1);
    return;
  }
  if ((state.auth.mode === "team" || state.auth.mode === "accounts") && !state.auth.user) {
    showGate();
  }
}

function wirePhase5() {
  $("btn-gate-login").onclick = doLogin;
  $("gate-pass").addEventListener("keydown", (e) => { if (e.key === "Enter") doLogin(); });
  $("gate-user").addEventListener("keydown", (e) => { if (e.key === "Enter") doLogin(); });

  $("skill-form").addEventListener("submit", onSkillSubmit);
  $("skill-import-form").addEventListener("submit", onSkillImport);
  $("hook-form").addEventListener("submit", onHookSubmit);
  $("theme-upload-form").addEventListener("submit", onThemeUpload);
  $("btn-save-appearance").onclick = saveAppearance;
  $("ui-lang").onchange = () => { state.lang = $("ui-lang").value; applyI18n(); };
  $("log-level").onchange = async () => {
    await api("/api/logs/level", { method: "POST", body: { level: $("log-level").value } }).catch(() => {});
    loadLogs();
  };
  $("btn-logs-refresh").onclick = loadLogs;
  $("btn-logs-clear").onclick = async () => {
    await api("/api/logs/clear", { method: "POST" }).catch(() => {});
    loadLogs();
  };
  $("btn-logs-export").onclick = () => { window.open("/api/logs/export", "_blank"); };
  $("btn-export-json").onclick = () => { const u = exportChatUrl("json"); if (u) window.open(u, "_blank"); };
  $("btn-export-md").onclick = () => { const u = exportChatUrl("md"); if (u) window.open(u, "_blank"); };
  $("import-form").addEventListener("submit", onImportSubmit);

  // shared-chat toggle appears in the chat header context menu of team mode:
  // долгий клик по заголовку чата — переключение «общий проект» (§2)
  $("chat-title").addEventListener("contextmenu", async (e) => {
    if (!state.chatId || state.auth.mode !== "team") return;
    e.preventDefault();
    const chat = state.chats.find((c) => c.id === state.chatId);
    if (!chat) return;
    const shared = !chat.shared;
    await api(`/api/chats/${state.chatId}`, { method: "PATCH", body: { shared } });
    await loadChats();
    showToast(shared ? "чат помечен как общий" : "чат снова личный", "ok");
  });

  // gate + wizard закрываются только успехом — ESC не саботирует онбординг
  const origOpenSettings = openSettings;
  openSettings = function () {
    origOpenSettings();
    loadSkills();
    loadHooks();
    loadAppearance();
  };
  // init() захвачed the original reference — rebind the button
  $("btn-settings").onclick = openSettings;
}

document.addEventListener("DOMContentLoaded", () => {
  init();               // Phase 1-4 wiring first
  wirePhase5();         // Phase 5 overrides + new tabs
  bootPhase5();         // auth / onboarding / theme / i18n
});

/* queued chip disappears when the turn actually starts (first live delta
   or a persisted event arrives) */
const _origHandleStreamEvent = handleStreamEvent;
handleStreamEvent = function (ev) {
  hideQueueChip();
  _origHandleStreamEvent(ev);
};
