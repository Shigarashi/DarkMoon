/* Darkmoon front-end. Plain JS, no build step.
   Public API for UI plugins lives on window.Darkmoon (alias: window.LocalAgent) — see README "UI plugins". */
(() => {
  "use strict";

  // ── Helpers ───────────────────────────────────────────────────────────
  const $ = (s, el = document) => el.querySelector(s);
  const $$ = (s, el = document) => [...el.querySelectorAll(s)];
  const h = (tag, attrs = {}, ...kids) => {
    const el = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (v == null || v === false) continue;
      if (k === "class") el.className = v;
      else if (k === "html") el.innerHTML = v;
      else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
      else if (k === "dataset") Object.assign(el.dataset, v);
      else el.setAttribute(k, v === true ? "" : v);
    }
    for (const kid of kids.flat()) if (kid != null && kid !== false) el.append(kid.nodeType ? kid : document.createTextNode(kid));
    return el;
  };
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const debounce = (fn, ms) => { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; };
  const get = (obj, path) => path.split(".").reduce((o, k) => (o == null ? o : o[k]), obj);
  const setPath = (obj, path, val) => { const ks = path.split("."); let o = obj; ks.slice(0, -1).forEach((k) => (o = o[k] ??= {})); o[ks.at(-1)] = val; };

  async function api(path, { method = "GET", body, raw = false } = {}) {
    const r = await fetch(path, {
      method, headers: { "x-token": window.LA_TOKEN, "content-type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    if (raw) return r;
    const ct = r.headers.get("content-type") || "";
    const data = ct.includes("json") ? await r.json() : await r.text();
    if (!r.ok) throw new Error((data && data.error) || `HTTP ${r.status}`);
    return data;
  }

  function toast(msg, kind = "") {
    const t = h("div", { class: `toast ${kind}` }, msg);
    $("#toasts").append(t);
    setTimeout(() => { t.style.opacity = "0"; t.style.transition = "opacity .3s"; setTimeout(() => t.remove(), 300); }, kind === "error" ? 6000 : 3200);
  }

  const BACKENDS = ["ollama", "grok_cli", "claude_cli", "xai_api", "claude_api", "openai_compat"];
  const CLOUD = BACKENDS.slice(1);
  const BL = (b) => (b === "openai_compat" && S.state?.config?.openai_compat?.label) || S.state?.backends?.[b]?.short || b || "";
  const isMcpTool = (n) => n && n.includes("__");
  const TOOL_ICON = { web_search: "🔎", fetch_url: "🌐", list_dir: "📁", read_file: "📄", write_file: "💾", edit_file: "✏️",
    find_files: "🗂", run_shell: "⌨", run_python: "🐍", current_datetime: "🕒",
    project_overview: "🧭", run_tests: "🧪", git_status: "🌿", git_diff: "±", git_log: "📜", git_branch: "🌱", git_commit: "✅", open_pull_request: "🔀" };

  // ── State ─────────────────────────────────────────────────────────────
  const S = {
    state: null, ui: {}, convs: [], convId: null, mode: localStorage.getItem("la.mode") || "auto",
    kind: localStorage.getItem("la.kind") || "chat", project: localStorage.getItem("la.project") || "", projects: [], convProject: null,
    model: "", busy: false, es: null, turn: null, approvalQueue: [],
  };

  // ── Event bus (used by plugins) ───────────────────────────────────────
  const bus = {
    h: {},
    on(type, fn) { (this.h[type] ??= new Set()).add(fn); return () => this.h[type].delete(fn); },
    emit(type, payload) { for (const fn of this.h[type] || []) { try { fn(payload); } catch (e) { console.error("[plugin handler]", e); } } },
  };

  // ── Markdown ──────────────────────────────────────────────────────────
  marked.setOptions({ gfm: true, breaks: false });
  function prepThink(text) {
    // <think>…</think> → collapsible block (handles unterminated block while streaming)
    return text.replace(/<think>([\s\S]*?)(<\/think>|$)/g, (_, inner, end) => {
      const open = S.ui.show_thinking || !end ? " open" : "";
      return `\n<details class="think"${open}><summary>${end ? "Thought process" : "Thinking…"}</summary>\n\n${esc(inner.trim())}\n\n</details>\n\n`;
    });
  }
  function renderMD(el, text) {
    el.innerHTML = DOMPurify.sanitize(marked.parse(prepThink(text || "")), { ADD_ATTR: ["open"] });
    enhanceCode(el);
    $$("a", el).forEach((a) => { a.target = "_blank"; a.rel = "noopener"; });
  }
  function enhanceCode(root) {
    $$("pre > code", root).forEach((code) => {
      const pre = code.parentElement;
      if (pre.parentElement.classList.contains("code-wrap")) return;
      const lang = (code.className.match(/language-([\w+-]+)/) || [])[1] || "";
      try {
        if (lang && hljs.getLanguage(lang)) code.innerHTML = hljs.highlight(code.textContent, { language: lang }).value;
        else if (code.textContent.length < 20000) code.innerHTML = hljs.highlightAuto(code.textContent).value;
      } catch { /* ignore */ }
      const copy = h("button", { onclick: () => { navigator.clipboard.writeText(code.textContent); copy.textContent = "Copied"; setTimeout(() => (copy.textContent = "Copy"), 1200); } }, "Copy");
      const wrap = h("div", { class: "code-wrap" }, h("div", { class: "code-head" }, h("span", {}, lang || "code"), copy));
      pre.replaceWith(wrap); wrap.append(pre);
    });
  }

  // ── Appearance ────────────────────────────────────────────────────────
  function applyAppearance() {
    const u = S.ui;
    const theme = (S.state?.themes || []).find((t) => t.file.replace(/\.css$/, "") === u.theme || t.file === u.theme);
    $("#theme-css").href = theme ? `${theme.url}?v=${Date.now() % 1e6}` : "";
    const vars = [];
    if (u.accent) vars.push(`--accent:${u.accent}`);
    if (u.font) vars.push(`--font:${u.font}, system-ui, sans-serif`);
    if (u.mono_font) vars.push(`--font-mono:${u.mono_font}, monospace`);
    if (u.font_size) vars.push(`--font-size:${u.font_size}px`);
    if (u.radius != null) vars.push(`--radius:${u.radius}px`);
    if (u.chat_width) vars.push(`--chat-width:${u.chat_width}px`);
    vars.push(`--bg-dim:${u.bg_image ? u.bg_dim : 0}`);
    $("#appearance-css").textContent = `:root{${vars.join(";")}}` +
      (u.bg_image ? `#bg-layer{background-image:url("${String(u.bg_image).replace(/"/g, "%22")}");filter:blur(${u.bg_blur || 0}px);transform:scale(1.04)}
       body{background:transparent} #topbar{background:color-mix(in srgb,var(--bg) 55%,transparent)}
       #sidebar{background:color-mix(in srgb,var(--sidebar-bg) 70%,transparent);backdrop-filter:blur(16px)}` : "");
    document.body.classList.toggle("density-compact", u.density === "compact");
    document.body.classList.toggle("density-cozy", u.density === "cozy");
    document.body.classList.toggle("hide-tool-details", !u.show_tool_details);
    document.body.classList.toggle("hide-avatars", u.show_avatars === false);
    document.body.classList.toggle("sidebar-collapsed", !!u.sidebar_collapsed);
  }
  async function applyQuickCSS() {
    if (!S.ui.quickcss_enabled) { $("#quick-css").textContent = ""; return; }
    $("#quick-css").textContent = await api("/api/quickcss");
  }
  const saveUI = debounce((patch) => api("/api/ui-settings", { method: "POST", body: patch }).catch((e) => toast(e.message, "error")), 300);
  function setUI(patch) { Object.assign(S.ui, patch); applyAppearance(); saveUI(patch); bus.emit("ui-settings", S.ui); }

  // ── UI plugin system (Vencord-style) ─────────────────────────────────
  const Plugins = {
    defs: {}, active: {}, loading: null, cmds: new Map(),
    register(def) {
      const file = this.loading || def.name;
      this.defs[file] = def;
    },
    async loadAll() {
      for (const p of S.state.ui_plugins) {
        if (!(S.ui.enabled_plugins || []).includes(p.file)) continue;
        await this.load(p); this.start(p.file);
      }
    },
    load(p) {
      if (this.defs[p.file]) return Promise.resolve();
      return new Promise((res) => {
        this.loading = p.file;
        const s = h("script", { src: `${p.url}?v=${Date.now() % 1e6}` });
        s.onload = s.onerror = () => { this.loading = null; res(); };
        document.body.append(s);
      });
    },
    start(file) {
      const def = this.defs[file];
      if (!def || this.active[file]) return;
      const disposers = [];
      const track = (fn) => (disposers.push(fn), fn);
      const papi = {
        on: (type, fn) => track(bus.on(type, fn)),
        onMessageRendered: (fn) => track(bus.on("message-rendered", fn)),
        injectCSS: (css) => { const st = h("style", { "data-plugin": file }, css); document.head.append(st); return track(() => st.remove()); },
        addHeaderButton: ({ icon, title, onClick }) => { const b = h("button", { class: "icon-btn", title, onclick: onClick }, icon); $("#header-plugins").append(b); return track(() => b.remove()); },
        addComposerWidget: (el) => { $("#composer-extra").append(el); return track(() => el.remove()); },
        addSlashCommand: (cmd) => { Plugins.cmds.set(cmd.name, { ...cmd, plugin: file }); return track(() => Plugins.cmds.delete(cmd.name)); },
        toast, send: (text) => sendMessage(text), setInput: (t) => { $("#input").value = t; autosize(); },
        getInput: () => $("#input").value, get state() { return { convId: S.convId, mode: S.mode, busy: S.busy, ui: { ...S.ui } }; },
        setUI, api, h, Darkmoon: window.Darkmoon,
        storage: { get: (k, d) => { try { return JSON.parse(localStorage.getItem(`la.p.${file}.${k}`)) ?? d; } catch { return d; } },
                   set: (k, v) => localStorage.setItem(`la.p.${file}.${k}`, JSON.stringify(v)) },
      };
      try { def.start?.(papi); this.active[file] = disposers; }
      catch (e) { console.error(e); toast(`Plugin ${def.name || file} failed: ${e.message}`, "error"); disposers.forEach((d) => d()); }
    },
    stop(file) {
      const def = this.defs[file];
      try { def?.stop?.(); } catch (e) { console.error(e); }
      (this.active[file] || []).forEach((d) => { try { d(); } catch { /* */ } });
      delete this.active[file];
    },
    async toggle(p, on) {
      const list = new Set(S.ui.enabled_plugins || []);
      if (on) { await this.load(p); this.start(p.file); list.add(p.file); } else { this.stop(p.file); list.delete(p.file); }
      setUI({ enabled_plugins: [...list] });
    },
  };

  // ── Event stream (SSE) + commands ───────────────────────────────────
  function connect() {
    const es = new EventSource(`/api/events?token=${encodeURIComponent(window.LA_TOKEN)}`);
    S.es = es;
    es.onmessage = (e) => {
      const ev = JSON.parse(e.data);
      if (ev.type === "hello") { if (!ev.busy && S.busy && !S.turn) setBusy(false); return; }
      handleEvent(ev); bus.emit("event", ev); bus.emit(ev.type, ev);
    };
    es.onerror = () => { if (S.busy && es.readyState === EventSource.CLOSED) { setBusy(false); turnError("Connection lost."); } };
  }
  const post = (path, body) => api(path, { method: "POST", body }).catch((e) => { toast(e.message, "error"); throw e; });

  // ── Message rendering ─────────────────────────────────────────────────
  const msgsEl = () => $("#messages");
  let stickBottom = true;
  function scrollDown(force = false) { if (force || stickBottom) msgsEl().scrollTop = msgsEl().scrollHeight; }

  function userMsgEl(text, id) {
    const el = h("div", { class: "msg user", dataset: { id: id || "" } },
      h("div", { class: "avatar" }, S.ui.avatar_user || "🧑"),
      h("div", { class: "body" }, h("div", { class: "bubble" }, text),
        h("div", { class: "msg-actions" },
          h("button", { title: "Copy", onclick: () => navigator.clipboard.writeText(text) }, "Copy"),
          h("button", { title: "Edit and resend", onclick: () => editMessage(el, text) }, "Edit"))));
    el._msg = { role: "user", content: text, id };
    return el;
  }

  function newTurnEl() {
    const content = h("div", { class: "turn-content" });
    const meta = h("div", { class: "meta-row" });
    const actions = h("div", { class: "msg-actions" });
    const el = h("div", { class: "msg assistant" }, h("div", { class: "avatar" }, S.ui.avatar_bot || "🤖"),
      h("div", { class: "body" }, meta, content, actions));
    return { el, content, meta, actions, seg: null, text: "", backend: null, tools: {}, texts: [] };
  }

  function setTurnMeta(turn, backend, model) {
    turn.backend = backend;
    turn.meta.innerHTML = "";
    turn.meta.append(h("span", { class: `chip ${backend}` }, BL(backend)), model ? h("span", {}, model) : "");
  }

  function startSegment(turn) {
    const md = h("div", { class: "md segment" });
    turn.content.append(md);
    turn.seg = md; turn.text = "";
    return md;
  }
  const renderSegThrottled = (() => {
    let pending = false;
    return (turn) => {
      if (pending) return; pending = true;
      requestAnimationFrame(() => { pending = false; if (turn.seg) { renderMD(turn.seg, turn.text); turn.seg.classList.add("cursor"); scrollDown(); } });
    };
  })();
  function endSegment(turn) {
    if (!turn.seg) return;
    renderMD(turn.seg, turn.text); turn.seg.classList.remove("cursor");
    if (turn.text.trim()) turn.texts.push(turn.text);
    else turn.seg.remove();
    turn.seg = null; turn.text = "";
  }

  function toolCardEl(name, args, result, ok) {
    const argStr = Object.entries(args || {}).map(([k, v]) => `${k}=${typeof v === "string" ? JSON.stringify(v.length > 80 ? v.slice(0, 80) + "…" : v) : JSON.stringify(v)}`).join(" ");
    const state = h("span", { class: "tstate" }, result == null ? "running…" : ok ? "done" : "failed");
    const live = h("pre", { class: "tlive", hidden: true });
    const res = h("pre", { class: "tresult", hidden: result == null }, result ?? "");
    const card = h("details", { class: `tool-card ${result == null ? "running" : ok ? "ok" : "fail"}` },
      h("summary", {}, h("span", { class: "ticon" }, TOOL_ICON[name] || (isMcpTool(name) ? "🔌" : "🔧")), h("span", { class: "tname" }, name), h("span", { class: "targ" }, argStr), state),
      h("div", { class: "tlabel" }, "arguments"), h("pre", {}, JSON.stringify(args, null, 2)),
      h("div", { class: "tlabel" }, "output"), live, res);
    card._parts = { state, live, res };
    return card;
  }

  function finishTurn(turn, { backend, model } = {}) {
    endSegment(turn);
    $(".typing", turn.content)?.remove();
    if (backend) setTurnMeta(turn, backend, model);
    const full = turn.texts.join("\n\n");
    turn.actions.innerHTML = "";
    turn.actions.append(
      h("button", { onclick: () => navigator.clipboard.writeText(full) }, "Copy"),
      h("button", { title: "Ask again", onclick: () => regenerate() }, "Retry"),
      ...(backend === "ollama" ? cloudAskButtons() : []));
    bus.emit("message-rendered", { el: turn.el, role: "assistant", content: full, backend, model, live: true, created: turn.created });
    scrollDown();
  }

  function turnError(message) {
    const t = S.turn;
    const box = h("div", { class: "error-box" }, "⚠ " + message);
    if (t) { endSegment(t); $(".typing", t.content)?.remove(); t.content.append(box); }
    else msgsEl().append(h("div", { class: "msg" }, h("div", { class: "body" }, box)));
    scrollDown();
  }

  function renderHistory(messages) {
    const box = msgsEl();
    box.innerHTML = "";
    let turn = null;
    const toolCards = {};
    for (const m of messages) {
      if (m.role === "user") {
        if (turn) finishTurn(turn, { backend: turn.backend, model: turn.model });
        turn = null;
        const el = userMsgEl(m.content, m.id);
        box.append(el);
        bus.emit("message-rendered", { el, role: "user", content: m.content, created: m.created });
      } else if (m.role === "assistant") {
        if (!turn) { turn = newTurnEl(); box.append(turn.el); }
        if (m.backend && m.backend !== turn.backend) {
          if (turn.backend) turn.content.append(h("div", { class: "escalation" }, "↗", h("span", {}, `Escalated to ${BL(m.backend)}`)));
          setTurnMeta(turn, m.backend, m.model);
        }
        turn.model = m.model;
        if (m.content) { const md = startSegment(turn); turn.text = m.content; renderMD(md, m.content); turn.texts.push(m.content); turn.seg = null; }
        for (const tc of m.tool_calls || []) {
          if (tc.name === "escalate_to_cloud" || tc.name === "escalate_to_grok") continue; // shown as the escalation banner instead
          const c = toolCardEl(tc.name, tc.arguments, null); toolCards[tc.id] = c; turn.content.append(c);
        }
        turn.created = m.created;
      } else if (m.role === "tool") {
        const c = toolCards[m.tool_call_id];
        if (c) {
          const ok = !/^(Error|Blocked|The user denied)/.test(m.content) && !/\nResult: (FAILED|TIMED OUT)/.test(m.content);
          c.className = `tool-card ${ok ? "ok" : "fail"}`; c._parts.state.textContent = ok ? "done" : "failed";
          c._parts.res.textContent = m.content; c._parts.res.hidden = false;
        }
      }
    }
    if (turn) finishTurn(turn, { backend: turn.backend, model: turn.model });
    $$(".tool-card.running", box).forEach((c) => { c.className = "tool-card"; c._parts.state.textContent = "interrupted"; });
    if (!messages.length) box.append(welcomeEl());
    scrollDown(true);
  }

  const welcomeHTML = $("#welcome").outerHTML;
  function welcomeEl() {
    const d = h("div", { html: welcomeHTML }).firstElementChild;
    const proj = activeProject();
    if (S.kind === "code") {
      const name = proj ? proj.split(/[\\/]/).pop() : null;
      $("h2", d).textContent = name ? `What should we build in ${name}?` : "Code mode";
      $("p", d).innerHTML = name ? "Darkmoon reads the project, makes changes, runs the tests until they pass and shows you the diff. Nothing is committed unless you ask."
        : "Pick or add a project folder in the top bar to start. Darkmoon will edit code, run tests and show you every change.";
      const sug = $(".suggestions", d); sug.innerHTML = "";
      ["Give me a tour of this codebase", "Run the tests and fix anything that fails", "Find likely bugs and fix the worst one", "Add a feature: "]
        .forEach((t) => sug.append(h("button", {}, t)));
      $$(".suggestions button", d).forEach((b) => (b.onclick = () => b.textContent.endsWith(": ") ? (input.value = b.textContent, input.focus()) : sendMessage(b.textContent)));
      return d;
    }
    $$(".suggestions button", d).forEach((b) => (b.onclick = () => sendMessage(b.textContent)));
    return d;
  }

  // ── Server events ─────────────────────────────────────────────────────
  function handleEvent(ev) {
    const t = S.turn;
    switch (ev.type) {
      case "conversation":
        S.convId = ev.id; S.convProject = ev.project || null; renderKind();
        $("#conv-title").textContent = ev.title; refreshConvs(); break;
      case "user_saved": {
        const last = $$(".msg.user", msgsEl()).at(-1);
        if (last) { last.dataset.id = ev.id; last._msg.id = ev.id; }
        break;
      }
      case "route":
        if (!t) break;
        setTurnMeta(t, ev.backend, ev.model);
        t.content.append(h("div", { class: "route-note", html: `→ <b>${esc(ev.label)}</b>${ev.model ? " · " + esc(ev.model) : ""} — ${esc(ev.reason)}` }));
        setBadge(ev.backend, true, ev.label);
        break;
      case "token":
        if (!t) break;
        $(".typing", t.content)?.remove();
        if (!t.seg) startSegment(t);
        t.text += ev.text; renderSegThrottled(t); break;
      case "discard_stream":
        if (t?.seg) { t.seg.remove(); t.seg = null; t.text = ""; } break;
      case "assistant_step_end":
        if (t) endSegment(t); break;
      case "status":
        t?.content.append(h("div", { class: "status-note" }, "• " + ev.text)); scrollDown(); break;
      case "tool_start": {
        if (!t) break;
        endSegment(t);
        const c = toolCardEl(ev.name, ev.arguments, null);
        t.tools[ev.id] = c; t.currentTool = c; t.content.append(c); scrollDown(); break;
      }
      case "tool_output": {
        const c = t?.currentTool; if (!c) break;
        c._parts.live.hidden = false; c._parts.live.textContent += ev.text;
        c._parts.live.scrollTop = c._parts.live.scrollHeight; break;
      }
      case "tool_result": {
        const c = t?.tools[ev.id]; if (!c) break;
        c.className = `tool-card ${ev.ok ? "ok" : "fail"}`;
        c._parts.state.textContent = `${ev.ok ? "done" : "failed"} · ${ev.seconds}s`;
        c._parts.live.hidden = true; c._parts.res.textContent = ev.result; c._parts.res.hidden = false;
        t.content.append(h("div", { class: "typing" }, h("i"), h("i"), h("i")));
        scrollDown(); break;
      }
      case "escalated":
        if (!t) break;
        endSegment(t);
        $$(".segment", t.content).forEach((s) => s.classList.add("superseded"));
        $(".typing", t.content)?.remove();
        t.content.append(h("div", { class: "escalation" }, "↗",
          h("span", { html: `Escalated <b>${esc(BL(ev.from))}</b> → <b>${esc(BL(ev.to))}</b>: ${esc(ev.reason)}` })));
        t.texts = [];
        t.content.append(h("div", { class: "typing" }, h("i"), h("i"), h("i")));
        scrollDown(); break;
      case "approval_request":
        S.approvalQueue.push(ev); if (S.approvalQueue.length === 1) showApproval(ev); break;
      case "approval_resolved":
        break;
      case "done":
        if (t) finishTurn(t, ev);
        refreshConvs(); if (activeProject()) refreshChanges(); break;
      case "error":
        turnError(ev.message); break;
      case "stopped":
        if (t) { endSegment(t); t.content.append(h("div", { class: "status-note" }, "■ Stopped")); }
        break;
      case "idle":
        if (t) { endSegment(t); $(".typing", t.content)?.remove(); }
        S.turn = null; setBusy(false); break;
    }
  }

  function setBadge(backend, busy, label) {
    const b = $("#live-badge");
    b.className = `badge ${backend || "idle"} ${busy ? "busy" : ""}`;
    $("span", b).textContent = label || (backend ? BL(backend) : "Idle");
  }
  function setBusy(on) {
    S.busy = on;
    const btn = $("#send");
    btn.classList.toggle("stop", on); btn.textContent = on ? "■" : "➤"; btn.title = on ? "Stop (Esc)" : "Send (Enter)";
    if (!on) { setBadge(defaultBadgeBackend(), false); $("#input").focus(); }
  }
  function defaultBadgeBackend() { return S.mode === "auto" ? null : S.mode; }

  // ── Approvals ─────────────────────────────────────────────────────────
  const KIND_TEXT = { shell: ["⌨", "Run a shell command?"], write: ["💾", "Write to a file?"], python: ["🐍", "Run Python code?"], mcp: ["🔌", "Use a connector tool?"], tests: ["🧪", "Run the project's tests?"] };
  function showApproval(ev) {
    const [icon, title] = KIND_TEXT[ev.kind] || ["⚠", "Allow this action?"];
    $("#approval-icon").textContent = icon;
    $("#approval-title").textContent = title;
    $("#approval-kind").textContent = `Tool: ${ev.title} · in ${S.state?.config?.agent?.workspace_resolved || "workspace"}`;
    $("#approval-detail").textContent = ev.detail;
    $("#approval").hidden = false;
    $('#approval [data-decision="allow"]').focus();
  }
  function decide(decision) {
    const ev = S.approvalQueue.shift();
    if (ev) post("/api/approval", { id: ev.id, decision });
    $("#approval").hidden = true;
    if (S.approvalQueue.length) showApproval(S.approvalQueue[0]);
  }
  $$("#approval [data-decision]").forEach((b) => (b.onclick = () => decide(b.dataset.decision)));

  // ── Sending ───────────────────────────────────────────────────────────
  function sendMessage(text) {
    text = (text ?? $("#input").value).trim();
    if (!text || S.busy) return;
    // client-side slash commands
    const m = text.match(/^\/(\S+)\s*([\s\S]*)$/);
    if (m) {
      const cmd = Plugins.cmds.get(m[1]);
      if (cmd && cmd.local !== false) {
        $("#input").value = ""; autosize(); hideSlash();
        Promise.resolve(cmd.run(m[2], { send: sendMessage, toast })).catch((e) => toast(e.message, "error"));
        return;
      }
    }
    if (S.kind === "code" && !S.convProject && !S.project) {
      toast("Pick a project folder for Code mode first.");
      addProject();
      return;
    }
    $("#welcome")?.remove();
    const u = userMsgEl(text); msgsEl().append(u);
    bus.emit("message-rendered", { el: u, role: "user", content: text, created: Date.now() / 1000, live: true });
    S.turn = newTurnEl();
    S.turn.content.append(h("div", { class: "typing" }, h("i"), h("i"), h("i")));
    msgsEl().append(S.turn.el);
    $("#input").value = ""; autosize(); hideSlash();
    stickBottom = true; scrollDown(true);
    setBusy(true); setBadge(S.mode === "auto" ? "ollama" : S.mode, true, "Routing…");
    bus.emit("send", { text });
    post("/api/chat", { conversation_id: S.convId, text, mode: S.mode, model: S.model,
                        project: !S.convId && S.kind === "code" ? S.project : null })
      .catch((e) => { turnError(e.message); S.turn = null; setBusy(false); });
  }
  function stop() { post("/api/stop", {}); }

  async function editMessage(el, text) {
    if (S.busy) return;
    const id = el.dataset.id;
    $("#input").value = text; autosize(); $("#input").focus();
    if (id && S.convId) {
      await api(`/api/conversations/${S.convId}/truncate`, { method: "POST", body: { message_id: Number(id) } });
      let n = el; while (n) { const next = n.nextElementSibling; n.remove(); n = next; }
    }
  }
  async function regenerate(prefix = "") {
    if (S.busy) return;
    const lastUser = $$(".msg.user", msgsEl()).at(-1);
    if (!lastUser) return;
    const text = lastUser._msg.content.replace(/^\/(grok|local|api)\s+/, "");
    await editMessage(lastUser, text);
    sendMessage(prefix + text);
  }

  // ── Composer ──────────────────────────────────────────────────────────
  const input = $("#input");
  function autosize() { input.style.height = "auto"; input.style.height = Math.min(input.scrollHeight, innerHeight * 0.4) + "px"; bus.emit("input", input.value); }
  input.addEventListener("input", () => { autosize(); updateSlash(); });
  input.addEventListener("keydown", (e) => {
    if (!$("#slash-menu").hidden) {
      const items = $$("#slash-menu div"); let i = items.findIndex((x) => x.classList.contains("sel"));
      if (e.key === "ArrowDown" || e.key === "ArrowUp") { e.preventDefault(); items[i]?.classList.remove("sel"); i = (i + (e.key === "ArrowDown" ? 1 : -1) + items.length) % items.length; items[i].classList.add("sel"); return; }
      if ((e.key === "Tab" || e.key === "Enter") && i >= 0) { e.preventDefault(); items[i].click(); return; }
      if (e.key === "Escape") { hideSlash(); return; }
    }
    const sendKey = S.ui.send_on_enter !== false ? (e.key === "Enter" && !e.shiftKey) : (e.key === "Enter" && (e.ctrlKey || e.metaKey));
    if (sendKey && !e.isComposing) { e.preventDefault(); sendMessage(); }
  });
  $("#send").onclick = () => (S.busy ? stop() : sendMessage());

  const BUILTIN_PREFIXES = [
    { name: "grok", description: "Send this message to Grok (CLI subscription, else API)", prefix: true },
    { name: "claude", description: "Send this message to Claude (Claude Code subscription, else API)", prefix: true },
    { name: "gpt", description: "Send this message to your OpenAI-compatible model", prefix: true },
    { name: "local", description: "Force local Darkmoon", prefix: true },
    { name: "api", description: "Send this message to the xAI API", prefix: true },
  ];
  function allCommands() { return [...BUILTIN_PREFIXES, ...Plugins.cmds.values()]; }
  function updateSlash() {
    const v = input.value;
    const m = v.match(/^\/(\S*)$/);
    if (!m) return hideSlash();
    const items = allCommands().filter((c) => c.name.startsWith(m[1]));
    if (!items.length) return hideSlash();
    const menu = $("#slash-menu"); menu.innerHTML = "";
    items.forEach((c, i) => menu.append(h("div", { class: i === 0 ? "sel" : "", onclick: () => { input.value = `/${c.name} `; hideSlash(); input.focus(); autosize(); } },
      h("b", {}, "/" + c.name), h("span", {}, c.description || ""))));
    menu.hidden = false;
  }
  function hideSlash() { $("#slash-menu").hidden = true; }

  // ── Conversations sidebar ─────────────────────────────────────────────
  async function refreshConvs() {
    S.convs = await api(`/api/conversations?q=${encodeURIComponent($("#search").value)}`);
    const list = $("#conv-list"); list.innerHTML = "";
    const now = new Date(); const day = 86400;
    const startToday = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime() / 1000;
    const group = (c) => c.pinned ? "Pinned" : c.updated >= startToday ? "Today" : c.updated >= startToday - day ? "Yesterday" : c.updated >= startToday - 7 * day ? "Previous 7 days" : "Older";
    let last = null;
    for (const c of S.convs) {
      const g = group(c);
      if (g !== last) { list.append(h("div", { class: "conv-group" }, g)); last = g; }
      list.append(h("div", { class: `conv ${c.id === S.convId ? "active" : ""}`, onclick: () => { openConv(c.id); closeDrawer(); }, title: c.title + (c.project ? `\n${c.project}` : "") },
        c.project ? h("span", { class: "proj" }, "‹/› " + c.project.split(/[\\/]/).pop()) : "",
        h("span", { class: "t" }, c.title),
        h("span", { class: "acts" },
          h("button", { title: c.pinned ? "Unpin" : "Pin", onclick: async (e) => { e.stopPropagation(); await api(`/api/conversations/${c.id}`, { method: "PATCH", body: { pinned: !c.pinned } }); refreshConvs(); } }, c.pinned ? "★" : "☆"),
          h("button", { title: "Delete", onclick: async (e) => { e.stopPropagation(); if (!confirm(`Delete "${c.title}"?`)) return; await api(`/api/conversations/${c.id}`, { method: "DELETE" }); if (c.id === S.convId) newChat(); refreshConvs(); } }, "🗑"))));
    }
    if (!S.convs.length) list.append(h("div", { class: "conv-group" }, $("#search").value ? "No matches" : "No chats yet"));
  }
  $("#search").addEventListener("input", debounce(refreshConvs, 200));

  async function openConv(id) {
    if (S.busy) return toast("Wait for the current reply to finish (or press Stop).");
    const data = await api(`/api/conversations/${id}`);
    S.convId = id; $("#conv-title").textContent = data.conversation.title;
    S.convProject = data.conversation.meta?.project || null;
    if (S.convProject) { S.kind = "code"; }
    renderKind();
    renderHistory(data.messages); refreshConvs(); bus.emit("conversation-opened", { id });
  }
  function newChat() {
    if (S.busy) return;
    S.convId = null; S.convProject = null; $("#conv-title").textContent = "New chat";
    renderKind();
    renderHistory([]); refreshConvs(); input.focus(); bus.emit("conversation-opened", { id: null });
  }
  $("#new-chat").onclick = newChat;

  $("#conv-title").addEventListener("click", function () {
    if (!S.convId) return;
    this.contentEditable = "true"; this.focus();
    document.getSelection().selectAllChildren(this);
  });
  $("#conv-title").addEventListener("keydown", function (e) { if (e.key === "Enter") { e.preventDefault(); this.blur(); } });
  $("#conv-title").addEventListener("blur", async function () {
    if (this.contentEditable !== "true") return;
    this.contentEditable = "false";
    const title = this.textContent.trim();
    if (S.convId && title) { await api(`/api/conversations/${S.convId}`, { method: "PATCH", body: { title } }); refreshConvs(); }
  });

  // ── Top bar: mode + model ─────────────────────────────────────────────
  function available(b) { return b === "ollama" || !!S.state?.health?.[b]?.ok; }
  function renderModes() {
    const sel = $("#mode-select");
    if (!sel) return;
    const h_ = S.state?.health || {};
    sel.innerHTML = "";
    sel.append(h("option", { value: "auto", selected: S.mode === "auto" }, "Auto — Darkmoon first"));
    sel.append(h("option", { value: "ollama", selected: S.mode === "ollama" }, "Darkmoon (local only)"));
    const grp = h("optgroup", { label: "Cloud" });
    for (const b of CLOUD) {
      const ok = available(b);
      grp.append(h("option", { value: b, selected: S.mode === b, disabled: !ok && S.mode !== b },
        `${BL(b)}${ok ? "" : " — " + (h_[b]?.detail || "not set up")}`));
    }
    sel.append(grp);
    $("#mode-dot").style.background = S.mode === "auto" ? "var(--accent)" : `var(${{ ollama: "--local-color", grok_cli: "--grok-color",
      xai_api: "--api-color", claude_cli: "--claude-color", claude_api: "--claude-api-color", openai_compat: "--openai-color" }[S.mode] || "--accent"})`;
  }
  $("#mode-select").onchange = (e) => {
    S.mode = e.target.value; localStorage.setItem("la.mode", S.mode); renderModes();
    if (!S.busy) setBadge(defaultBadgeBackend(), false);
    bus.emit("mode", S.mode);
  };
  function cloudAskButtons() {
    const out = [];
    if (available("grok_cli") || available("xai_api")) out.push(h("button", { title: "Send this question to Grok", onclick: () => regenerate("/grok ") }, "Ask Grok"));
    if (available("claude_cli") || available("claude_api")) out.push(h("button", { title: "Send this question to Claude", onclick: () => regenerate("/claude ") }, "Ask Claude"));
    if (available("openai_compat")) out.push(h("button", { title: `Send this question to ${BL("openai_compat")}`, onclick: () => regenerate("/gpt ") }, `Ask ${BL("openai_compat")}`));
    return out;
  }
  function renderModelSelect() {
    const sel = $("#model-select");
    const models = S.state?.health?.ollama?.models || [];
    const current = S.model || S.state?.config?.ollama?.model;
    sel.innerHTML = "";
    const names = [...new Set([current, ...models].filter(Boolean))];
    names.forEach((n) => sel.append(h("option", { value: n, selected: n === current }, n + (models.includes(n) || models.includes(n + ":latest") ? "" : n === "darkmoon" ? " (not built yet)" : " (not pulled)"))));
    sel.append(h("option", { value: "__manage" }, "Manage models…"));
  }
  $("#model-select").onchange = (e) => {
    if (e.target.value === "__manage") { renderModelSelect(); return openSettings("backends"); }
    S.model = e.target.value; localStorage.setItem("la.model", S.model);
    toast(`Local model: ${S.model}`);
  };

  function renderStatus() {
    const box = $("#backend-status"); box.innerHTML = "";
    const h_ = S.state?.health || {};
    for (const k of BACKENDS) {
      if (k !== "ollama" && !S.state.config[k]?.enabled) continue;
      const st = h_[k] || {};
      box.append(h("span", { class: `dot-pill ${st.ok ? "ok" : "bad"}`, title: `${BL(k)}: ${st.detail || ""}` }, h("i"), k === "ollama" ? "Darkmoon" : BL(k)));
    }
    const conns = (S.state.mcp || []).filter((c) => c.enabled !== false);
    if (conns.length) {
      const up = conns.filter((c) => c.status === "connected").length;
      box.append(h("span", { class: `dot-pill ${up === conns.length ? "ok" : "bad"}`, title: conns.map((c) => `${c.name}: ${c.status}`).join("\n") }, h("i"), `🔌 ${up}/${conns.length}`));
    }
  }
  $("#backend-status").onclick = async () => { await loadState(true); toast("Backend status refreshed"); };

  async function loadState(force = false) {
    S.state = await api(`/api/state${force ? "?force=1" : ""}`);
    S.ui = S.state.ui;
    S.model = localStorage.getItem("la.model") || "";
    if (S.model && !(S.state.health.ollama?.models || []).includes(S.model)) S.model = "";
    renderModes(); renderModelSelect(); renderStatus();
    $("#brand-sub").textContent = `v${S.state.version} · ${S.state.health.ollama?.ok ? "online" : "Ollama offline"}`;
  }

  // ── Sidebar collapse ──────────────────────────────────────────────────
  const isMobile = () => matchMedia("(max-width: 900px)").matches;
  function closeDrawer() { document.body.classList.remove("drawer-open"); $("#drawer-backdrop").hidden = true; }
  $("#collapse-btn").onclick = () => setUI({ sidebar_collapsed: true });
  $("#expand-btn").onclick = () => {
    if (isMobile()) { document.body.classList.add("drawer-open"); $("#drawer-backdrop").hidden = false; }
    else setUI({ sidebar_collapsed: false });
  };
  $("#drawer-backdrop").onclick = () => { closeDrawer(); closeChanges(); };
  $("#new-chat").addEventListener("click", closeDrawer);
  msgsEl().addEventListener("scroll", () => { const el = msgsEl(); stickBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 80; });

  // ── Code mode ─────────────────────────────────────────────────────────
  const activeProject = () => (S.kind === "code" ? (S.convProject || S.project || "") : "");
  const baseName = (p) => (p || "").split(/[\\/]/).filter(Boolean).pop() || p;
  async function loadProjects() {
    try { S.projects = (await api("/api/code/projects")).projects; } catch { S.projects = []; }
    if (S.project && !S.projects.some((p) => p.path === S.project)) S.project = "";
    if (!S.project && S.projects.length) S.project = S.projects[0].path;
    renderKind();
  }
  function renderKind() {
    document.body.classList.toggle("kind-code", S.kind === "code");
    $$("#kind-seg button").forEach((b) => b.classList.toggle("active", b.dataset.kind === S.kind));
    const locked = !!S.convId;  // a chat keeps its mode/project
    $$("#kind-seg button").forEach((b) => (b.disabled = locked && b.dataset.kind !== S.kind));
    $("#project-pick").hidden = S.kind !== "code";
    const sel = $("#project-select"); sel.innerHTML = "";
    const cur = activeProject();
    for (const p of S.projects) sel.append(h("option", { value: p.path, selected: p.path === cur, title: p.path }, p.name + (p.branch ? `  ·  ${p.branch}` : "")));
    if (S.convProject && !S.projects.some((p) => p.path === S.convProject)) sel.append(h("option", { value: S.convProject, selected: true }, baseName(S.convProject)));
    if (!locked || !S.convProject) sel.append(h("option", { value: "__add" }, "＋ Add project folder…"));
    sel.disabled = !!S.convProject;
    $("#changes-btn").hidden = !(S.kind === "code" && cur);
    if (S.kind === "code" && cur) refreshChanges();
    if (!S.convId && $("#welcome")) { const w = $("#welcome"); w.replaceWith(welcomeEl()); }
    input.placeholder = S.kind === "code" ? `Tell Darkmoon what to change in ${baseName(cur) || "your project"}…` : "Message Darkmoon…  (/grok · /claude · /gpt to use the cloud)";
  }
  $$("#kind-seg button").forEach((b) => (b.onclick = () => {
    if (S.convId) return;
    S.kind = b.dataset.kind; localStorage.setItem("la.kind", S.kind); renderKind();
    if (S.kind === "code" && !S.projects.length) addProject();
  }));
  $("#project-select").onchange = (e) => {
    if (e.target.value === "__add") { renderKind(); return addProject(); }
    S.project = e.target.value; localStorage.setItem("la.project", S.project); renderKind();
  };
  async function addProject() {
    let path = null;
    if (window.pywebview?.api?.pick_folder) {
      try { path = await window.pywebview.api.pick_folder(); } catch { path = null; }
    }
    if (!path) path = prompt("Full path of the project folder (e.g. C:\\Users\\you\\code\\my-app):");
    if (!path) return;
    try {
      const r = await api("/api/code/projects", { method: "POST", body: { path } });
      S.project = r.path; localStorage.setItem("la.project", S.project);
      S.kind = "code"; localStorage.setItem("la.kind", "code");
      await loadProjects(); toast(`Project added: ${baseName(r.path)}`, "success");
    } catch (e) { toast(e.message, "error"); }
  }

  let lastChanges = null, selFile = null;
  async function refreshChanges() {
    const proj = activeProject(); if (!proj) return;
    try { lastChanges = await api(`/api/code/changes?path=${encodeURIComponent(proj)}`); } catch { lastChanges = null; return; }
    const n = lastChanges.files.length;
    $("#changes-count").textContent = lastChanges.is_git ? n : "–";
    $("#changes-btn").classList.toggle("has", n > 0);
    if (!$("#changes-panel").hidden) renderChanges();
  }
  function openChanges() {
    $("#changes-panel").hidden = false;
    if (isMobile()) $("#drawer-backdrop").hidden = false;
    renderChanges(); refreshChanges();
  }
  function closeChanges() { $("#changes-panel").hidden = true; if (!document.body.classList.contains("drawer-open")) $("#drawer-backdrop").hidden = true; }
  $("#changes-btn").onclick = openChanges;
  $("#cp-close").onclick = closeChanges;
  $("#cp-refresh").onclick = refreshChanges;
  function renderDiff(text, onlyFile) {
    const box = $("#cp-diff"); box.innerHTML = "";
    if (!text.trim()) { box.append(h("div", { class: "empty" }, "No uncommitted changes.")); return; }
    let show = !onlyFile;
    for (const line of text.split("\n")) {
      if (line.startsWith("diff --git")) {
        const f = line.split(" b/").pop();
        show = !onlyFile || f === onlyFile;
        if (show) box.append(h("div", { class: "dl file" }, f));
        continue;
      }
      if (!show || /^(index |--- |\+\+\+ |new file|deleted file|similarity|rename )/.test(line)) continue;
      const cls = line.startsWith("+") ? "add" : line.startsWith("-") ? "del" : line.startsWith("@@") ? "hunk" : "";
      box.append(h("div", { class: `dl ${cls}` }, line || " "));
    }
  }
  function renderChanges() {
    const c = lastChanges; const proj = activeProject();
    $("#cp-project").textContent = baseName(proj);
    const files = $("#cp-files"); files.innerHTML = "";
    if (!c) { $("#cp-branch").textContent = ""; return; }
    if (!c.is_git) {
      $("#cp-branch").textContent = "not a git repository";
      files.append(h("div", { class: "cp-diff" }, h("div", { class: "empty" }, "Darkmoon tracks changes with git. ",
        h("button", { class: "primary-btn", style: "margin-top:10px", onclick: () => codeAction("git-init") }, "Initialize git here"))));
      $("#cp-diff").innerHTML = "";
      return;
    }
    $("#cp-branch").textContent = `⎇ ${c.branch} · ${c.files.length} changed`;
    const map = { added: "A", deleted: "D", modified: "M", renamed: "R" };
    for (const f of c.files) {
      files.append(h("div", { class: `cp-file ${selFile === f.path ? "sel" : ""}`, onclick: () => { selFile = selFile === f.path ? null : f.path; renderChanges(); } },
        h("span", { class: `st ${f.status}` }, map[f.status] || "M"), h("span", { class: "fp", title: f.path }, f.path),
        h("span", { class: "ad" }, `+${f.added}`), h("span", { class: "de" }, `−${f.deleted}`)));
    }
    renderDiff(c.diff, selFile);
  }
  async function codeAction(action, extra = {}) {
    const proj = activeProject(); if (!proj) return;
    try {
      const r = await api(`/api/code/${action}`, { method: "POST", body: { path: proj, ...extra } });
      toast(r.message || "Done", "success");
      if (r.url) window.open(r.url, "_blank");
    } catch (e) { toast(e.message, "error"); }
    await refreshChanges(); loadProjects();
  }
  $("#cp-commit").onclick = () => {
    const m = $("#cp-message").value.trim();
    if (!lastChanges?.files.length) return toast("Nothing to commit");
    codeAction("commit", { message: m || "Update from Darkmoon" }).then(() => ($("#cp-message").value = ""));
  };
  $("#cp-branch-btn").onclick = () => { const n = prompt("New branch name:", "feature/"); if (n) codeAction("branch", { name: n.trim() }); };
  $("#cp-pr").onclick = () => {
    const t = prompt("Pull request title:", $("#cp-message").value || $("#conv-title").textContent);
    if (t) codeAction("pr", { title: t, body: `Created with Darkmoon.\n\nConversation: ${$("#conv-title").textContent}` });
  };
  $("#cp-discard").onclick = () => {
    if (!confirm("Revert all uncommitted changes to tracked files?\n\nNew (untracked) files are kept. This can't be undone.")) return;
    codeAction("discard");
  };

  // ── Settings ──────────────────────────────────────────────────────────
  const THEME_VARS = ["--bg", "--bg-elev", "--bg-elev-2", "--bg-hover", "--sidebar-bg", "--text", "--text-muted", "--text-faint", "--border",
    "--accent", "--accent-2", "--accent-contrast", "--user-bubble", "--user-text", "--bot-bubble", "--code-bg", "--inline-code-bg",
    "--success", "--warning", "--danger", "--local-color", "--grok-color", "--api-color", "--font", "--font-mono", "--font-size",
    "--radius", "--chat-width", "--shadow", "--glass", "--hl-keyword", "--hl-string", "--hl-number", "--hl-comment", "--hl-function"];

  function openSettings(tab = "appearance") {
    $("#settings").hidden = false;
    $$(".settings-nav button").forEach((b) => b.classList.toggle("active", b.dataset.tab === tab));
    renderTab(tab);
  }
  function closeSettings() { $("#settings").hidden = true; }
  $("#settings-btn").onclick = () => openSettings();
  $(".settings-close").onclick = closeSettings;
  $("#settings").addEventListener("mousedown", (e) => { if (e.target.id === "settings") closeSettings(); });
  $$(".settings-nav button").forEach((b) => (b.onclick = () => openSettings(b.dataset.tab)));

  // form helpers --------------------------------------------------------
  function toggleRow(title, sub, checked, onchange, path) {
    const inp = h("input", { type: "checkbox", checked, dataset: path ? { path, type: "bool" } : {} });
    if (onchange) inp.onchange = () => onchange(inp.checked);
    return h("label", { class: "toggle-row" }, h("div", { class: "tr-text" }, h("b", {}, title), sub ? h("small", {}, sub) : ""),
      h("span", { class: "switch" }, inp, h("span")));
  }
  function field(label, control, help) {
    return h("div", { class: "field" }, h("label", {}, label), control, help ? h("div", { class: "help", html: help }) : "");
  }
  function cfgInput(path, type = "str", attrs = {}) {
    let v = get(S.state.config, path);
    if (type === "list") v = (v || []).join(", ");
    if (type === "lines") v = (v || []).join("\n");
    if (type === "lines" || type === "text") return h("textarea", { dataset: { path, type }, ...attrs }, v ?? "");
    return h("input", { value: v ?? "", dataset: { path, type }, type: type === "number" ? "number" : attrs.type || "text", ...attrs });
  }
  function cfgSelect(path, options) {
    const v = get(S.state.config, path);
    return h("select", { dataset: { path, type: "str" } }, options.map(([val, label]) => h("option", { value: val, selected: String(v) === String(val) }, label)));
  }
  async function saveForm(root, { quiet = false } = {}) {
    const patch = {};
    $$("[data-path]", root).forEach((el) => {
      const t = el.dataset.type; let v = el.type === "checkbox" ? el.checked : el.value;
      if (t === "number") v = Number(v);
      if (t === "list") v = v.split(",").map((s) => s.trim()).filter(Boolean);
      if (t === "lines") v = v.split("\n").map((s) => s.trim()).filter(Boolean);
      if (t === "json") { try { v = JSON.parse(v || "[]"); } catch { toast(`Invalid JSON in ${el.dataset.path}`, "error"); throw new Error("bad json"); } }
      setPath(patch, el.dataset.path, v);
    });
    try {
      await api("/api/config", { method: "POST", body: patch });
      await loadState(true);
      if (!quiet) toast("Saved", "success");
    } catch (e) { toast(e.message, "error"); }
  }
  const saveBtn = (root) => h("div", { class: "row", style: "margin-top:18px" }, h("button", { class: "primary-btn", onclick: () => saveForm(root) }, "Save changes"));
  const statusLine = (st) => h("span", { class: `status-line ${st?.ok ? "ok" : "bad"}` }, h("i"), st?.detail || "unknown");
  const openFolder = (which) => api("/api/open-folder", { method: "POST", body: { which } }).then((r) => !r.ok && toast(`Open manually: ${r.path}`));

  const TABS = {
    appearance() {
      const u = S.ui;
      const range = (key, min, max, step, unit = "") => {
        const out = h("span", { style: "min-width:52px;text-align:right;color:var(--text-muted);font-size:13px" }, `${u[key]}${unit}`);
        const r = h("input", { type: "range", min, max, step, value: u[key] });
        r.oninput = () => { out.textContent = `${r.value}${unit}`; setUI({ [key]: Number(r.value) }); };
        return h("div", { class: "row", style: "flex-wrap:nowrap" }, r, out);
      };
      const accent = h("input", { type: "color", value: u.accent || getComputedStyle(document.documentElement).getPropertyValue("--accent").trim() || "#7c5cff" });
      accent.oninput = () => setUI({ accent: accent.value });
      const fontIn = h("input", { value: u.font || "", placeholder: "Theme default", list: "font-list" });
      fontIn.onchange = () => setUI({ font: fontIn.value.trim() });
      const monoIn = h("input", { value: u.mono_font || "", placeholder: "Theme default", list: "mono-list" });
      monoIn.onchange = () => setUI({ mono_font: monoIn.value.trim() });
      const density = h("select", {}, ["compact", "comfortable", "cozy"].map((d) => h("option", { value: d, selected: u.density === d }, d)));
      density.onchange = () => setUI({ density: density.value });
      const bg = h("input", { value: u.bg_image?.startsWith("data:") ? "(local image)" : u.bg_image || "", placeholder: "https://… image URL" });
      bg.onchange = () => setUI({ bg_image: bg.value.trim() });
      const file = h("input", { type: "file", accept: "image/*", hidden: true });
      file.onchange = () => { const f = file.files[0]; if (!f) return; if (f.size > 6e6) return toast("Image too large (max 6 MB)", "error");
        const rd = new FileReader(); rd.onload = () => { setUI({ bg_image: rd.result }); bg.value = "(local image)"; }; rd.readAsDataURL(f); };
      const avU = h("input", { value: u.avatar_user, style: "width:70px" }); avU.onchange = () => setUI({ avatar_user: avU.value });
      const avB = h("input", { value: u.avatar_bot, style: "width:70px" }); avB.onchange = () => setUI({ avatar_bot: avB.value });
      return [
        h("h2", {}, "Appearance"), h("p", { class: "lead" }, "Tweaks apply instantly on top of the selected theme."),
        h("datalist", { id: "font-list" }, ["Inter", "Segoe UI", "Roboto", "Nunito", "IBM Plex Sans", "Atkinson Hyperlegible", "Georgia", "Comic Sans MS"].map((f) => h("option", { value: f }))),
        h("datalist", { id: "mono-list" }, ["JetBrains Mono", "Cascadia Code", "Fira Code", "Consolas", "IBM Plex Mono"].map((f) => h("option", { value: f }))),
        h("div", { class: "grid2" },
          field("Accent color", h("div", { class: "row" }, accent, h("button", { class: "ghost-btn", onclick: () => { setUI({ accent: "" }); renderTab("appearance"); } }, "Use theme's"))),
          field("Message density", density),
          field("Font", fontIn, "Any font installed on your PC."), field("Code font", monoIn),
          field("Font size", range("font_size", 12, 20, 1, "px")), field("Corner roundness", range("radius", 0, 24, 1, "px")),
          field("Chat width", range("chat_width", 600, 1500, 20, "px")),
          field("Avatars", h("div", { class: "row" }, avU, avB, h("small", { style: "color:var(--text-muted)" }, "emoji or text")))),
        h("h3", {}, "Background"),
        field("Background image", h("div", { class: "row", style: "flex-wrap:nowrap" }, bg, h("button", { class: "ghost-btn", onclick: () => file.click() }, "Browse…"),
          h("button", { class: "ghost-btn", onclick: () => { setUI({ bg_image: "" }); bg.value = ""; } }, "Clear"), file)),
        h("div", { class: "grid2" }, field("Blur", range("bg_blur", 0, 30, 1, "px")), field("Dim", range("bg_dim", 0, 1, 0.05))),
        h("h3", {}, "Behavior"),
        toggleRow("Show avatars", "", u.show_avatars !== false, (v) => setUI({ show_avatars: v })),
        toggleRow("Show tool details", "Arguments and output inside tool cards", u.show_tool_details, (v) => setUI({ show_tool_details: v })),
        toggleRow("Expand model thinking", "Show <think> blocks from reasoning models open by default", u.show_thinking, (v) => setUI({ show_thinking: v })),
        toggleRow("Enter sends", "Off = Ctrl+Enter sends, Enter adds a new line", u.send_on_enter !== false, (v) => setUI({ send_on_enter: v })),
      ];
    },

    themes() {
      const grid = h("div", { class: "theme-grid" });
      const cards = S.state.themes.map((t) => {
        const preview = h("div", { class: "theme-preview" });
        const card = h("div", { class: `theme-card ${S.ui.theme === t.file.replace(/\.css$/, "") ? "active" : ""}`, title: t.description,
          onclick: () => { setUI({ theme: t.file.replace(/\.css$/, "") }); $$(".theme-card", grid).forEach((c) => c.classList.remove("active")); card.classList.add("active"); } },
          preview, h("div", { class: "theme-info" }, h("b", {}, t.name, t.source === "user" ? h("span", { class: "tag" }, "yours") : ""),
            h("small", {}, (t.author ? `by ${t.author} · ` : "") + (t.description || ""))),
          t.source === "user" ? h("button", { class: "icon-btn del", title: "Delete theme", onclick: async (e) => { e.stopPropagation(); if (!confirm(`Delete ${t.name}?`)) return; await api(`/api/themes/${t.file}`, { method: "DELETE" }); await loadState(); renderTab("themes"); } }, "🗑") : "");
        fetch(t.url).then((r) => r.text()).then((css) => {
          const v = (name, d) => (css.match(new RegExp(`${name}\\s*:\\s*([^;]+);`)) || [])[1]?.trim() || d;
          const bgc = v("--bg", "#0f1117"), side = v("--sidebar-bg", bgc), acc = v("--accent", "#7c5cff"), txt = v("--text", "#ddd"), ub = v("--user-bubble", "#333");
          preview.innerHTML = `<div class="tp-side" style="background:${esc(side)}"><i style="background:${esc(txt)}"></i><i style="background:${esc(txt)}"></i><i style="background:${esc(txt)};width:60%"></i></div>
            <div class="tp-main" style="background:${esc(bgc)}"><div class="tp-bubble" style="background:${esc(ub)}"></div><div class="tp-line" style="background:${esc(txt)}"></div><div class="tp-line" style="background:${esc(txt)};width:70%"></div><div class="tp-btn" style="background:${esc(acc)}"></div></div>`;
        });
        return card;
      });
      grid.append(...cards);
      const url = h("input", { placeholder: "https://raw.githubusercontent.com/…/my-theme.css" });
      const file = h("input", { type: "file", accept: ".css,text/css", hidden: true });
      const install = async (body) => { try { const r = await api("/api/themes/install", { method: "POST", body }); toast(`Installed ${r.file}`, "success"); await loadState(); renderTab("themes"); } catch (e) { toast(e.message, "error"); } };
      file.onchange = () => { const f = file.files[0]; if (f) f.text().then((css) => install({ css, name: f.name })); };
      const dup = async () => {
        const cur = S.state.themes.find((t) => t.file.replace(/\.css$/, "") === S.ui.theme) || S.state.themes[0];
        const name = prompt("Name for your copy:", `My ${cur.name}`); if (!name) return;
        let css = await (await fetch(cur.url)).text();
        css = css.replace(/@name\s+[^\n*]+/, `@name ${name}`).replace(/@author\s+[^\n*]+/, "@author you");
        await install({ css, name });
      };
      return [
        h("h2", {}, "Themes"), h("p", { class: "lead" }, "Pick a theme, or drop .css files into your themes folder. Themes are plain CSS that set variables — see README."),
        grid,
        h("h3", {}, "Get more"),
        field("Install from URL", h("div", { class: "row", style: "flex-wrap:nowrap" }, url, h("button", { class: "primary-btn", onclick: () => url.value && install({ url: url.value }) }, "Install")),
          "GitHub file links are converted to raw automatically. Only CSS is accepted."),
        h("div", { class: "row" }, h("button", { class: "ghost-btn", onclick: () => file.click() }, "Import .css file…"), file,
          h("button", { class: "ghost-btn", onclick: dup }, "Duplicate current theme"),
          h("button", { class: "ghost-btn", onclick: () => openFolder("themes") }, "Open themes folder"),
          h("button", { class: "ghost-btn", onclick: async () => { await loadState(); renderTab("themes"); applyAppearance(); } }, "Reload")),
      ];
    },

    quickcss() {
      const ed = h("textarea", { id: "quickcss-editor", spellcheck: "false" });
      api("/api/quickcss").then((css) => (ed.value = css));
      const save = debounce(() => api("/api/quickcss", { method: "POST", body: { css: ed.value } }), 400);
      ed.addEventListener("input", () => { if (S.ui.quickcss_enabled) $("#quick-css").textContent = ed.value; save(); });
      ed.addEventListener("keydown", (e) => { if (e.key === "Tab") { e.preventDefault(); const s = ed.selectionStart; ed.setRangeText("  ", s, ed.selectionEnd, "end"); ed.dispatchEvent(new Event("input")); } });
      const vars = h("div", { class: "var-list" }, THEME_VARS.map((v) => h("code", { title: "Insert", onclick: () => { ed.setRangeText(`${v}: ;`, ed.selectionStart, ed.selectionEnd, "end"); ed.focus(); } }, v)));
      return [
        h("h2", {}, "QuickCSS"), h("p", { class: "lead" }, "Your own CSS, applied live on top of any theme. Saved automatically."),
        toggleRow("Enable QuickCSS", "", S.ui.quickcss_enabled, (v) => { setUI({ quickcss_enabled: v }); applyQuickCSS(); }),
        h("div", { style: "height:12px" }), ed,
        h("h3", {}, "Theme variables"), h("p", { class: "lead", style: "margin:0" }, "Click to insert. Example: ", h("code", {}, ":root { --accent: hotpink; --radius: 4px; }")), vars,
        h("h3", {}, "Handy selectors"),
        h("p", { class: "lead", html: "<code>#sidebar</code> <code>#topbar</code> <code>.msg.user .bubble</code> <code>.msg.assistant</code> <code>.tool-card</code> <code>#composer</code> <code>.chip.ollama</code> <code>.chip.grok_cli</code> <code>.code-wrap</code>" }),
      ];
    },

    plugins() {
      const rows = S.state.ui_plugins.map((p) => {
        const on = (S.ui.enabled_plugins || []).includes(p.file);
        const inp = h("input", { type: "checkbox", checked: on });
        inp.onchange = () => Plugins.toggle(p, inp.checked);
        return h("div", { class: "plugin-row" },
          h("div", { class: "pr-text" }, h("b", {}, p.name), p.source === "user" ? h("span", { class: "tag warn" }, "third-party") : h("span", { class: "tag" }, "built-in"),
            p.version ? h("span", { class: "tag" }, "v" + p.version) : "",
            h("small", {}, (p.description || "") + (p.author ? ` — by ${p.author}` : ""))),
          h("label", { class: "switch" }, inp, h("span")));
      });
      return [
        h("h2", {}, "UI Plugins"),
        h("p", { class: "lead" }, "Small scripts that extend the interface — slash commands, buttons, widgets. Drop .js files into your UI plugins folder."),
        ...rows,
        h("div", { class: "error-box", style: "background:color-mix(in srgb,var(--warning) 10%,transparent);border-color:color-mix(in srgb,var(--warning) 40%,transparent)" },
          "Third-party plugins run with full access to this app (including sending messages and changing settings). Only install plugins you've read or trust."),
        h("div", { class: "row", style: "margin-top:12px" },
          h("button", { class: "ghost-btn", onclick: () => openFolder("ui_plugins") }, "Open UI plugins folder"),
          h("button", { class: "ghost-btn", onclick: () => location.reload() }, "Reload app")),
      ];
    },

    backends() {
      const root = h("div");
      const hl = S.state.health;
      const C = S.state.config;
      const streamNDJSON = async (resp, onEv) => {
        const reader = resp.body.getReader(); const dec = new TextDecoder(); let buf = "";
        for (;;) {
          const { done, value } = await reader.read(); if (done) break;
          buf += dec.decode(value, { stream: true });
          const lines = buf.split("\n"); buf = lines.pop();
          for (const l of lines) { try { onEv(JSON.parse(l)); } catch { /* */ } }
        }
      };
      const prog = h("div", { class: "progress", hidden: true }, h("div"));
      const progText = h("div", { class: "help" });
      const showEv = (j) => {
        if (j.error) { progText.textContent = "Error: " + j.error; return; }
        if (j.total) { $("div", prog).style.width = `${(100 * (j.completed || 0)) / j.total}%`; progText.textContent = `${j.status} — ${((j.completed || 0) / 1e9).toFixed(2)} / ${(j.total / 1e9).toFixed(2)} GB`; }
        else progText.textContent = j.status || "";
      };
      const pull = async (name) => {
        name = name || pullName.value.trim(); if (!name) return;
        prog.hidden = false; progText.textContent = `Pulling ${name}…`;
        await streamNDJSON(await api("/api/ollama/pull", { method: "POST", body: { name }, raw: true }), showEv);
        toast(`Pulled ${name}`, "success"); await loadState(true); renderTab("backends");
      };
      const pullName = h("input", { placeholder: "e.g. qwen3.5:9b" });
      const models = hl.ollama?.models || [];
      const rec = ["qwen3.5:4b", "qwen3.5:9b", "granite4:3b", "gpt-oss:20b", "qwen3.5:27b", "devstral:24b", "qwen3-coder:30b", "qwen3.5:35b"];

      // darkmoon model card
      const dmStatus = h("span", { class: "status-line" }, h("i"), "checking…");
      const dmBase = h("input", { value: C.darkmoon.base_model, list: "ollama-models" });
      const buildDM = async () => {
        prog.hidden = false; progText.textContent = `Building darkmoon from ${dmBase.value}…`;
        if (!models.some((m) => m.replace(/:latest$/, "") === dmBase.value)) {
          progText.textContent = `Pulling base model ${dmBase.value} first…`;
          await streamNDJSON(await api("/api/ollama/pull", { method: "POST", body: { name: dmBase.value }, raw: true }), showEv);
        }
        let err = null;
        await streamNDJSON(await api("/api/darkmoon/create", { method: "POST", body: { base_model: dmBase.value }, raw: true }), (j) => { if (j.error) err = j.error; showEv(j); });
        if (err) return toast(err, "error");
        await api("/api/config", { method: "POST", body: { ollama: { model: "darkmoon" } } });
        S.model = ""; localStorage.removeItem("la.model");
        toast("darkmoon model ready", "success"); await loadState(true); renderTab("backends");
      };
      api("/api/darkmoon").then((d) => {
        dmStatus.className = `status-line ${d.exists ? "ok" : "bad"}`;
        dmStatus.lastChild.textContent = !d.ollama ? "Ollama offline" : d.exists ? `built from ${d.base}` : d.base_pulled ? `not built yet — base ${d.base} is ready` : `base ${d.base} not pulled yet`;
      });

      const card = (title, tag, ...kids) => h("div", { class: "backend-card" }, h("div", { class: "bc-head" }, h("b", {}, title), tag ? h("span", { class: "tag" }, tag) : ""), ...kids);
      const presets = {
        openai: ["OpenAI", "https://api.openai.com/v1"], openrouter: ["OpenRouter", "https://openrouter.ai/api/v1"],
        gemini: ["Gemini", "https://generativelanguage.googleapis.com/v1beta/openai"], groq: ["Groq", "https://api.groq.com/openai/v1"],
        deepseek: ["DeepSeek", "https://api.deepseek.com/v1"], mistral: ["Mistral", "https://api.mistral.ai/v1"],
        together: ["Together", "https://api.together.xyz/v1"], lmstudio: ["LM Studio", "http://localhost:1234/v1"],
        custom: ["Custom", ""],
      };
      const presetSel = h("select", {}, Object.entries(presets).map(([k, [n]]) => h("option", { value: k, selected: C.openai_compat.preset === k }, n)));
      const ocLabel = cfgInput("openai_compat.label"), ocUrl = cfgInput("openai_compat.base_url");
      const ocPreset = h("input", { type: "hidden", value: C.openai_compat.preset, dataset: { path: "openai_compat.preset", type: "str" } });
      presetSel.onchange = () => { const [n, u] = presets[presetSel.value]; ocPreset.value = presetSel.value; if (presetSel.value !== "custom") { ocLabel.value = n; ocUrl.value = u; } };

      root.append(
        h("h2", {}, "Backends"), h("p", { class: "lead" }, "Darkmoon runs locally for free. Cloud backends are optional — subscription CLIs use your existing logins, APIs bill per token."),
        h("datalist", { id: "ollama-models" }, models.map((m) => h("option", { value: m }))),
        card("Darkmoon (local, Ollama)", "free",
          h("div", { class: "row", style: "margin-bottom:12px" }, statusLine(hl.ollama)),
          h("div", { class: "grid2" },
            field("Ollama host", cfgInput("ollama.host")),
            field("Default local model", cfgInput("ollama.model", "str", { list: "ollama-models" }), "<code>darkmoon</code> = your named model below."),
            field("Context window (num_ctx)", cfgInput("ollama.num_ctx", "number")),
            field("Temperature", cfgInput("ollama.temperature", "number", { step: "0.1" }))),
          h("h3", { style: "margin-top:8px" }, "The darkmoon model"),
          h("p", { class: "lead", style: "margin:0 0 10px" }, "An Ollama model named darkmoon, built from a base model with Darkmoon's identity baked in. It also works in a terminal: ollama run darkmoon."),
          h("div", { class: "row", style: "margin-bottom:10px" }, dmStatus),
          field("Base model", h("div", { class: "row", style: "flex-wrap:nowrap" }, dmBase, h("button", { class: "primary-btn", onclick: buildDM }, "Build / rebuild darkmoon")),
            "Pick any tool-capable model (see the VRAM table in the README). It's pulled automatically if needed."),
          field("Pull another model", h("div", { class: "row", style: "flex-wrap:nowrap" }, pullName, h("button", { class: "ghost-btn", onclick: () => pull() }, "Pull"))),
          h("div", { class: "row", style: "gap:6px;margin-bottom:10px" }, rec.map((m) => h("button", { class: "ghost-btn", style: "padding:4px 9px;font-size:12.5px", onclick: () => pull(m) }, (models.includes(m) ? "✓ " : "") + m))),
          prog, progText),
        card("Grok CLI", "your Grok / SuperGrok login",
          h("div", { class: "row", style: "margin-bottom:12px" }, statusLine(hl.grok_cli)),
          toggleRow("Enabled", "Official Grok Build CLI — install, then run `grok login` once", C.grok_cli.enabled, null, "grok_cli.enabled"),
          h("div", { class: "grid2", style: "margin-top:12px" },
            field("CLI path", cfgInput("grok_cli.path"), "\"grok\" if it's on PATH, or a full path."),
            field("Model", cfgInput("grok_cli.model", "str", { placeholder: "CLI default" }), "Run <code>grok models</code> to list."),
            field("Output format", cfgSelect("grok_cli.output_format", [["streaming-json", "streaming-json (live)"], ["json", "json"], ["plain", "plain"]])),
            field("Timeout (s)", cfgInput("grok_cli.timeout", "number"))),
          field("Extra arguments (JSON list)", h("input", { value: JSON.stringify(C.grok_cli.extra_args || []), dataset: { path: "grok_cli.extra_args", type: "json" } }),
            'e.g. <code>["--sandbox", "workspace-write"]</code>. Runs inside your workspace folder.')),
        card("Claude Code", "your Claude Pro / Max login",
          h("div", { class: "row", style: "margin-bottom:12px" }, statusLine(hl.claude_cli)),
          toggleRow("Enabled", "Official Claude Code CLI — install, then run `claude` once to sign in", C.claude_cli.enabled, null, "claude_cli.enabled"),
          h("div", { class: "grid2", style: "margin-top:12px" },
            field("CLI path", cfgInput("claude_cli.path"), "\"claude\" if it's on PATH, or a full path."),
            field("Model", cfgInput("claude_cli.model", "str", { placeholder: "CLI default", list: "claude-aliases" }), "Alias (<code>sonnet</code>, <code>opus</code>, <code>haiku</code>) or a full model name."),
            field("Permission mode", cfgSelect("claude_cli.permission_mode", [["", "default (asks → denied when headless)"], ["acceptEdits", "acceptEdits (may edit files)"], ["plan", "plan (read-only)"], ["auto", "auto"], ["bypassPermissions", "bypassPermissions (⚠ no checks)"]])),
            field("Timeout (s)", cfgInput("claude_cli.timeout", "number"))),
          h("datalist", { id: "claude-aliases" }, ["sonnet", "opus", "haiku", "fable"].map((m) => h("option", { value: m }))),
          field("Extra arguments (JSON list)", h("input", { value: JSON.stringify(C.claude_cli.extra_args || []), dataset: { path: "claude_cli.extra_args", type: "json" } }),
            'e.g. <code>["--allowedTools", "Read,Grep,Glob"]</code>. Runs inside your workspace folder.')),
        card("Grok API", "xAI · per token",
          h("div", { class: "row", style: "margin-bottom:12px" }, statusLine(hl.xai_api)),
          toggleRow("Enabled", "Grok drives Darkmoon's own tools via the API", C.xai_api.enabled, null, "xai_api.enabled"),
          h("div", { class: "grid2", style: "margin-top:12px" },
            field("API key", cfgInput("xai_api.api_key", "str", { type: "password", placeholder: "xai-…", autocomplete: "off" }), "Or set XAI_API_KEY."),
            field("Model", cfgInput("xai_api.model", "str", { list: "xai-models" }), "Suggestions appear once your key works."),
            field("Base URL", cfgInput("xai_api.base_url"))),
          h("datalist", { id: "xai-models" }, (hl.xai_api?.models || []).map((m) => h("option", { value: m })))),
        card("Claude API", "Anthropic · per token",
          h("div", { class: "row", style: "margin-bottom:12px" }, statusLine(hl.claude_api)),
          toggleRow("Enabled", "Claude drives Darkmoon's own tools via the API (key from console.anthropic.com)", C.claude_api.enabled, null, "claude_api.enabled"),
          h("div", { class: "grid2", style: "margin-top:12px" },
            field("API key", cfgInput("claude_api.api_key", "str", { type: "password", placeholder: "sk-ant-…", autocomplete: "off" }), "Or set ANTHROPIC_API_KEY."),
            field("Model", cfgInput("claude_api.model", "str", { list: "claude-models" })),
            field("Max output tokens", cfgInput("claude_api.max_tokens", "number")),
            field("Base URL", cfgInput("claude_api.base_url"))),
          h("datalist", { id: "claude-models" }, [...new Set(["claude-sonnet-5-5", "claude-opus-5-5", "claude-haiku-5-5", ...(hl.claude_api?.models || [])])].map((m) => h("option", { value: m })))),
        card("OpenAI-compatible", "OpenAI, OpenRouter, Gemini, Groq, DeepSeek, Mistral, LM Studio…",
          h("div", { class: "row", style: "margin-bottom:12px" }, statusLine(hl.openai_compat)),
          toggleRow("Enabled", "Any provider with an OpenAI-style /chat/completions API — uses /gpt", C.openai_compat.enabled, null, "openai_compat.enabled"),
          h("div", { class: "grid2", style: "margin-top:12px" },
            field("Provider", presetSel), field("Display name", ocLabel),
            field("Base URL", ocUrl), field("API key", cfgInput("openai_compat.api_key", "str", { type: "password", autocomplete: "off", placeholder: "not needed for LM Studio" }), "Or set OPENAI_API_KEY."),
            field("Model", cfgInput("openai_compat.model", "str", { list: "oc-models" }), "Save once with your key, then pick from the suggestions.")),
          ocPreset,
          h("datalist", { id: "oc-models" }, (hl.openai_compat?.models || []).map((m) => h("option", { value: m })))),
      );
      root.append(saveBtn(root));
      return [root];
    },

    routing() {
      const root = h("div");
      const opts = [["auto", "Auto (Darkmoon first)"], ["ollama", "Darkmoon only"], ...CLOUD.map((b) => [b, BL(b)])];
      root.append(
        h("h2", {}, "Routing"), h("p", { class: "lead" }, "How Darkmoon picks who answers. The picker in the top bar overrides the default."),
        h("div", { class: "grid2" },
          field("Default mode", cfgSelect("routing.mode", opts)),
          field("Escalate to", cfgSelect("routing.escalation_backend", [["auto", "Auto (first available in the order below)"], ...CLOUD.map((b) => [b, BL(b)])]))),
        field("Escalation order", cfgInput("routing.escalation_order", "list"), `Comma-separated, first available wins: ${CLOUD.join(", ")}`),
        toggleRow("Escalate on failure", "Local error, empty answer, repeated tool errors or step limit → cloud", S.state.config.routing.escalate_on_failure, null, "routing.escalate_on_failure"),
        toggleRow("Escalate complex tasks", "Long, multi-part or 'refactor/architecture/debug'-style requests go straight to the cloud", S.state.config.routing.escalate_complex, null, "routing.escalate_complex"),
        toggleRow("Let Darkmoon escalate itself", "Adds an escalate_to_cloud tool the local model can call", S.state.config.routing.allow_self_escalation, null, "routing.allow_self_escalation"),
        h("div", { style: "height:12px" }),
        field("Complexity threshold (0–1)", cfgInput("routing.complexity_threshold", "number", { step: "0.05", min: 0, max: 1 }), "Higher = escalate less often. 0.65 is a good start."),
        h("div", { class: "grid2" },
          field("Grok keywords", cfgInput("routing.grok_keywords", "list")),
          field("Claude keywords", cfgInput("routing.claude_keywords", "list")),
          field("OpenAI-compatible keywords", cfgInput("routing.openai_keywords", "list")),
          field("Local keywords", cfgInput("routing.local_keywords", "list"))),
        field("Agent step limit", cfgInput("agent.max_steps", "number"), "Max tool-call rounds per message."),
      );
      root.append(saveBtn(root));
      return [root];
    },

    connectors() {
      const root = h("div");
      const servers = S.state.config.mcp?.servers || {};
      const info = Object.fromEntries((S.state.mcp || []).map((x) => [x.name, x]));
      const TEMPLATES = [
        { name: "filesystem", title: "Filesystem", desc: "Read/write files in your workspace", conf: { command: "npx", args: ["-y", "@modelcontextprotocol/server-filesystem", "${workspace}"] } },
        { name: "browser", title: "Playwright browser", desc: "Drive a real browser", conf: { command: "npx", args: ["-y", "@playwright/mcp@latest"] } },
        { name: "github", title: "GitHub (remote)", desc: "Repos, issues, PRs — needs a token", conf: { transport: "http", url: "https://api.githubcopilot.com/mcp/", headers: { Authorization: "Bearer YOUR_GITHUB_TOKEN" } } },
        { name: "docs", title: "Context7 docs (remote)", desc: "Up-to-date library docs", conf: { transport: "http", url: "https://mcp.context7.com/mcp" } },
        { name: "linear", title: "Linear", desc: "Issues & projects — sign in", conf: { transport: "http", url: "https://mcp.linear.app/mcp" } },
        { name: "notion", title: "Notion", desc: "Pages & databases — sign in", conf: { transport: "http", url: "https://mcp.notion.com/mcp" } },
        { name: "sentry", title: "Sentry", desc: "Errors & issues — sign in", conf: { transport: "http", url: "https://mcp.sentry.dev/mcp" } },
        { name: "supabase", title: "Supabase", desc: "Database & projects — sign in", conf: { transport: "http", url: "https://mcp.supabase.com/mcp" } },
        { name: "stripe", title: "Stripe", desc: "Payments — sign in or agent key", conf: { transport: "http", url: "https://mcp.stripe.com" } },
        { name: "posthog", title: "PostHog", desc: "Product analytics — sign in", conf: { transport: "http", url: "https://mcp.posthog.com/mcp" } },
        { name: "vercel", title: "Vercel", desc: "Deployments — via mcp-remote (Node.js)", conf: { command: "npx", args: ["-y", "mcp-remote", "https://mcp.vercel.com"], timeout: 300 } },
        { name: "memory", title: "Memory", desc: "Long-term knowledge graph", conf: { command: "npx", args: ["-y", "@modelcontextprotocol/server-memory"] } },
        { name: "thinking", title: "Sequential thinking", desc: "Step-by-step reasoning scratchpad", conf: { command: "npx", args: ["-y", "@modelcontextprotocol/server-sequential-thinking"] } },
        { name: "fetch", title: "Fetch", desc: "Web pages → markdown (needs uv)", conf: { command: "uvx", args: ["mcp-server-fetch"] } },
        { name: "git", title: "Git", desc: "Inspect a git repo (needs uv)", conf: { command: "uvx", args: ["mcp-server-git", "--repository", "${workspace}"] } },
        { name: "time", title: "Time", desc: "Time & timezone conversion (needs uv)", conf: { command: "uvx", args: ["mcp-server-time"] } },
        { name: "", title: "Custom…", desc: "Any MCP server (stdio or URL)", conf: { command: "" } },
      ];
      const formBox = h("div");
      const kv = (obj, sep) => Object.entries(obj || {}).map(([k, v]) => `${k}${sep}${v}`).join("\n");
      const parseKV = (txt, sep) => Object.fromEntries(txt.split("\n").map((l) => l.trim()).filter(Boolean).map((l) => {
        const i = l.indexOf(sep); return i < 0 ? [l, ""] : [l.slice(0, i).trim(), l.slice(i + sep.length).trim()]; }));
      const openForm = (name, conf, oldName = null) => {
        conf = { transport: "stdio", enabled: true, require_approval: S.state.config.safety.require_approval_mcp !== false, timeout: 120, ...conf };
        const nameIn = h("input", { value: name, placeholder: "my-connector" });
        const trans = h("select", {}, [["stdio", "Local command (stdio)"], ["http", "Remote URL (HTTP)"]].map(([v, l]) => h("option", { value: v, selected: conf.transport === v }, l)));
        const cmd = h("input", { value: conf.command || "", placeholder: "npx / uvx / docker / path to .exe" });
        const args = h("textarea", { style: "min-height:70px", placeholder: "one argument per line — ${workspace} = your workspace folder" }, (conf.args || []).join("\n"));
        const env = h("textarea", { style: "min-height:60px", placeholder: "API_KEY=…  (one per line)" }, kv(conf.env, "="));
        const url = h("input", { value: conf.url || "", placeholder: "https://example.com/mcp" });
        const headers = h("textarea", { style: "min-height:60px", placeholder: "Authorization: Bearer …  (one per line)" }, kv(conf.headers, ": "));
        const timeout = h("input", { type: "number", value: conf.timeout });
        const appr = h("input", { type: "checkbox", checked: conf.require_approval });
        const stdioBox = h("div", {}, field("Command", cmd), field("Arguments", args), field("Environment variables", env, "Secrets are stored in config.toml on this PC and masked in the UI."));
        const httpBox = h("div", {}, field("Server URL", url, "Streamable-HTTP MCP endpoint. Services that need a sign-in show a Sign in button after saving; or paste a token header below."), field("Headers", headers));
        const sync = () => { stdioBox.hidden = trans.value !== "stdio"; httpBox.hidden = trans.value !== "http"; };
        trans.onchange = sync; sync();
        const save = async (btn) => {
          btn.disabled = true; btn.textContent = "Connecting…";
          const c = { transport: trans.value, enabled: conf.enabled, require_approval: appr.checked, timeout: Number(timeout.value) || 120,
            command: cmd.value.trim(), args: args.value.split("\n").map((x) => x.trim()).filter(Boolean), env: parseKV(env.value, "="),
            url: url.value.trim(), headers: parseKV(headers.value, ":") };
          try {
            const r = await api("/api/mcp/save", { method: "POST", body: { name: nameIn.value, conf: c, old_name: oldName } });
            toast(r.server.status === "connected" ? `${r.server.name}: connected, ${r.server.tools.length} tools`
              : r.server.status === "needs_auth" ? `${r.server.name}: opening sign-in…` : `${r.server.name}: ${r.server.error || r.server.status}`,
              r.server.status === "connected" ? "success" : r.server.status === "needs_auth" ? "" : "error");
            await loadState(); renderTab("connectors");
            if (r.server.status === "needs_auth") TABS.__signIn?.(r.server.name);
          } catch (e) { toast(e.message, "error"); btn.disabled = false; btn.textContent = "Save & connect"; }
        };
        const saveBtn_ = h("button", { class: "primary-btn" }, "Save & connect"); saveBtn_.onclick = () => save(saveBtn_);
        formBox.innerHTML = "";
        formBox.append(h("div", { class: "conn-form" },
          h("div", { class: "grid2" }, field("Name", nameIn), field("Type", trans)), stdioBox, httpBox,
          h("div", { class: "grid2" }, field("Timeout per call (s)", timeout),
            field("Approval", h("label", { class: "row" }, h("span", { class: "switch" }, appr, h("span")), h("span", { style: "font-size:13.5px" }, "Ask before each tool call")))),
          h("div", { class: "row" }, saveBtn_, h("button", { class: "ghost-btn", onclick: () => (formBox.innerHTML = "") }, "Cancel"))));
        nameIn.focus();
      };
      const signIn = async (name) => {
        let r;
        try { r = await api(`/api/mcp/${name}/auth`, { method: "POST" }); } catch (e) { return toast(e.message, "error"); }
        if (S.state.remote) window.open(r.url, "_blank");  // on the PC the server opens your default browser
        formBox.innerHTML = "";
        formBox.append(h("div", { class: "conn-form" }, h("b", {}, `Signing in to ${name}…`),
          h("p", { class: "lead", style: "margin:6px 0" }, "Finish in the browser window that opened, then come back here."),
          h("a", { href: r.url, target: "_blank" }, "Open the sign-in page again")));
        for (let i = 0; i < 90; i++) {  // poll for up to 3 minutes
          await new Promise((res) => setTimeout(res, 2000));
          const st = (await api("/api/mcp")).servers.find((x) => x.name === name);
          if (st?.status === "connected") { toast(`${name} connected — ${st.tools.length} tools`, "success"); break; }
          if ($("#settings").hidden) break;
        }
        await loadState(); if (!$("#settings").hidden) renderTab("connectors");
      };
      TABS.__signIn = signIn;
      const signOut = async (name) => {
        if (!confirm(`Sign out of ${name}? Darkmoon forgets its token.`)) return;
        await api(`/api/mcp/${name}/auth`, { method: "DELETE" }); await loadState(); renderTab("connectors");
      };
      const rows = Object.entries(servers).map(([name, conf]) => {
        const st = info[name] || { status: "off", tools: [] };
        const en = h("input", { type: "checkbox", checked: conf.enabled !== false });
        en.onchange = async () => { await api("/api/mcp/save", { method: "POST", body: { name, conf: { ...conf, enabled: en.checked } } }); await loadState(); renderTab("connectors"); };
        const sub = st.status === "connected" ? `${st.server?.name || ""} ${st.server?.version || ""} · ${st.tools.length} tools`.trim()
          : conf.transport === "http" ? conf.url : [conf.command, ...(conf.args || [])].join(" ");
        return h("div", { class: "conn-row" },
          h("div", { class: "conn-head" }, h("span", { class: `conn-dot ${st.status}`, title: st.status }),
            h("div", { class: "pr-text" }, h("b", {}, name), h("span", { class: "tag" }, conf.transport === "http" ? "remote" : "local"),
              conf.require_approval === false ? h("span", { class: "tag warn" }, "no approval") : "", h("small", {}, sub)),
            conf.transport === "http" && (st.status === "needs_auth" || st.signed_in)
              ? h("button", { class: st.status === "needs_auth" ? "primary-btn" : "ghost-btn", style: "padding:5px 10px",
                  onclick: () => (st.status === "needs_auth" ? signIn(name) : signOut(name)) }, st.status === "needs_auth" ? "Sign in" : "Sign out") : "",
            h("button", { class: "ghost-btn", style: "padding:5px 10px", onclick: async () => { toast(`Reconnecting ${name}…`); await api(`/api/mcp/${name}`, { method: "POST" }); await loadState(); renderTab("connectors"); } }, "↻"),
            h("button", { class: "ghost-btn", style: "padding:5px 10px", onclick: () => openForm(name, conf, name) }, "Edit"),
            h("button", { class: "danger-btn", style: "padding:5px 10px", onclick: async () => { if (!confirm(`Remove connector ${name}?`)) return; await api(`/api/mcp/${name}`, { method: "DELETE" }); await loadState(); renderTab("connectors"); } }, "🗑"),
            h("label", { class: "switch" }, en, h("span"))),
          st.error ? h("div", { class: "conn-err" }, st.error) : "",
          st.tools.length ? h("div", { class: "conn-tools" }, st.tools.map((t) => h("code", { title: t.description }, t.name))) : "");
      });
      root.append(
        h("h2", {}, "Connectors"),
        h("p", { class: "lead" }, "Plug outside tools into Darkmoon with MCP (Model Context Protocol) servers — the same standard Claude, Grok and others use. Every model you route to can call them."),
        h("h3", {}, "Your connectors"),
        ...(rows.length ? rows : [h("p", { class: "lead" }, "None yet — add one below.")]),
        formBox,
        h("h3", {}, "Add a connector"),
        h("div", { class: "tpl-grid" }, TEMPLATES.map((t) => h("button", { onclick: () => openForm(t.name && !servers[t.name] ? t.name : t.name ? t.name + "-2" : "", JSON.parse(JSON.stringify(t.conf))) },
          h("b", {}, t.title), h("small", {}, t.desc)))),
        h("p", { class: "lead", html: "Local connectors need <b>Node.js</b> (for <code>npx</code>) or <b>uv</b> (for <code>uvx</code>). Find thousands more at <a href='https://github.com/modelcontextprotocol/servers' target='_blank'>github.com/modelcontextprotocol/servers</a>. Remote services sign in through your browser (OAuth); Darkmoon stores the token on this PC. Connector tools ask for approval by default." }),
      );
      return [root];
    },

    phone() {
      const root = h("div");
      const ph = S.state.phone || {};
      const remote = S.state.remote;
      const qrBox = h("div");
      const draw = (st) => {
        qrBox.innerHTML = "";
        if (st.error) qrBox.append(h("div", { class: "error-box" }, st.error));
        if (!st.running) return;
        const url = st.urls[0];
        const others = st.urls.slice(1);
        qrBox.append(h("div", { class: "phone-box" },
          h("img", { src: `/api/phone/qr.svg?token=${encodeURIComponent(window.LA_TOKEN)}&t=${Date.now()}`, alt: "Pairing QR code" }),
          h("div", { class: "pb-text" },
            h("b", {}, "Scan with your phone's camera"),
            h("p", { class: "lead", style: "margin:6px 0 10px" }, "Your phone must be on the same Wi-Fi. Once paired, add it to your home screen for an app-like icon."),
            h("div", { class: "field" }, h("label", {}, "Or open this link on the phone"), h("code", {}, url),
              h("div", { class: "row", style: "margin-top:8px" }, h("button", { class: "ghost-btn", onclick: () => { navigator.clipboard.writeText(url); toast("Link copied"); } }, "Copy link"))),
            others.length ? h("div", { class: "help" }, "Other addresses for this PC: ", others.map((u) => u.split("/pair")[0]).join(", ")) : "")));
      };
      const toggle = h("input", { type: "checkbox", checked: ph.enabled, disabled: remote });
      toggle.onchange = async () => {
        toggle.disabled = true;
        try { const st = await api("/api/phone", { method: "POST", body: { enabled: toggle.checked } }); S.state.phone = st; draw(st);
          if (toggle.checked && st.running) toast("Phone access is on", "success"); }
        catch (e) { toast(e.message, "error"); }
        toggle.disabled = false;
      };
      root.append(
        h("h2", {}, "Phone"),
        h("p", { class: "lead" }, "Use Darkmoon from your phone at home: start a task on your PC, check in from the couch, approve actions, review code changes. Your PC does the work — nothing goes through the cloud."),
        h("label", { class: "toggle-row" }, h("div", { class: "tr-text" }, h("b", {}, "Allow phone access on my Wi-Fi"),
          h("small", {}, remote ? "Change this on the PC." : `Listens on port ${ph.port || 8766} for paired devices only`)),
          h("span", { class: "switch" }, toggle, h("span"))),
        qrBox,
        h("h3", {}, "Security"),
        h("div", { class: "md", html: DOMPurify.sanitize(marked.parse(
          "- Only devices that scanned the QR code (or opened the link) can connect — others see a *Pair this device* page.\n" +
          "- Traffic on your home network isn't encrypted. Don't enable this on public or shared Wi-Fi.\n" +
          "- Windows may ask to allow Darkmoon through the firewall — choose **Private networks** only.\n" +
          "- Lost a phone or shared the link? **Reset pairing** below logs out every paired device.")) }),
        h("div", { class: "row", style: "margin-top:12px" }, h("button", { class: "danger-btn", disabled: remote, onclick: async () => {
          if (!confirm("Reset pairing? Every paired phone will need to scan the new code.")) return;
          const st = await api("/api/phone/reset", { method: "POST" }); S.state.phone = st; draw(st); toast("Pairing reset — scan the new code"); } }, "Reset pairing")),
      );
      draw(ph);
      return [root];
    },

    tools() {
      const disabled = new Set(S.state.config.tools.disabled || []);
      const rows = S.state.tools.map((t) => {
        const inp = h("input", { type: "checkbox", checked: !disabled.has(t.name) });
        inp.onchange = async () => { inp.checked ? disabled.delete(t.name) : disabled.add(t.name);
          await api("/api/config", { method: "POST", body: { tools: { disabled: [...disabled] } } }); await loadState(); toast(`${t.name} ${inp.checked ? "enabled" : "disabled"}`); };
        return h("div", { class: "plugin-row" }, h("span", { style: "font-size:18px" }, TOOL_ICON[t.name] || (isMcpTool(t.name) ? "🔌" : "🔧")),
          h("div", { class: "pr-text" }, h("b", { style: "font-family:var(--font-mono)" }, t.name),
            t.approval ? h("span", { class: "tag warn" }, `asks: ${t.approval}`) : "", t.source !== "builtin" ? h("span", { class: "tag" }, t.source) : "",
            h("small", {}, t.description.split("\n")[0])),
          h("label", { class: "switch" }, inp, h("span")));
      });
      const errs = Object.entries(S.state.plugin_errors || {});
      return [
        h("h2", {}, "Tools"), h("p", { class: "lead" }, "What the agent can do. Add your own by dropping a Python file in the tool plugins folder (see README)."),
        ...rows,
        errs.length ? h("div", { class: "error-box" }, errs.map(([f, e]) => `${f}:\n${e}`).join("\n")) : "",
        h("div", { class: "row", style: "margin-top:12px" },
          h("button", { class: "ghost-btn", onclick: async () => { const r = await api("/api/tools/reload", { method: "POST" }); await loadState(); renderTab("tools"); toast(`Loaded ${r.loaded.length} plugin file(s)`); } }, "Reload tool plugins"),
          h("button", { class: "ghost-btn", onclick: () => openFolder("plugins") }, "Open tool plugins folder")),
      ];
    },

    safety() {
      const root = h("div");
      const c = S.state.config.safety;
      root.append(
        h("h2", {}, "Safety"), h("p", { class: "lead" }, "The agent can touch your files and run commands. These guards keep you in control."),
        toggleRow("Ask before shell commands", "Strongly recommended", c.require_approval_shell, null, "safety.require_approval_shell"),
        toggleRow("Ask before writing files", "Covers write_file and edit_file", c.require_approval_write, null, "safety.require_approval_write"),
        toggleRow("Ask before running Python", "", c.require_approval_python, null, "safety.require_approval_python"),
        toggleRow("Ask before running project tests", "Code mode's run_tests (tests execute your project's code)", c.require_approval_tests, null, "safety.require_approval_tests"),
                toggleRow("Ask before connector tools", "Default for MCP connectors (each connector can override)", c.require_approval_mcp, null, "safety.require_approval_mcp"),
        toggleRow("Allow file access outside the workspace", "Off = file tools are locked to the workspace folder", c.allow_outside_workspace, null, "safety.allow_outside_workspace"),
        h("div", { style: "height:12px" }),
        h("div", { class: "grid2" }, field("Command timeout (s)", cfgInput("safety.shell_timeout", "number")), field("Max tool output (chars)", cfgInput("safety.max_output_chars", "number"))),
        field("Blocked command patterns (one per line)", cfgInput("safety.blocked_commands", "lines"), "Commands containing any of these are refused outright. This is a seatbelt, not a sandbox — approvals are the real protection."),
      );
      root.append(saveBtn(root));
      return [root];
    },

    general() {
      const root = h("div");
      root.append(
        h("h2", {}, "General"),
        field("Workspace folder", h("div", { class: "row", style: "flex-wrap:nowrap" }, cfgInput("agent.workspace"), h("button", { class: "ghost-btn", onclick: () => openFolder("workspace") }, "Open")),
          `Resolved: <code>${esc(S.state.config.agent.workspace_resolved)}</code>`),
        field("System prompt", cfgInput("agent.system_prompt", "text", { style: "min-height:150px" })),
        h("h3", {}, "Desktop"),
        field("Global hotkey", cfgInput("ui.hotkey"), "pynput format, e.g. <code>&lt;ctrl&gt;+&lt;alt&gt;+&lt;space&gt;</code>. Restart to apply."),
        toggleRow("Close button hides to tray", "Quit from the tray menu", S.state.config.ui.minimize_to_tray, null, "ui.minimize_to_tray"),
        toggleRow("Start hidden in tray", "Useful with 'start with Windows'", S.state.config.ui.start_hidden, null, "ui.start_hidden"),
      );
      root.append(saveBtn(root), h("div", { class: "row", style: "margin-top:10px" }, h("button", { class: "ghost-btn", onclick: () => openFolder("data") }, "Open data folder (config, history, themes)")));
      return [root];
    },

    about() {
      return [
        h("h2", {}, "Darkmoon"), h("p", { class: "lead" }, `Version ${S.state.version} — your local-first AI agent. Data folder: ${S.state.data_dir}`),
        h("h3", {}, "Keyboard shortcuts"),
        h("div", { class: "md", html: DOMPurify.sanitize(marked.parse(
          "| Keys | Action |\n|---|---|\n| `Enter` / `Shift+Enter` | send / new line |\n| `Ctrl+N` | new chat |\n| `Ctrl+,` | settings |\n| `Ctrl+B` | toggle sidebar |\n| `Ctrl+K` | search chats |\n| `Esc` | stop generating / close dialogs |\n| `Ctrl+Shift+E` | export chat as Markdown |\n| global hotkey | show / hide window (see General) |")) }),
        h("h3", {}, "Routing cheatsheet"),
        h("div", { class: "md", html: DOMPurify.sanitize(marked.parse("- `/grok …` → Grok (CLI, else API)\n- `/claude …` → Claude (Claude Code, else API)\n- `/gpt …` → your OpenAI-compatible model\n- `/local …` → local Darkmoon only\n- **Auto**: Darkmoon first; escalates in your chosen order when it fails or the task looks complex")) }),
      ];
    },
  };
  function renderTab(tab) {
    const box = $("#settings-content"); box.innerHTML = "";
    try { box.append(...TABS[tab]()); } catch (e) { console.error(e); box.append(h("div", { class: "error-box" }, e.message)); }
    $(".settings-body").scrollTop = 0;
  }

  // ── Export ───────────────────────────────────────────────────────────
  async function exportChat() {
    if (!S.convId) return toast("Nothing to export yet");
    const md = await api(`/api/conversations/${S.convId}/export`);
    const a = h("a", { href: URL.createObjectURL(new Blob([md], { type: "text/markdown" })), download: `${$("#conv-title").textContent.replace(/[^\w-]+/g, "_")}.md` });
    a.click(); toast("Exported");
  }

  // ── Keyboard ─────────────────────────────────────────────────────────
  document.addEventListener("keydown", (e) => {
    const mod = e.ctrlKey || e.metaKey;
    if (!$("#approval").hidden) {
      if (e.key === "Escape") { e.preventDefault(); decide("deny"); }
      else if (e.key === "Enter" && document.activeElement?.tagName !== "BUTTON") { e.preventDefault(); decide("allow"); }
      return;
    }
    if (e.key === "Escape") {
      if (!$("#settings").hidden) return closeSettings();
      if (!$("#changes-panel").hidden) return closeChanges();
      if (document.body.classList.contains("drawer-open")) return closeDrawer();
      if (S.busy) return stop();
    }
    if (mod && e.key.toLowerCase() === "n") { e.preventDefault(); newChat(); }
    if (mod && e.key === ",") { e.preventDefault(); openSettings(); }
    if (mod && e.key.toLowerCase() === "b") { e.preventDefault(); setUI({ sidebar_collapsed: !S.ui.sidebar_collapsed }); }
    if (mod && e.key.toLowerCase() === "k") { e.preventDefault(); if (S.ui.sidebar_collapsed) setUI({ sidebar_collapsed: false }); $("#search").focus(); }
    if (mod && e.shiftKey && e.key.toLowerCase() === "e") { e.preventDefault(); exportChat(); }
  });

  // ── Public API ───────────────────────────────────────────────────────
  window.Darkmoon = window.LocalAgent = {
    version: window.LA_VERSION,
    plugins: { register: (def) => Plugins.register(def) },
    newChat, openSettings, send: sendMessage, exportChat, toast, setUI, api,
    setTheme: (name) => setUI({ theme: name }),
    get themes() { return S.state?.themes || []; },
    on: (t, fn) => bus.on(t, fn),
  };

  // ── Init ─────────────────────────────────────────────────────────────
  (async function init() {
    try { await loadState(); } catch (e) { document.body.innerHTML = `<pre style="padding:30px">Failed to load: ${esc(e.message)}</pre>`; return; }
    applyAppearance(); applyQuickCSS();
    setTimeout(() => $("#boot")?.classList.add("gone"), 120);
    setTimeout(() => $("#boot")?.remove(), 700);
    $$(".suggestions button").forEach((b) => (b.onclick = () => sendMessage(b.textContent)));
    // built-in client commands
    Plugins.cmds.set("new", { name: "new", description: "Start a new chat", run: newChat });
    Plugins.cmds.set("settings", { name: "settings", description: "Open settings", run: () => openSettings() });
    Plugins.cmds.set("export", { name: "export", description: "Export this chat as Markdown", run: exportChat });
    Plugins.cmds.set("theme", { name: "theme", description: "Switch theme: /theme <name>", run: (arg) => {
      const t = S.state.themes.find((x) => x.name.toLowerCase().startsWith(arg.toLowerCase()) || x.file.startsWith(arg.toLowerCase()));
      if (!t) return toast(`Themes: ${S.state.themes.map((x) => x.name).join(", ")}`);
      setUI({ theme: t.file.replace(/\.css$/, "") }); toast(`Theme: ${t.name}`);
    } });
    connect();
    await loadProjects();
    await refreshConvs();
    await Plugins.loadAll();
    if (!S.state.health.ollama?.ok) toast("Ollama isn't reachable — start Ollama, or switch to Grok mode.", "error");
    else if (!S.state.health.ollama.model_ready) {
      if (S.state.config.ollama.model === "darkmoon") toast("The darkmoon model isn't built yet — using its base model for now. Settings › Backends › Build darkmoon.");
      else toast(`Model ${S.state.config.ollama.model} isn't pulled yet — Settings › Backends.`, "error");
    }
    setInterval(() => loadState().catch(() => {}), 60000);
    input.focus();
  })();
})();
