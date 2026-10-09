/* agy/deck v3 — client */
(() => {
"use strict";
const $ = (id) => document.getElementById(id);
const TOKEN_KEY = "agydeck_token";
let token = localStorage.getItem(TOKEN_KEY) || "";
let ws = null, wsReady = false, reconnectTimer = null, reconnectDelay = 1500;
let currentConversationId = null;
let selectedModel = "";
let yolo = true;
let isGenerating = false;
let currentTurn = null;   // {el, head, body, raw, thinkBody, tools:[]}
let term = null, termStarted = false;
let filesPath = "";
let editorPath = "", editorMtime = 0, editorCM = null, editorIsImage = false;

/* ---------------- utils ---------------- */
function toast(msg) {
  const t = $("toast"); t.textContent = msg; t.classList.add("show");
  clearTimeout(t._h); t._h = setTimeout(() => t.classList.remove("show"), 2400);
}
function esc(s) { return (s ?? "").toString(); }
function api(path, opts = {}) {
  return fetch(path, {
    ...opts,
    headers: { "X-Auth-Token": token, "Content-Type": "application/json",
               ...(opts.headers || {}) },
  });
}
function renderMD(text) {
  try {
    const html = marked.parse(text || "", { breaks: true, gfm: true });
    return DOMPurify.sanitize(html);
  } catch (e) { return esc(text).replace(/\n/g, "<br>"); }
}
function fmtDur(s) { return s ? `${Number(s).toFixed(1)}s` : ""; }
function fmtTok(u) {
  if (!u) return "";
  const t = u.total_tokens ?? u.total ?? ((u.input_tokens||0)+(u.output_tokens||0));
  return t ? `${t} tok` : "";
}

/* ---------------- boot ---------------- */
const BOOT_LINES = [
  ["b-tx", "agy/deck v3.0 — terminal session gateway"],
  ["b-ok", "[ok] static assets loaded"],
  ["b-tx", "workspace : " + (window.__WS || "…")],
  ["b-am", "auth      : token required"],
  ["b-tx", "ready."],
];
async function boot() {
  const log = $("boot-log");
  for (const [cls, txt] of BOOT_LINES) {
    await new Promise(r => setTimeout(r, 130));
    const d = document.createElement("div"); d.className = cls; d.textContent = txt;
    log.appendChild(d);
  }
  await new Promise(r => setTimeout(r, 350));
  $("boot").style.display = "none";
  if (token) connect(); else { $("login").hidden = false; $("login-token").focus(); }
}

/* ---------------- login ---------------- */
$("login-btn").addEventListener("click", doLogin);
$("login-token").addEventListener("keydown", e => { if (e.key === "Enter") doLogin(); });
async function doLogin() {
  const v = $("login-token").value.trim();
  if (!v) return;
  try {
    const r = await fetch("/api/status", { headers: { "X-Auth-Token": v } });
    if (r.status === 401) throw new Error("token salah");
    if (!r.ok) throw new Error("server error " + r.status);
    token = v; localStorage.setItem(TOKEN_KEY, v);
    $("login").hidden = true; connect();
  } catch (e) {
    const er = $("login-err"); er.hidden = false; er.textContent = "// " + e.message;
  }
}

/* ---------------- websocket ---------------- */
function wsURL() {
  const p = location.protocol === "https:" ? "wss:" : "ws:";
  return `${p}//${location.host}/ws`;
}
function connect() {
  $("app").hidden = false;
  emptyTranscriptHint();
  ws = new WebSocket(wsURL());
  ws.onopen = () => { ws.send(JSON.stringify({ type: "auth", token })); };
  ws.onmessage = (ev) => handleWS(JSON.parse(ev.data));
  ws.onclose = () => {
    wsReady = false; setComposerStatus("reconnecting…");
    clearTimeout(reconnectTimer);
    reconnectTimer = setTimeout(() => {
      reconnectDelay = Math.min(reconnectDelay * 1.6, 15000); connect();
    }, reconnectDelay);
  };
  ws.onerror = () => { try { ws.close(); } catch (_) {} };
}
function send(o) { if (ws && ws.readyState === 1) ws.send(JSON.stringify(o)); }

function handleWS(m) {
  switch (m.type) {
    case "ready":
      wsReady = true; reconnectDelay = 1500;
      yolo = m.yolo_default !== false;
      updateYoloBadge();
      if (m.models && m.models.length) {
        selectedModel = m.default_model || m.models[0].id;
        $("model-badge").textContent = "model:" + shortModel(selectedModel);
        window._models = m.models;
      }
      setComposerStatus(m.mock ? "mode: mock — pasang API key untuk live"
                               : `mode: live (${m.workspace.split("/").pop()})`);
      $("drawer-foot").textContent = `ws: ${m.mock ? "MOCK" : "LIVE"}\nws: ${m.workspace}`;
      loadSessions(); refreshAuthStatus();
      break;
    case "session":
      if (m.conversation_id) currentConversationId = m.conversation_id;
      if (currentTurn) setHead(currentTurn, "run",
        `[..] ${m.session_kind || ""} ${m.permission_mode ? "· " + m.permission_mode : ""}`);
      break;
    case "turn_start": onTurnStart(m); break;
    case "delta": onDelta(m.text); break;
    case "thinking": onThinking(m.text); break;
    case "tool": onTool(m); break;
    case "approval_request": onApproval(m); break;
    case "turn_end": onTurnEnd(m); break;
    case "approval_ack":
      toast(m.ok ? "keputusan terkirim" : "gagal kirim keputusan"); break;
    case "term_output": onTermOutput(m.data); break;
    case "term_exit": setComposerStatus("shell exited — tap _term untuk restart"); break;
    case "term_started": break;
    case "models":
      window._models = m.models; toast(`${m.models.length} model dimuat`); break;
    case "log": appendLog(m.text); break;
    case "pong": break;
    case "error": toast("⚠ " + m.message); break;
  }
}
function shortModel(id) {
  return (id || "").replace(/^(gemini-|claude-)/, "").slice(0, 22);
}

/* ---------------- transcript ---------------- */
const transcript = $("transcript");
function emptyTranscriptHint() {
  transcript.innerHTML = "";
  const d = document.createElement("div");
  d.className = "u-line"; d.style.borderLeftColor = "var(--faint)";
  d.innerHTML = `<span class="prompt-char" style="color:var(--faint)">$</span>` +
    `<span style="color:var(--faint)">ketik perintah — '/' untuk palette, 'enter' kirim</span>`;
  transcript.appendChild(d);
}
function addUserLine(text) {
  const d = document.createElement("div"); d.className = "u-line";
  const p = document.createElement("span"); p.className = "prompt-char"; p.textContent = "$";
  const t = document.createElement("span"); t.textContent = " " + text;
  d.append(p, t); transcript.appendChild(d); scrollBottom();
}
function onTurnStart(m) {
  isGenerating = true; updateSendBtn();
  const el = document.createElement("div"); el.className = "a-turn";
  const head = document.createElement("div"); head.className = "a-head";
  const body = document.createElement("div"); body.className = "a-body";
  const raw = document.createElement("div"); raw.className = "raw";
  body.appendChild(raw); el.append(head, body); transcript.appendChild(el);
  currentTurn = { el, head, body, raw, thinkBody: null, tools: [], acc: "" };
  setHead(currentTurn, "run", `[..] ${m.session_kind || "agy"} · yolo:${yolo ? "on" : "off"}`);
  setComposerStatus(`<span class="live">● live</span> — ${esc(shortModel(selectedModel))}`);
  scrollBottom(true);
}
function setHead(t, cls, text) {
  t.head.innerHTML = "";
  const s = document.createElement("span"); s.className = "st " + cls;
  s.textContent = text.slice(0, 4);
  const r = document.createElement("span"); r.textContent = text.slice(4);
  t.head.append(s, r);
}
function onDelta(text) {
  if (!currentTurn) return;
  currentTurn.acc += text;
  currentTurn.raw.textContent = currentTurn.acc.slice(-6000);
  scrollBottom();
}
function onThinking(text) {
  if (!currentTurn) return;
  if (!currentTurn.thinkBody) {
    const det = document.createElement("details"); det.className = "thinking";
    const sum = document.createElement("summary"); sum.textContent = "thinking…";
    const tb = document.createElement("div"); tb.className = "th-body";
    det.append(sum, tb);
    currentTurn.body.insertBefore(det, currentTurn.raw);
    currentTurn.thinkBody = tb; currentTurn._thAcc = "";
  }
  currentTurn._thAcc += text;
  currentTurn.thinkBody.textContent = currentTurn._thAcc.slice(-4000);
}
function onTool(m) {
  if (!currentTurn) return;
  const st = (m.state || "ACTIVE").toUpperCase();
  if (st === "ACTIVE") {
    const d = document.createElement("div"); d.className = "tool-line";
    d.dataset.s = "ACTIVE"; d.dataset.name = m.name;
    const a = document.createElement("span"); a.className = "t-arrow"; a.textContent = "→ ";
    const n = document.createElement("b"); n.textContent = m.name;
    const s = document.createElement("span"); s.className = "t-sum";
    s.textContent = " " + (m.summary || "");
    const stEl = document.createElement("span"); stEl.className = "t-state"; stEl.textContent = "[..]";
    d.append(a, n, s, stEl);
    currentTurn.body.insertBefore(d, currentTurn.raw);
    currentTurn.tools.push(d); scrollBottom();
  } else {
    const line = [...currentTurn.tools].reverse()
      .find(el => el.dataset.name === m.name && el.dataset.s === "ACTIVE");
    if (line) {
      line.dataset.s = st === "DONE" ? "DONE" : "ERROR";
      line.querySelector(".t-state").textContent = st === "DONE" ? "[ok]" : "[err]";
      if (m.summary) line.querySelector(".t-sum").textContent = " " + m.summary;
    }
  }
}
function onApproval(m) {
  if (!currentTurn) return;
  const card = document.createElement("div"); card.className = "approval";
  card.innerHTML = `<div class="ap-title">// APPROVAL_REQUIRED (eksperimental)</div>`;
  const tool = document.createElement("div"); tool.className = "ap-tool";
  tool.textContent = `${m.tool} — ${m.summary || ""}`;
  const row = document.createElement("div"); row.className = "ap-row";
  const yes = document.createElement("button"); yes.className = "ap-yes"; yes.textContent = "ALLOW";
  const no = document.createElement("button"); no.className = "ap-no"; no.textContent = "DENY";
  const done = (dec) => {
    send({ type: "approve", approval_id: m.approval_id, decision: dec });
    yes.disabled = no.disabled = true;
    yes.textContent = dec === "allow" ? "ALLOWED ✓" : "ALLOW";
    no.textContent = dec === "deny" ? "DENIED ✕" : "DENY";
  };
  yes.onclick = () => done("allow"); no.onclick = () => done("deny");
  row.append(yes, no);
  const note = document.createElement("div"); note.className = "ap-note";
  note.textContent = "menunggu keputusanmu — turn dijeda";
  card.append(tool, row, note);
  currentTurn.body.insertBefore(card, currentTurn.raw);
  scrollBottom(true); toast("⚠ butuh persetujuan tool");
}
function onTurnEnd(m) {
  isGenerating = false; updateSendBtn();
  if (m.conversation_id) currentConversationId = m.conversation_id;
  if (currentTurn) {
    const t = currentTurn;
    const ok = m.status === "SUCCESS";
    const aborted = m.status === "ABORTED" || m.status === "TIMEOUT";
    setHead(t, ok ? "ok" : "err",
      ok ? `[ok] ${fmtDur(m.duration)} · ${fmtTok(m.usage)}`
         : aborted ? `[--] ${m.status.toLowerCase()}`
                   : `[err] ${m.error || m.status}`.slice(0, 60));
    let finalMD = t.acc;
    if (!finalMD && m.response) finalMD = m.response;
    if (finalMD.trim()) {
      const md = document.createElement("div"); md.className = "md";
      md.innerHTML = renderMD(finalMD);
      t.raw.replaceWith(md);
    } else if (!t.tools.length) {
      t.raw.textContent = "(turn selesai tanpa output teks)";
      t.raw.style.color = "var(--faint)";
    } else { t.raw.remove(); }
    if (m.usage && (m.usage.total_tokens || m.usage.total)) {
      const u = document.createElement("div"); u.className = "usage-line";
      u.textContent = `tokens: in ${m.usage.input_tokens ?? "?"} / out ${m.usage.output_tokens ?? "?"} / total ${m.usage.total_tokens ?? m.usage.total ?? "?"}`;
      t.body.appendChild(u);
    }
    currentTurn = null;
  }
  setComposerStatus("idle — '/' untuk palette");
  scrollBottom(true); loadSessions();
}
function appendLog(text) {
  const d = document.createElement("div"); d.className = "log-line";
  d.textContent = "// " + text; transcript.appendChild(d);
}
function scrollBottom(force) {
  const near = transcript.scrollHeight - transcript.scrollTop - transcript.clientHeight < 160;
  if (force || near) transcript.scrollTop = transcript.scrollHeight;
}

/* ---------------- composer ---------------- */
const promptInput = $("prompt-input"), sendBtn = $("send-btn");
promptInput.addEventListener("input", () => {
  promptInput.style.height = "auto";
  promptInput.style.height = Math.min(promptInput.scrollHeight, 140) + "px";
  if (promptInput.value === "/") openSheet("sheet-palette");
});
promptInput.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); sendOrStop(); }
});
sendBtn.addEventListener("click", sendOrStop);
function updateSendBtn() {
  sendBtn.classList.toggle("stop", isGenerating);
  sendBtn.textContent = isGenerating ? "■" : "➤";
}
function sendOrStop() {
  if (isGenerating) { send({ type: "abort" }); setComposerStatus("menghentikan…"); return; }
  const text = promptInput.value.trim();
  if (!text || !wsReady) return;
  promptInput.value = ""; promptInput.style.height = "auto";
  addUserLine(text);
  send({ type: "chat", prompt: text, model: selectedModel,
         conversation_id: currentConversationId || "", yolo, new_chat: false });
}
function setComposerStatus(html) { $("composer-status").innerHTML = html; }

