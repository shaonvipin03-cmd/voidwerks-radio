/* Voidwerks radio: private panel.
   nginx injects this script ONLY into the tailnet-only copy of the page.
   All text from the server (YouTube titles etc.) is untrusted: it is only ever
   written with textContent, never as HTML. */
(() => {
  "use strict";
  const API = "/radio/private-api";
  const FILES = "/radio/private-files/";

  const style = document.createElement("style");
  style.textContent = `
    #priv { max-width: 478px; box-sizing: border-box; margin: 20px auto 40px; padding: 14px 20px 18px;
            border: 2px solid var(--ink, #222); color: var(--ink, inherit); font: inherit; font-size: 14px; }
    #priv .ph { display: flex; justify-content: space-between; letter-spacing: .08em; font-size: 12px; margin-bottom: 10px; }
    #priv .dim { opacity: .6; }
    #priv .row { display: flex; gap: 8px; margin: 8px 0; }
    #priv input { flex: 1; min-width: 0; box-sizing: border-box; padding: 8px; font: inherit; color: inherit;
                  background: transparent; border: 1px solid var(--ink, #222); border-radius: 0; }
    #priv button { font: inherit; color: inherit; background: transparent; padding: 4px 9px; cursor: pointer;
                   border: 1px solid var(--ink, #222); border-radius: 0; }
    #priv button:hover { background: var(--ink, #222); color: var(--bg, #fff); }
    #priv audio { width: 100%; margin: 8px 0 2px; }
    #priv .msg { min-height: 1.4em; }
    #priv .job { font-size: 12px; opacity: .8; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    #priv .file { padding: 8px 0; border-top: 1px dotted var(--ink, #222); }
    #priv .ft { overflow-wrap: anywhere; }
    #priv .fm { font-size: 12px; opacity: .6; margin: 2px 0 6px; }
    #priv .fb { display: flex; flex-wrap: wrap; gap: 6px; }
    #priv .on { font-weight: bold; }
  `;
  document.head.appendChild(style);

  const el = (tag, cls, text) => {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = text;
    return e;
  };

  const panel = el("section");
  panel.id = "priv";
  const head = el("div", "ph");
  head.append(el("span", "", "PRIVATE"), el("span", "dim", "tailnet only"));
  const addRow = el("div", "row");
  const input = el("input");
  input.type = "text";
  input.placeholder = "youtube link or playlist...";
  input.autocomplete = "off";
  input.spellcheck = false;
  const addBtn = el("button", "", "add");
  addRow.append(input, addBtn);
  const msg = el("div", "msg dim");
  const jobsBox = el("div");
  const audio = el("audio");
  audio.controls = true;
  audio.preload = "none";
  const nowEl = el("div", "dim");
  const libHead = el("div", "ph");
  libHead.style.marginTop = "14px";
  const libTitle = el("span", "", "LIBRARY");
  const libInfo = el("span", "dim", "");
  libHead.append(libTitle, libInfo);
  const clearBtn = el("button", "", "clear radio queue");
  const libBox = el("div");
  panel.append(head, addRow, msg, jobsBox, audio, nowEl, libHead, clearBtn, libBox);
  document.body.appendChild(panel);

  let files = [];
  let shared = [];
  let playing = -1;
  let msgTimer = 0;
  let lastSig = "";
  let lastLib = 0;

  function say(text, ms) {
    msg.textContent = text;
    clearTimeout(msgTimer);
    if (ms) msgTimer = setTimeout(() => { if (msg.textContent === text) msg.textContent = ""; }, ms);
  }
  function fmtDur(s) {
    s = Math.max(0, Math.floor(s || 0));
    const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), x = s % 60;
    const mm = h ? String(m).padStart(2, "0") : String(m);
    return (h ? h + ":" : "") + mm + ":" + String(x).padStart(2, "0");
  }
  function fmtSize(b) { return b >= 1e9 ? (b / 1e9).toFixed(1) + " GB" : Math.max(1, Math.round(b / 1e6)) + " MB"; }

  async function api(path, body) {
    const opt = body === undefined
      ? { cache: "no-store", signal: AbortSignal.timeout(15000) }
      : { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
          signal: AbortSignal.timeout(15000) };
    const r = await fetch(API + path, opt);
    let data = null;
    try { data = await r.json(); } catch (e) { /* not json */ }
    if (!r.ok) throw new Error((data && data.error) || "http " + r.status);
    return data;
  }

  function playIndex(i) {
    const f = files[i];
    if (!f) return;
    playing = i;
    audio.src = FILES + encodeURIComponent(f.name);
    audio.play().catch(() => { say("press play on the player", 4000); });
    nowEl.textContent = "> " + (f.title || f.name);
    renderLib();
  }
  audio.addEventListener("ended", () => { if (playing >= 0 && playing + 1 < files.length) playIndex(playing + 1); });

  async function share(f, now) {
    try {
      await api("/share", { name: f.name, now });
      say(now ? "> sent to the radio, switching in a few seconds" : "> queued on the radio, plays after the current track", 8000);
      loadLib();
    } catch (e) { say("! " + e.message, 9000); }
  }

  function delBtn(f) {
    const b = el("button", "", "x");
    let armed = false, t = 0;
    b.addEventListener("click", async () => {
      if (!armed) {
        armed = true; b.textContent = "sure?";
        t = setTimeout(() => { armed = false; b.textContent = "x"; }, 4000);
        return;
      }
      clearTimeout(t);
      try { await api("/delete", { name: f.name }); loadLib(); }
      catch (e) { say("! " + e.message, 9000); armed = false; b.textContent = "x"; }
    });
    return b;
  }

  function renderLib() {
    libBox.replaceChildren();
    files.forEach((f, i) => {
      const row = el("div", "file");
      row.append(el("div", "ft" + (i === playing ? " on" : ""), (i === playing ? "> " : "") + (f.title || f.name)));
      const bits = [];
      if (f.artist) bits.push(f.artist);
      if (f.duration) bits.push(fmtDur(f.duration));
      bits.push(fmtSize(f.size));
      if (shared.includes(f.name)) bits.push("on radio queue");
      row.append(el("div", "fm", bits.join(" \u00b7 ")));
      const bar = el("div", "fb");
      const play = el("button", "", "play");
      play.addEventListener("click", () => playIndex(i));
      const next = el("button", "", "radio: next");
      next.addEventListener("click", () => share(f, false));
      const now = el("button", "", "radio: now");
      now.addEventListener("click", () => share(f, true));
      bar.append(play, next, now, delBtn(f));
      row.append(bar);
      libBox.append(row);
    });
    if (!files.length) libBox.append(el("div", "dim", "nothing here yet - paste a link above"));
  }

  async function loadLib() {
    const d = await api("/library");
    const cur = playing >= 0 && files[playing] ? files[playing].name : null;
    files = d.files || [];
    shared = d.shared || [];
    playing = cur ? files.findIndex(f => f.name === cur) : -1;
    libInfo.textContent = files.length + " files, " + (d.bytes / 1e9).toFixed(1) + " / " + d.limit_gb + " GB";
    lastLib = Date.now();
    renderLib();
  }

  function renderJobs(jobs) {
    jobsBox.replaceChildren();
    jobs.slice(0, 4).forEach(j => {
      const tag = j.state === "done" ? "[ok] " : j.state === "error" ? "[!!] " : "[..] ";
      const line = el("div", "job", tag + (j.state === "running" || j.state === "error" ? j.line : j.state === "queued" ? "queued" : j.url));
      line.title = j.url;
      jobsBox.append(line);
    });
  }

  async function add() {
    const url = input.value.trim();
    if (!url) return;
    addBtn.disabled = true;
    try {
      await api("/add", { url });
      input.value = "";
      say("> queued for download", 5000);
      tick();
    } catch (e) { say("! " + e.message, 9000); }
    addBtn.disabled = false;
  }
  addBtn.addEventListener("click", add);
  input.addEventListener("keydown", e => { if (e.key === "Enter") add(); });
  clearBtn.addEventListener("click", async () => {
    try { await api("/unshare", {}); say("> radio queue cleared (a track already playing finishes)", 7000); loadLib(); }
    catch (e) { say("! " + e.message, 9000); }
  });

  let timer = 0;
  async function tick() {
    clearTimeout(timer);
    let busy = false;
    try {
      const j = await api("/jobs");
      renderJobs(j.jobs);
      busy = j.jobs.some(x => x.state === "queued" || x.state === "running");
      const sig = j.jobs.map(x => x.id + x.state).join();
      if (sig !== lastSig || Date.now() - lastLib > 20000) { lastSig = sig; await loadLib(); }
    } catch (e) {
      say("! private service not reachable", 4000);
    }
    timer = setTimeout(tick, busy ? 2000 : 6000);
  }
  tick();
})();