/* ---------------- drawer / sessions ---------------- */
function openDrawer() { $("drawer").classList.add("open"); $("drawer-backdrop").classList.add("on"); loadSessions(); }
function closeDrawer() { $("drawer").classList.remove("open"); $("drawer-backdrop").classList.remove("on"); }
$("menu-btn").addEventListener("click", openDrawer);
$("drawer-close").addEventListener("click", closeDrawer);
$("drawer-backdrop").addEventListener("click", closeDrawer);
$("new-chat-btn").addEventListener("click", () => {
  currentConversationId = null; emptyTranscriptHint(); closeDrawer();
  setComposerStatus("sesi baru — turn berikutnya mulai fresh"); toast("$ new_session");
});
async function loadSessions() {
  try {
    const r = await api("/api/conversations"); const d = await r.json();
    const list = $("sessions-list"); list.innerHTML = "";
    (d.conversations || []).forEach(c => {
      const s = document.createElement("div");
      s.className = "sess" + (c.id === currentConversationId ? " active" : "");
      const t = document.createElement("div"); t.className = "sess-title"; t.textContent = c.title;
      const m = document.createElement("div"); m.className = "sess-meta";
      m.textContent = `${c.id.slice(0, 8)} · ${c.steps || 0} steps`;
      s.append(t, m);
      s.addEventListener("click", () => { resumeSession(c.id, c.title); closeDrawer(); });
      list.appendChild(s);
    });
    if (!(d.conversations || []).length) list.innerHTML = `<div class="f-empty">// no sessions yet</div>`;
  } catch (e) { /* offline */ }
}
async function resumeSession(cid, title) {
  currentConversationId = cid; transcript.innerHTML = "";
  addUserLine(`-- resume: ${title}`);
  try {
    const r = await api(`/api/conversations/${encodeURIComponent(cid)}`);
    const d = await r.json();
    (d.messages || []).forEach(m => renderHistoryMsg(m));
    scrollBottom(true);
  } catch (e) { toast("gagal muat riwayat"); }
}
function renderHistoryMsg(m) {
  if (m.role === "user") { addUserLine(m.content); return; }
  const el = document.createElement("div"); el.className = "a-turn";
  const head = document.createElement("div"); head.className = "a-head";
  head.innerHTML = `<span class="st ok">[ok]</span><span>history</span>`;
  const body = document.createElement("div"); body.className = "a-body";
  if (m.thinking && m.thinking.trim()) {
    const det = document.createElement("details"); det.className = "thinking";
    det.innerHTML = `<summary>thinking…</summary>`;
    const tb = document.createElement("div"); tb.className = "th-body";
    tb.textContent = m.thinking; det.appendChild(tb); body.appendChild(det);
  }
  (m.tools || []).forEach(tl => {
    const d = document.createElement("div"); d.className = "tool-line"; d.dataset.s = "DONE";
    d.innerHTML = `<span class="t-arrow">→ </span><b></b> <span class="t-sum"></span><span class="t-state">[ok]</span>`;
    d.querySelector("b").textContent = tl.name;
    d.querySelector(".t-sum").textContent = tl.action || tl.summary || "";
    body.appendChild(d);
  });
  if (m.content && m.content.trim()) {
    const md = document.createElement("div"); md.className = "md";
    md.innerHTML = renderMD(m.content); body.appendChild(md);
  }
  el.append(head, body); transcript.appendChild(el);
}

/* ---------------- tabs ---------------- */
document.querySelectorAll(".tab").forEach(tab => {
  tab.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach(t => t.classList.remove("active"));
    document.querySelectorAll(".pane").forEach(p => p.classList.remove("active"));
    tab.classList.add("active"); $(tab.dataset.pane).classList.add("active");
    if (tab.dataset.pane === "pane-term") initTerm();
    if (tab.dataset.pane === "pane-files") loadFiles(filesPath);
    if (tab.dataset.pane === "pane-chat") setTimeout(() => promptInput.focus(), 80);
  });
});

/* ---------------- sheets ---------------- */
function openSheet(id) {
  closeSheets();
  $(id).classList.add("open"); $("sheet-backdrop").classList.add("on");
  if (id === "sheet-palette") { renderPalette(""); setTimeout(() => $("palette-input").focus(), 120); }
  if (id === "sheet-models") renderModels();
  if (id === "sheet-yolo") markYoloSel();
}
function closeSheets() {
  document.querySelectorAll(".sheet").forEach(s => s.classList.remove("open"));
  $("sheet-backdrop").classList.remove("on");
  if (typeof cancelGoogleLogin === "function") cancelGoogleLogin(true);
}
$("sheet-backdrop").addEventListener("click", closeSheets);

const PALETTE = [
  { cmd: "/new", desc: "sesi chat baru (fresh)", run: () => { currentConversationId = null; emptyTranscriptHint(); toast("$ new_session"); } },
  { cmd: "/model", desc: "ganti model AI", run: () => openSheet("sheet-models") },
  { cmd: "/yolo", desc: "mode persetujuan tools (yolo/review)", run: () => openSheet("sheet-yolo") },
  { cmd: "/stop", desc: "hentikan turn berjalan", run: () => send({ type: "abort" }) },
  { cmd: "/clear", desc: "bersihkan layar chat", run: () => emptyTranscriptHint() },
  { cmd: "/export", desc: "unduh transkrip sebagai .md", run: exportMD },
  { cmd: "/term", desc: "buka tab terminal", run: () => document.querySelector('[data-pane="pane-term"]').click() },
  { cmd: "/files", desc: "buka tab files", run: () => document.querySelector('[data-pane="pane-files"]').click() },
  { cmd: "/models", desc: "refresh daftar model dari CLI", run: () => send({ type: "models" }) },
  { cmd: "/key", desc: "login google / pasang API key (akunmu sendiri)", run: () => { openSheet("sheet-key"); refreshAuthStatus(); } },
];
function renderPalette(filter) {
  const list = $("palette-list"); list.innerHTML = "";
  const f = (filter || "").toLowerCase().replace(/^\//, "");
  PALETTE.filter(p => !f || p.cmd.includes(f) || p.desc.toLowerCase().includes(f))
    .forEach(p => {
      const b = document.createElement("button"); b.className = "pal-item";
      b.innerHTML = `<span class="pal-cmd"></span><span class="pal-desc"></span>`;
      b.querySelector(".pal-cmd").textContent = p.cmd;
      b.querySelector(".pal-desc").textContent = p.desc;
      b.addEventListener("click", () => { closeSheets(); p.run(); });
      list.appendChild(b);
    });
}
$("palette-input").addEventListener("input", e => renderPalette(e.target.value));
$("palette-input").addEventListener("keydown", e => {
  if (e.key === "Enter") {
    const first = $("palette-list").querySelector(".pal-item");
    if (first) first.click();
  }
  if (e.key === "Escape") closeSheets();
});
function renderModels() {
  const list = $("models-list"); list.innerHTML = "";
  (window._models || []).forEach(m => {
    const b = document.createElement("button");
    b.className = "model-item" + (m.id === selectedModel ? " active" : "");
    b.innerHTML = `<div class="m-name"></div><div class="m-id"></div>`;
    b.querySelector(".m-name").textContent = m.name;
    b.querySelector(".m-id").textContent = m.id;
    b.addEventListener("click", () => {
      selectedModel = m.id;
      $("model-badge").textContent = "model:" + shortModel(m.id);
      closeSheets(); toast("model → " + m.id);
    });
    list.appendChild(b);
  });
}
$("model-badge").addEventListener("click", () => openSheet("sheet-models"));
$("yolo-badge").addEventListener("click", () => openSheet("sheet-yolo"));
$("key-badge").addEventListener("click", () => { openSheet("sheet-key"); refreshAuthStatus(); });
async function refreshAuthStatus() {
  try {
    const r = await api("/api/auth/status"); const d = await r.json();
    const b = $("key-badge");
    if (d.method === "google") {
      b.textContent = "google:✓"; b.classList.add("ok");
    } else if (d.method === "key") {
      b.textContent = "key:✓"; b.classList.add("ok");
    } else {
      b.textContent = "key:—"; b.classList.remove("ok");
    }
    const st = $("key-status");
    st.textContent = d.method === "google" ? "// login google aktif — agy jalan sebagai akunmu"
      : d.method === "key" ? "// key tersimpan — sesi berikutnya pakai API key"
      : "// belum ada auth — masih mode mock / butuh login CLI";
    st.classList.toggle("ok", d.method !== "none");
  } catch (_) {}
}
// ---- Google OAuth login flow ----
let gLoginId = null, gPollTimer = null;
function googleFlowReset() {
  $("google-flow").hidden = true;
  $("google-login-btn").hidden = false;
  $("google-code-row").hidden = true;
  $("google-code").value = "";
}
async function cancelGoogleLogin(silent) {
  if (gPollTimer) { clearInterval(gPollTimer); gPollTimer = null; }
  if (gLoginId) {
    const id = gLoginId; gLoginId = null;
    if (!silent) {
      try { await api("/api/auth/google/cancel", { method: "POST", body: JSON.stringify({ login_id: id }) }); } catch (_) {}
    } else {
      api("/api/auth/google/cancel", { method: "POST", body: JSON.stringify({ login_id: id }) }).catch(() => {});
    }
  }
  googleFlowReset();
}
$("google-login-btn").addEventListener("click", async () => {
  $("google-login-btn").disabled = true;
  try {
    const r = await api("/api/auth/google/start", { method: "POST" });
    if (!r.ok) { toast("gagal mulai login google"); return; }
    const d = await r.json();
    gLoginId = d.login_id;
    $("google-flow").hidden = false;
    $("google-login-btn").hidden = true;
    $("google-wait").textContent = "menyiapkan…";
    $("google-url").textContent = "menyiapkan link…";
    $("google-url").removeAttribute("href");
    gPollTimer = setInterval(pollGoogle, 2500);
    pollGoogle();
  } finally { $("google-login-btn").disabled = false; }
});
async function pollGoogle() {
  if (!gLoginId) return;
  let d;
  try {
    const r = await api("/api/auth/google/poll?login_id=" + encodeURIComponent(gLoginId));
    d = await r.json();
  } catch (_) { return; }
  if (d.status === "unknown") { cancelGoogleLogin(); toast("sesi login hilang"); return; }
  if (d.url) {
    const a = $("google-url");
    a.href = d.url;
    a.textContent = d.url.length > 72 ? d.url.slice(0, 72) + "…" : d.url;
    if ($("google-wait").textContent === "menyiapkan…")
      $("google-wait").textContent = "// buka link di atas, login, lalu kembali & tekan cek";
  }
  if (d.check === "ok") {
    const id = gLoginId;
    clearInterval(gPollTimer); gPollTimer = null; gLoginId = null;
    const done = await api("/api/auth/google/done", { method: "POST",
      body: JSON.stringify({ login_id: id }) });
    googleFlowReset();
    if (done.ok) { toast("login google ok ✓ — agy jalan sebagai akunmu"); refreshAuthStatus(); }
    else toast("verifikasi ok, tapi finalisasi gagal");
    return;
  }
  if (d.check === "fail")
    $("google-wait").textContent = "// belum terdeteksi — login dulu di browser, atau tempel kode manual";
  if (d.status === "exited" && !d.url) { cancelGoogleLogin(); toast("proses login berhenti — coba lagi"); }
}
$("google-check").addEventListener("click", async () => {
  if (!gLoginId) return;
  $("google-wait").textContent = "memverifikasi ke server…";
  await api("/api/auth/google/check", { method: "POST",
    body: JSON.stringify({ login_id: gLoginId }) });
});
$("google-code-toggle").addEventListener("click", () => {
  $("google-code-row").hidden = !$("google-code-row").hidden;
});
$("google-code-send").addEventListener("click", async () => {
  const code = $("google-code").value.trim();
  if (!code || !gLoginId) { toast("isi kodenya dulu"); return; }
  $("google-wait").textContent = "mengirim kode & memverifikasi…";
  const r = await api("/api/auth/google/code", { method: "POST",
    body: JSON.stringify({ login_id: gLoginId, code }) });
  if (!r.ok) toast("gagal kirim kode");
});
$("google-cancel").addEventListener("click", () => cancelGoogleLogin());
$("key-save").addEventListener("click", async () => {
  const v = $("key-input").value.trim();
  if (v.length < 20) { toast("key terlalu pendek"); return; }
  const r = await api("/api/settings/key", { method: "POST",
    body: JSON.stringify({ key: v }) });
  if (r.ok) {
    $("key-input").value = "";
    toast("key tersimpan ✓"); refreshAuthStatus();
  } else toast("gagal simpan key");
});
function updateYoloBadge() {
  const b = $("yolo-badge");
  b.textContent = yolo ? "yolo" : "review";
  b.classList.toggle("warn", yolo);
}
function markYoloSel() {
  document.querySelectorAll(".yolo-opt").forEach(o =>
    o.classList.toggle("sel", o.dataset.yolo === (yolo ? "1" : "0")));
}
document.querySelectorAll(".yolo-opt").forEach(o => {
  o.addEventListener("click", () => {
    yolo = o.dataset.yolo === "1"; updateYoloBadge(); markYoloSel();
    closeSheets(); toast("tool approval: " + (yolo ? "yolo (auto)" : "review (eksperimental)"));
  });
});
function exportMD() {
  let out = `# agy/deck — ${new Date().toISOString().slice(0, 10)}\n\n`;
  transcript.querySelectorAll(".u-line,.a-turn").forEach(el => {
    if (el.classList.contains("u-line")) out += `## $ ${el.textContent.trim().slice(2)}\n\n`;
    else {
      const md = el.querySelector(".md");
      out += (md ? md.innerText : el.textContent).trim() + "\n\n---\n\n";
    }
  });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(new Blob([out], { type: "text/markdown" }));
  a.download = "agy-deck-transcript.md"; a.click();
  toast("transkrip diunduh");
}

/* ---------------- terminal (xterm.js + PTY) ---------------- */
function initTerm() {
  if (term) { setTimeout(() => term.focus(), 60); fitTerm(); return; }
  term = new Terminal({
    cursorBlink: true, fontSize: 13.5, fontFamily: "'JetBrains Mono',monospace",
    theme: { background: "#070A0F", foreground: "#D6DEEB", cursor: "#FFB000",
             selectionBackground: "rgba(255,176,0,.25)", black: "#0B0E14",
             green: "#3FB950", red: "#F85149", yellow: "#FFB000" },
  });
  term.open($("xterm"));
  term.writeln("\x1b[33m$ connecting pty…\x1b[0m");
  const { cols, rows } = termSize();
  send({ type: "term_start", cols, rows });
  termStarted = true;
  term.onData(d => send({ type: "term_input", data: d }));
  term.attachCustomKeyEventHandler(() => true);
  setTimeout(() => { fitTerm(); term.focus(); }, 120);
  window.addEventListener("resize", fitTerm);
}
function termSize() {
  const el = $("xterm");
  const w = el.clientWidth || 360, h = el.clientHeight || 400;
  return { cols: Math.max(40, Math.floor(w / 8.2)), rows: Math.max(12, Math.floor(h / 19)) };
}
function fitTerm() {
  if (!term) return;
  const { cols, rows } = termSize();
  try { term.resize(cols, rows); } catch (_) {}
  send({ type: "term_resize", cols, rows });
}
function onTermOutput(b64) {
  if (!term) return;
  try {
    const bin = atob(b64); const bytes = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
    term.write(bytes);
  } catch (_) {}
}

/* ---------------- files ---------------- */
async function loadFiles(path = "") {
  filesPath = path;
  const list = $("files-list");
  list.innerHTML = `<div class="f-empty">// listing…</div>`;
  try {
    const r = await api(`/api/files?path=${encodeURIComponent(path)}`);
    const d = await r.json();
    renderCrumbs(d.breadcrumbs || []);
    list.innerHTML = "";
    if (d.parent_path !== null && d.parent_path !== undefined) {
      list.appendChild(fRow("..", "..", true, "", "", () => loadFiles(d.parent_path)));
    }
    (d.items || []).forEach(it => {
      list.appendChild(fRow(it.name, it.path, it.is_dir, it.ext,
        it.is_dir ? "dir" : `${fmtBytes(it.size)} · ${it.mtime_str}`,
        () => it.is_dir ? loadFiles(it.path) : openEditor(it.path)));
    });
    if (!(d.items || []).length && d.parent_path === null)
      list.innerHTML = `<div class="f-empty">// empty</div>`;
  } catch (e) { list.innerHTML = `<div class="f-empty">// ${e.message}</div>`; }
}
function fRow(name, path, isDir, ext, meta, onClick) {
  const r = document.createElement("div"); r.className = "f-row";
  const ico = document.createElement("span"); ico.className = "f-ico";
  ico.textContent = isDir ? "[d]" : "[" + (ext || "f").replace(".", "").slice(0, 3) + "]";
  const nm = document.createElement("span"); nm.className = "f-name"; nm.textContent = name;
  const mt = document.createElement("span"); mt.className = "f-meta"; mt.textContent = meta;
  r.append(ico, nm, mt);
  if (!isDir && path !== "..") {
    const del = document.createElement("button"); del.className = "f-del"; del.textContent = "×";
    del.title = "hapus";
    del.addEventListener("click", async (e) => {
      e.stopPropagation();
      if (confirm(`hapus ${name}?`)) {
        await api("/api/file_delete", { method: "POST", body: JSON.stringify({ path }) });
        loadFiles(filesPath); toast("dihapus");
      }
    });
    r.appendChild(del);
  }
  r.querySelector(".f-name").addEventListener("click", onClick);
  ico.addEventListener("click", onClick);
  return r;
}
function fmtBytes(b) {
  if (!b) return "0 B";
  const u = ["B", "KB", "MB", "GB"]; const i = Math.floor(Math.log(b) / Math.log(1024));
  return (b / Math.pow(1024, i)).toFixed(1) + " " + u[i];
}
function renderCrumbs(crumbs) {
  const c = $("crumbs"); c.innerHTML = "";
  crumbs.forEach((b, i) => {
    const s = document.createElement("button"); s.className = "crumb"; s.textContent = b.name;
    s.addEventListener("click", () => loadFiles(b.path));
    c.appendChild(s);
    if (i < crumbs.length - 1) {
      const sep = document.createElement("span"); sep.className = "crumb-sep"; sep.textContent = "/";
      c.appendChild(sep);
    }
  });
}
$("files-refresh").addEventListener("click", () => loadFiles(filesPath));
$("file-new").addEventListener("click", () => makeItem(false));
$("folder-new").addEventListener("click", () => makeItem(true));
async function makeItem(isDir) {
  const name = prompt(isDir ? "nama folder:" : "nama file:");
  if (!name) return;
  const p = filesPath ? filesPath + "/" + name : name;
  const r = await api("/api/file_create", { method: "POST",
    body: JSON.stringify({ path: p, is_dir: isDir }) });
  if (r.ok) { loadFiles(filesPath); if (!isDir) openEditor(p); }
  else toast("gagal: " + (await r.json()).detail);
}

/* editor */
const IMG_EXTS = [".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".bmp", ".ico"];
const CM_MODES = { ".py": "python", ".js": "javascript", ".ts": "javascript",
  ".jsx": "javascript", ".tsx": "javascript", ".json": "javascript",
  ".html": "xml", ".xml": "xml", ".vue": "xml",
  ".css": "css", ".md": "markdown", ".sh": "shell", ".bash": "shell" };
async function openEditor(path) {
  editorPath = path; editorIsImage = false;
  $("editor-title").textContent = path.split("/").pop();
  $("editor-dirty").hidden = true;
  $("editor-modal").hidden = false;
  $("editor-img-wrap").hidden = true;
  if (editorCM) { editorCM.getWrapperElement().style.display = "none"; }
  $("editor-fallback").style.display = "none";
  try {
    const r = await api(`/api/file_content?path=${encodeURIComponent(path)}`);
    const d = await r.json();
    if (d.kind === "image") {
      editorIsImage = true;
      $("editor-img-wrap").hidden = false;
      $("editor-img").src = `/api/raw?path=${encodeURIComponent(path)}&token=${encodeURIComponent(token)}`;
      $("editor-save").style.display = "none";
      return;
    }
    $("editor-save").style.display = "";
    editorMtime = d.mtime || 0;
    const ext = (d.name.match(/\.[^.]+$/) || [""])[0].toLowerCase();
    if (IMG_EXTS.includes(ext)) { /* handled above */ }
    if (window.CodeMirror && !editorIsImage) {
      if (!editorCM) {
        editorCM = CodeMirror($("editor-modal"), {
          lineNumbers: true, theme: "default", indentUnit: 2,
          viewportMargin: Infinity,
        });
        editorCM.on("change", () => { $("editor-dirty").hidden = false; });
        $("editor-modal").insertBefore(editorCM.getWrapperElement(), $("editor-fallback"));
      }
      editorCM.getWrapperElement().style.display = "";
      editorCM.setOption("mode", CM_MODES[ext] || null);
      editorCM.setValue(d.content || "");
      editorCM.clearHistory();
      setTimeout(() => editorCM.refresh(), 60);
    } else {
      const ta = $("editor-fallback"); ta.style.display = "";
      ta.value = d.content || "";
      ta.oninput = () => { $("editor-dirty").hidden = false; };
    }
  } catch (e) { toast("gagal buka file"); $("editor-modal").hidden = true; }
}
$("editor-close").addEventListener("click", () => { $("editor-modal").hidden = true; });
$("editor-save").addEventListener("click", async () => {
  const content = editorCM && editorCM.getWrapperElement().style.display !== "none"
    ? editorCM.getValue() : $("editor-fallback").value;
  const r = await api("/api/file_save", { method: "POST",
    body: JSON.stringify({ path: editorPath, content, expected_mtime: editorMtime }) });
  if (r.status === 409) {
    toast("⚠ file berubah di luar — dimuat ulang, editanmu TIDAK disimpan");
    openEditor(editorPath); return;
  }
  const d = await r.json();
  if (d.ok) { editorMtime = d.mtime; $("editor-dirty").hidden = true;
    toast("saved ✓"); loadFiles(filesPath); }
  else toast("gagal simpan");
});

/* ---------------- PWA ---------------- */
if ("serviceWorker" in navigator) {
  window.addEventListener("load", () => {
    navigator.serviceWorker.register("/static/sw.js").catch(() => {});
  });
}

/* ---------------- go ---------------- */
updateSendBtn();
boot();
})();
