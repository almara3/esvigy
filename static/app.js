"use strict";

// ---------------------------------------------------------------- utilities

const $ = (id) => document.getElementById(id);
// Every server URL lives below the secret /<token>/ prefix the page was opened with.
const BASE = location.pathname.replace(/[^/]*$/, "");
const el = (tag, cls, text) => {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text != null) e.textContent = text;
  return e;
};
const joinPath = (dir, name) => (dir ? dir + "/" + name : name);
const encPath = (p) => p.split("/").map(encodeURIComponent).join("/");
const fileUrl = (dir, f, fix) => BASE + "file/" + encPath(joinPath(dir, f.name)) + (fix ? "?fix" : "");
const thumbUrl = (f) => BASE + "thumb/" + f.key + ".webp";
const collator = new Intl.Collator(undefined, { numeric: true, sensitivity: "base" });

function fmtSize(n) {
  if (n < 1024) return n + " B";
  const u = ["KB", "MB", "GB"];
  let i = -1;
  do { n /= 1024; i++; } while (n >= 1024 && i < u.length - 1);
  return n.toFixed(n < 10 ? 1 : 0) + " " + u[i];
}
const dateFmt = new Intl.DateTimeFormat(undefined, { dateStyle: "short", timeStyle: "short" });
const fmtDate = (t) => dateFmt.format(t * 1000);
const fmtDims = (f) => Math.round(f.w) + "×" + Math.round(f.h);

const prefs = {
  get(k, d) { try { return localStorage.getItem("esvigy." + k) ?? d; } catch { return d; } },
  set(k, v) { try { localStorage.setItem("esvigy." + k, v); } catch {} },
};

// ---------------------------------------------------------------- data

const listCache = new Map();
function list(dir) {
  if (!listCache.has(dir)) {
    const p = fetch(BASE + "api/list?dir=" + encodeURIComponent(dir)).then((r) => {
      if (!r.ok) throw new Error(r.status + " " + r.statusText);
      return r.json();
    });
    p.catch(() => listCache.delete(dir));
    listCache.set(dir, p);
  }
  return listCache.get(dir);
}

const state = {
  dir: null,        // currently shown directory ("" = root)
  files: [],        // all SVGs in dir (sorted)
  view: [],         // files after filter
  current: -1,      // index into view while the viewer is open
  cmp: -1,          // index of the comparison file (compare mode), else -1
  openedByPush: false,
};

// ---------------------------------------------------------------- routing
// URL hash: #dir=<path>&file=<name>. Opening a file pushes history, so Back closes it.

function readHash() {
  const p = new URLSearchParams(location.hash.slice(1));
  return { dir: p.get("dir") || "", file: p.get("file"), vs: p.get("vs") };
}
function writeHash(dir, file, push, vs) {
  const p = new URLSearchParams();
  if (dir) p.set("dir", dir);
  if (file) p.set("file", file);
  if (file && vs) p.set("vs", vs);
  const h = "#" + p.toString().replace(/\+/g, "%20");
  if (push) location.hash = h;
  else { history.replaceState(null, "", h); route(); }
}

let routeToken = 0;
async function route() {
  const token = ++routeToken;
  const { dir, file, vs } = readHash();
  if (dir !== state.dir) {
    closeViewer();
    await loadDir(dir);
    if (token !== routeToken) return;
  }
  const i = file ? state.view.findIndex((f) => f.name === file) : -1;
  const j = vs ? state.view.findIndex((f) => f.name === vs) : -1;
  if (i >= 0) openViewer(i, j);
  else closeViewer();
}
window.addEventListener("hashchange", route);

// ---------------------------------------------------------------- folder tree

const treeNodes = new Map(); // path -> {li, row, ul, loaded, open}

function makeNode(path, name) {
  const li = el("li", "closed");
  const row = el("div", "row");
  const twisty = el("span", "twisty", "▸");
  row.append(twisty, el("span", "label", name));
  row.title = path || name;
  const ul = el("ul");
  li.append(row, ul);
  const node = { li, row, ul, twisty, loaded: false, open: false };
  treeNodes.set(path, node);
  row.addEventListener("click", () => writeHash(path, null, true));
  twisty.addEventListener("click", (e) => {
    e.stopPropagation();
    node.open ? collapse(path) : expand(path);
  });
  return node;
}

async function expand(path) {
  const node = treeNodes.get(path);
  if (!node) return;
  if (!node.loaded) {
    let data;
    try { data = await list(path); } catch { return; }
    node.loaded = true;
    node.ul.replaceChildren();
    const dirs = data.dirs.slice().sort(collator.compare);
    for (const d of dirs) node.ul.append(makeNode(joinPath(path, d), d).li);
    if (!dirs.length) node.twisty.style.visibility = "hidden";
  }
  node.open = true;
  node.li.classList.remove("closed");
  node.twisty.textContent = "▾";
}

function collapse(path) {
  const node = treeNodes.get(path);
  node.open = false;
  node.li.classList.add("closed");
  node.twisty.textContent = "▸";
}

async function revealInTree(dir) {
  const parts = dir ? dir.split("/") : [];
  for (let i = 0; i <= parts.length; i++) await expand(parts.slice(0, i).join("/"));
  for (const n of treeNodes.values()) n.row.classList.remove("sel");
  const node = treeNodes.get(dir);
  if (node) {
    node.row.classList.add("sel");
    node.row.scrollIntoView({ block: "nearest" });
  }
}

// ---------------------------------------------------------------- grid

const grid = $("grid");
const cards = new Map(); // file object -> card element

async function loadDir(dir) {
  state.dir = dir;
  selected.length = 0;
  thumbs.clear();
  let data;
  try {
    data = await list(dir);
  } catch (e) {
    grid.replaceChildren(el("div", "note", "Cannot open folder: " + e.message));
    return;
  }
  if (state.dir !== dir) return;
  if (!treeNodes.size) {
    const ul = el("ul");
    ul.append(makeNode("", data.root).li);
    $("tree").append(ul);
    document.title = "esvigy — " + data.root;
  }
  revealInTree(dir);
  renderCrumbs(dir, data.root);
  state.files = data.files;
  sortFiles();
  renderGrid();
}

let sortRev = prefs.get("rev", "0") === "1";
function sortFiles() {
  const key = $("sort").value;
  const cmp = {
    name: (a, b) => collator.compare(a.name, b.name),
    size: (a, b) => b.size - a.size,
    mtime: (a, b) => b.mtime - a.mtime,
  }[key];
  state.files.sort(cmp);
  if (sortRev) state.files.reverse();
  // Mark the active column; the arrow shows the visible direction.
  const asc = (key === "name") !== sortRev;
  document.querySelectorAll("#thead [data-sort]").forEach((b) => {
    const on = b.dataset.sort === key;
    b.classList.toggle("active", on);
    b.textContent = b.textContent.replace(/ [▲▼]$/, "") + (on ? (asc ? " ▲" : " ▼") : "");
  });
}

function renderCrumbs(dir, rootName) {
  const c = $("crumbs");
  c.replaceChildren();
  const parts = dir ? dir.split("/") : [];
  const add = (label, path) => {
    const a = el("a", null, label);
    a.addEventListener("click", () => writeHash(path, null, true));
    c.append(a);
  };
  add(rootName, "");
  parts.forEach((p, i) => {
    c.append(el("span", "sep", "/"));
    add(p, parts.slice(0, i + 1).join("/"));
  });
}

function renderGrid() {
  const q = $("filter").value.trim().toLowerCase();
  state.view = q ? state.files.filter((f) => f.name.toLowerCase().includes(q)) : state.files.slice();
  observer.disconnect();
  thumbs.clear();
  cards.clear();
  selected.splice(0, selected.length, ...selected.filter((f) => state.view.includes(f)));
  $("g-cmp").hidden = selected.length !== 2;
  const frag = document.createDocumentFragment();
  state.view.forEach((f, i) => {
    const card = el("button", "card");
    card.dataset.i = i;
    const pic = el("div", "pic");
    const img = el("img");
    img.alt = "";
    img.decoding = "async";
    pic.append(img);
    const meta = el("div", "meta");
    meta.append(el("span", "dims", fmtDims(f)), el("span", "size", fmtSize(f.size)),
                el("span", "date", fmtDate(f.mtime)));
    card.append(pic, el("div", "name", f.name), meta);
    card.title = f.name;
    card.classList.toggle("sel", selected.includes(f));
    card._file = f;
    cards.set(f, card);
    frag.append(card);
  });
  grid.replaceChildren(frag);
  grid.scrollTop = 0;
  for (const c of cards.values()) observer.observe(c);
  $("empty").hidden = state.view.length > 0;
  updateStatus();
}

// Ctrl/Cmd-click selects up to two files to compare; a plain click opens one.
const selected = [];
function toggleSelect(f) {
  const k = selected.indexOf(f);
  if (k >= 0) selected.splice(k, 1);
  else { selected.push(f); if (selected.length > 2) selected.shift(); }
  for (const [file, card] of cards) card.classList.toggle("sel", selected.includes(file));
  $("g-cmp").hidden = selected.length !== 2;
}
function compareSelected() {
  if (selected.length !== 2) return;
  const [a, b] = selected.map((f) => f.name);
  state.openedByPush = true;
  writeHash(state.dir, a, true, b);
}
$("g-cmp").addEventListener("click", compareSelected);

grid.addEventListener("click", (e) => {
  const card = e.target.closest(".card");
  if (!card) return;
  if (e.ctrlKey || e.metaKey) return toggleSelect(card._file);
  state.openedByPush = true;
  writeHash(state.dir, card._file.name, true);
});

// Arrow-key navigation between cards.
grid.addEventListener("keydown", (e) => {
  const card = e.target.closest(".card");
  if (!card || !["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown", "Home", "End"].includes(e.key)) return;
  e.preventDefault();
  const i = +card.dataset.i;
  const cols = $("main").classList.contains("table") ? 1 : getComputedStyle(grid).gridTemplateColumns.split(" ").length;
  const n = state.view.length;
  const j = { ArrowLeft: i - 1, ArrowRight: i + 1, ArrowUp: i - cols, ArrowDown: i + cols, Home: 0, End: n - 1 }[e.key];
  const target = grid.children[Math.max(0, Math.min(n - 1, j))];
  target?.focus();
  target?.scrollIntoView({ block: "nearest" });
});

// ---------------------------------------------------------------- thumbnails
// Cached thumbs load straight from disk. Missing ones are rendered by the browser
// (the same engine that makes Firefox fast), one at a time, visible cards first,
// and uploaded to the cache so the next visit is instant.

const THUMB_PX = 400;

const observer = new IntersectionObserver((entries) => {
  for (const e of entries) {
    const card = e.target;
    const f = card._file;
    if (e.isIntersecting) {
      const img = card.querySelector("img");
      if (img.hasAttribute("src")) continue;
      if (f.thumb) img.src = thumbUrl(f);
      else thumbs.add(f);
    } else {
      thumbs.remove(f);
    }
  }
}, { root: grid, rootMargin: "400px 0px" });

const thumbs = {
  pending: new Set(),
  running: 0,
  paused: false,
  generation: 0,
  add(f) { this.pending.add(f); this.pump(); },
  remove(f) { this.pending.delete(f); },
  clear() { this.pending.clear(); this.generation++; },
  pause(p) { this.paused = p; if (!p) this.pump(); },
  pump() {
    updateStatus();
    while (!this.paused && this.running < 2 && this.pending.size) {
      const f = this.pending.values().next().value;
      this.pending.delete(f);
      this.running++;
      const gen = this.generation, dir = state.dir;
      renderThumb(f, dir, gen).finally(() => {
        this.running--;
        this.pump();
      });
    }
  },
};

// Drawing a huge SVG blocks whatever thread does it, so it happens in a hidden iframe on
// the *other* loopback hostname. That is a different origin, which Firefox puts in its own
// process, so the UI keeps running while thumbnails are drawn.
const renderer = {
  frame: null,
  ready: null,
  nextId: 1,
  waiting: new Map(),
  start() {
    if (this.ready) return this.ready;
    const host = location.hostname === "localhost" ? "127.0.0.1" : "localhost";
    this.origin = location.protocol + "//" + host + ":" + location.port;
    this.ready = new Promise((resolve) => {
      this.frame = el("iframe");
      this.frame.hidden = true;
      this.frame.src = this.origin + BASE + "static/render.html";
      this.onReady = resolve;
      document.body.append(this.frame);
    });
    window.addEventListener("message", (e) => {
      if (e.origin !== this.origin) return;
      if (e.data?.ready) return this.onReady();
      this.waiting.get(e.data?.id)?.(e.data);
    });
    return this.ready;
  },
  async render(msg) {
    await this.start();
    const id = this.nextId++;
    return new Promise((resolve) => {
      this.waiting.set(id, (r) => { this.waiting.delete(id); resolve(r); });
      this.frame.contentWindow.postMessage({ type: "render", id, ...msg }, this.origin);
    });
  },
};

async function renderThumb(f, dir, gen) {
  const card = cards.get(f);
  card?.classList.add("busy");
  try {
    const r = await renderer.render({ url: fileUrl(dir, f, !f.intrinsic), key: f.key, w: f.w, h: f.h, max: THUMB_PX });
    if (r.error) throw new Error(r.error);
    if (r.cached) f.thumb = true;
    const img = cards.get(f)?.querySelector("img");
    if (img && gen === thumbs.generation) img.src = r.cached ? thumbUrl(f) : URL.createObjectURL(r.blob);
  } catch (e) {
    const pic = cards.get(f)?.querySelector(".pic");
    if (pic && !pic.querySelector(".note")) pic.append(el("span", "note", "⚠ cannot render"));
    console.warn("thumbnail failed", f.name, e);
  } finally {
    cards.get(f)?.classList.remove("busy");
  }
}

function updateStatus() {
  const n = state.view.length;
  const left = thumbs.pending.size + thumbs.running;
  let s = n + (n === 1 ? " file" : " files");
  if (state.view.length !== state.files.length) s += " (of " + state.files.length + ")";
  if (left) s += " · rendering thumbnails…";
  $("status").textContent = s;
}

// ---------------------------------------------------------------- viewer
// Each pane has a layer sized in CSS px to its `committed` zoom, so Firefox rasterises
// the SVG at the right resolution. While wheel-zooming we only change a CSS scale
// on top of that (cheap, slightly blurry) and commit the real size once the
// user pauses, which triggers one crisp re-render.
// Compare mode shows two panes; with `link` on, pan and zoom are applied to both.

const viewer = $("viewer"), panesEl = $("panes");
const MIN_ZOOM = 0.01, MAX_ZOOM = 64;
const panes = [];
const cmp = { layout: prefs.get("layout", "side"), link: prefs.get("link", "1") === "1" };
const active = () => panes.filter((p) => !p.el.hidden);
const targets = (p) => (!p || cmp.link ? active() : [p]);

class Pane {
  constructor() {
    this.el = el("div", "pane");
    this.stage = el("div", "stage");
    this.layer = el("div", "layer");
    this.thumb = el("img", "thumb");
    this.img = el("img", "img");
    this.thumb.alt = this.img.alt = "";
    this.load = el("div", "load");
    this.cap = el("div", "cap");
    this.layer.append(this.thumb, this.img);
    this.stage.append(this.layer, this.load);
    this.el.append(this.stage, this.cap);
    Object.assign(this, { f: null, s: 1, x: 0, y: 0, committed: 1, fitted: true, token: 0, timer: 0, drag: null });
    this.bind();
  }

  setFile(f) {
    this.f = f;
    const token = ++this.token;
    const t0 = performance.now();
    this.cap.replaceChildren(el("b", null, f.name), el("span", null, fmtDims(f) + " · " + fmtSize(f.size)));
    this.timeEl = el("span");
    this.cap.append(this.timeEl);
    $("v-time").textContent = "";

    // Show the (blurry) thumbnail instantly, then swap in the real render.
    const cardImg = cards.get(f)?.querySelector("img");
    this.thumb.src = cardImg?.getAttribute("src") || (f.thumb ? thumbUrl(f) : "");
    this.thumb.hidden = !this.thumb.getAttribute("src");
    this.img.classList.remove("ready");
    this.img.removeAttribute("src");
    this.load.replaceChildren(el("span", "spinner"), " rendering…");
    this.load.hidden = false;

    this.img.src = fileUrl(state.dir, f);
    this.img.decode().then(() => {
      if (token !== this.token) return;
      // decode() resolves once parsed; wait two frames so the paint is included.
      requestAnimationFrame(() => requestAnimationFrame(() => {
        if (token !== this.token) return;
        this.img.classList.add("ready");
        this.thumb.hidden = true;
        this.load.hidden = true;
        const ms = Math.round(performance.now() - t0) + " ms";
        this.timeEl.textContent = ms;
        if (this === panes[0]) $("v-time").textContent = ms;
        if (!active()[1]) prefetch(state.current + 1);
      }));
    }).catch(() => {
      if (token !== this.token) return;
      this.load.textContent = "⚠ cannot render this file";
    });
  }

  clear() {
    this.token++;
    this.f = null;
    this.img.removeAttribute("src");
    this.thumb.removeAttribute("src");
    this.cap.replaceChildren();
  }

  apply() {
    this.layer.style.transform = `translate(${this.x}px, ${this.y}px) scale(${this.s / this.committed})`;
    showZoom();
  }

  commit() {
    this.committed = this.s;
    this.layer.style.width = this.f.w * this.s + "px";
    this.layer.style.height = this.f.h * this.s + "px";
    this.layer.classList.remove("moving");
    this.apply();
  }

  interact() {
    this.layer.classList.add("moving");
    clearTimeout(this.timer);
    this.timer = setTimeout(() => this.commit(), 250);
  }

  fitScale() {
    const pad = 24;
    return Math.max(MIN_ZOOM, Math.min((this.stage.clientWidth - pad * 2) / this.f.w,
                                       (this.stage.clientHeight - pad * 2) / this.f.h));
  }

  fit(s) {
    if (!this.f) return;
    this.s = s ?? this.fitScale();
    this.x = (this.stage.clientWidth - this.f.w * this.s) / 2;
    this.y = (this.stage.clientHeight - this.f.h * this.s) / 2;
    this.fitted = true;
    clearTimeout(this.timer);
    this.commit();
  }

  zoomAt(factor, cx, cy, now) {
    if (!this.f) return;
    const s = Math.max(MIN_ZOOM, Math.min(MAX_ZOOM, this.s * factor));
    this.x = cx - (cx - this.x) * (s / this.s);
    this.y = cy - (cy - this.y) * (s / this.s);
    this.s = s;
    this.fitted = false;
    if (now) { clearTimeout(this.timer); this.commit(); }
    else { this.apply(); this.interact(); }
  }

  panBy(dx, dy) {
    this.x += dx;
    this.y += dy;
    this.fitted = false;
    this.apply();
  }

  bind() {
    const st = this.stage;
    st.addEventListener("wheel", (e) => {
      e.preventDefault();
      const r = st.getBoundingClientRect();
      const unit = e.deltaMode === 1 ? 16 : e.deltaMode === 2 ? 400 : 1;
      const k = Math.exp(-e.deltaY * unit * 0.0015);
      targets(this).forEach((q) => q.zoomAt(k, e.clientX - r.left, e.clientY - r.top));
    }, { passive: false });
    st.addEventListener("pointerdown", (e) => {
      if (e.button !== 0) return;
      this.drag = { x: e.clientX, y: e.clientY };
      st.setPointerCapture(e.pointerId);
      st.classList.add("dragging");
    });
    st.addEventListener("pointermove", (e) => {
      if (!this.drag) return;
      const dx = e.clientX - this.drag.x, dy = e.clientY - this.drag.y;
      this.drag = { x: e.clientX, y: e.clientY };
      targets(this).forEach((q) => q.panBy(dx, dy));
    });
    const end = () => { this.drag = null; st.classList.remove("dragging"); };
    st.addEventListener("pointerup", end);
    st.addEventListener("pointercancel", end);
    st.addEventListener("dblclick", (e) => {
      const r = st.getBoundingClientRect();
      if (Math.abs(this.s - 1) < 0.01) fitGroup(this);
      else targets(this).forEach((q) => q.zoomAt(1 / q.s, e.clientX - r.left, e.clientY - r.top, true));
    });
    new ResizeObserver(() => { if (this.f && this.fitted && !this.el.hidden) fitGroup(this); }).observe(st);
  }
}

// Fit the panes in the group; when linked they share one scale so sizes stay comparable.
function fitGroup(p) {
  const ps = targets(p).filter((q) => q.f);
  const s = cmp.link && ps.length > 1 ? Math.min(...ps.map((q) => q.fitScale())) : undefined;
  ps.forEach((q) => q.fit(s));
}

function showZoom() {
  const z = active().filter((p) => p.f).map((p) => Math.round(p.s * 100));
  $("v-zoom").textContent = z.every((v) => v === z[0]) ? z[0] + "%" : z.join("% | ") + "%";
}

function ensurePanes(n) {
  while (panes.length < n) {
    const p = new Pane();
    panes.push(p);
    panesEl.append(p.el);
  }
  panes.forEach((p, i) => {
    const hide = i >= n;
    if (hide && !p.el.hidden) p.clear();
    p.el.hidden = hide;
  });
}

// Put file `f` into pane `p`, fitting it (and the neighbours if they are still fitted).
function place(p, f) {
  if (p.f === f) return;
  p.setFile(f);
  const others = active().filter((q) => q !== p && q.f);
  if (others.every((q) => q.fitted)) fitGroup(null);
  else if (cmp.link) p.fit(others[0].s);
  else p.fit();
}

function openViewer(i, j = -1) {
  const compare = j >= 0;
  viewer.hidden = false;
  viewer.classList.toggle("compare", compare);
  panesEl.classList.toggle("stack", cmp.layout === "stack");
  thumbs.pause(true);
  state.current = i;
  state.cmp = j;
  ensurePanes(compare ? 2 : 1);
  place(panes[0], state.view[i]);
  if (compare) place(panes[1], state.view[j]);

  const n = state.view.length, fa = state.view[i], fb = state.view[j];
  $("v-name").textContent = compare ? fa.name + "  ⇄  " + fb.name : fa.name;
  $("v-info").textContent = compare ? "" : fmtDims(fa) + " · " + fmtSize(fa.size);
  $("v-pos").textContent = compare ? `A ${i + 1} · B ${j + 1} / ${n}` : i + 1 + " / " + n;
  $("v-raw").href = fileUrl(state.dir, fa);
  $("v-cmp").classList.toggle("on", compare);
  $("v-link").classList.toggle("on", cmp.link);
  showZoom();
}

function prefetch(i) {
  const f = state.view[i];
  if (!f || f.size > 64 * 1024 * 1024) return;
  const img = new Image();
  img.src = fileUrl(state.dir, f);
}

function closeViewer() {
  if (viewer.hidden) return;
  viewer.hidden = true;
  const f = panes[0]?.f;
  panes.forEach((p) => p.clear());
  state.current = state.cmp = -1;
  state.openedByPush = false;
  thumbs.pause(false);
  const card = f && cards.get(f);
  if (card) { card.focus({ preventScroll: true }); card.scrollIntoView({ block: "nearest" }); }
}

function leaveViewer() {
  if (state.openedByPush) { state.openedByPush = false; history.back(); }
  else writeHash(state.dir, null, false);
}

// Show the viewer state in the URL. `b` is the comparison file index or -1.
function go(a, b) {
  writeHash(state.dir, state.view[a].name, false, b >= 0 ? state.view[b].name : null);
}

// Step which file is shown. In compare mode `which` picks pane 0 (A) or 1 (B).
function step(d, which = 0) {
  const n = state.view.length;
  if (!n) return;
  if (state.cmp < 0) return go((state.current + d + n) % n, -1);
  let a = state.current, b = state.cmp;
  const other = which ? a : b;
  let i = which ? b : a;
  do { i = (i + d + n) % n; } while (i === other && n > 1);
  which ? (b = i) : (a = i);
  go(a, b);
}

function toggleCompare() {
  const n = state.view.length;
  if (state.current < 0) return;
  if (state.cmp >= 0) return go(state.current, -1);
  if (n < 2) return;
  go(state.current, (state.current + 1) % n);
}

function setLayout(l) {
  cmp.layout = l;
  prefs.set("layout", l);
  panesEl.classList.toggle("stack", l === "stack");
}
function setLink(on) {
  cmp.link = on;
  prefs.set("link", on ? "1" : "0");
  $("v-link").classList.toggle("on", on);
  if (on) fitGroup(null);
}

const zoomKeys = (k) => active().forEach((p) => p.zoomAt(k, p.stage.clientWidth / 2, p.stage.clientHeight / 2, true));

$("v-close").addEventListener("click", leaveViewer);
$("v-prev").addEventListener("click", () => step(-1, 1));
$("v-next").addEventListener("click", () => step(1, 1));
$("v-fit").addEventListener("click", () => fitGroup(null));
$("v-100").addEventListener("click", () => active().forEach((p) => p.zoomAt(1 / p.s, p.stage.clientWidth / 2, p.stage.clientHeight / 2, true)));
$("v-cmp").addEventListener("click", toggleCompare);
$("v-layout").addEventListener("click", () => setLayout(cmp.layout === "stack" ? "side" : "stack"));
$("v-link").addEventListener("click", () => setLink(!cmp.link));

// ---------------------------------------------------------------- controls & keys

$("filter").addEventListener("input", () => renderGrid());
$("sort").addEventListener("change", () => {
  sortRev = false;
  prefs.set("sort", $("sort").value);
  prefs.set("rev", "0");
  sortFiles();
  renderGrid();
});
// Clicking a column header sorts by it; clicking the active one reverses the order.
$("thead").addEventListener("click", (e) => {
  const key = e.target.closest("[data-sort]")?.dataset.sort;
  if (!key) return;
  if ($("sort").value === key) sortRev = !sortRev;
  else { $("sort").value = key; sortRev = false; }
  prefs.set("sort", key);
  prefs.set("rev", sortRev ? "1" : "0");
  sortFiles();
  renderGrid();
});

function setView(v) {
  $("main").classList.toggle("table", v === "table");
  $("thead").hidden = v !== "table";
  prefs.set("view", v);
}
const toggleView = () => setView($("main").classList.contains("table") ? "grid" : "table");
$("view").addEventListener("click", toggleView);
$("tile").addEventListener("input", (e) => {
  document.documentElement.style.setProperty("--tile", e.target.value + "px");
  prefs.set("tile", e.target.value);
});
const BGS = ["white", "checker", "dark"];
function cycleBg() {
  const next = BGS[(BGS.indexOf(document.body.dataset.bg) + 1) % BGS.length];
  document.body.dataset.bg = next;
  prefs.set("bg", next);
}
$("bg").addEventListener("click", cycleBg);
function toggleSide() {
  document.body.classList.toggle("no-side");
  prefs.set("side", document.body.classList.contains("no-side") ? "0" : "1");
}
$("toggle-side").addEventListener("click", toggleSide);
function refresh() {
  listCache.clear();
  treeNodes.clear();
  $("tree").replaceChildren();
  state.dir = null;
  route();
}
$("refresh").addEventListener("click", refresh);

async function openFolder() {
  const btn = $("open");
  btn.disabled = true;
  try {
    const r = await fetch(BASE + "api/open", { method: "POST", headers: { "X-Esvigy": "1" } });
    if (r.status === 204) return; // dialog cancelled
    if (!r.ok) { alert(await r.text()); return; }
    history.replaceState(null, "", "#");
    refresh();
  } finally {
    btn.disabled = false;
  }
}
$("open").addEventListener("click", openFolder);

document.addEventListener("keydown", (e) => {
  if (e.ctrlKey || e.altKey || e.metaKey) return;
  const typing = e.target.matches("input[type=search], input[type=text]");
  if (typing) {
    if (e.key === "Escape") { e.target.value = ""; e.target.blur(); renderGrid(); }
    if (e.key === "Enter") grid.querySelector(".card")?.focus();
    return;
  }
  if (!viewer.hidden) {
    const sh = e.shiftKey;
    const keys = {
      Escape: leaveViewer, Backspace: leaveViewer,
      // In compare mode the arrows move B, Shift+arrows move A.
      ArrowLeft: () => step(-1, sh ? 0 : 1), ArrowRight: () => step(1, sh ? 0 : 1),
      PageUp: () => step(-1, 1), PageDown: () => step(1, 1), " ": () => step(1, 1),
      Home: () => go(0, state.cmp), End: () => go(state.view.length - 1, state.cmp),
      f: () => fitGroup(null), "0": () => fitGroup(null), "1": () => $("v-100").click(),
      "+": () => zoomKeys(1.25), "=": () => zoomKeys(1.25), "-": () => zoomKeys(0.8),
      c: toggleCompare, l: () => setLayout(cmp.layout === "stack" ? "side" : "stack"), y: () => setLink(!cmp.link),
    };
    const k = keys[e.key];
    if (k) { e.preventDefault(); k(); return; }
  }
  if (e.key === "c" && viewer.hidden) compareSelected();
  else if (e.key === "/") { e.preventDefault(); $("filter").focus(); }
  else if (e.key === "s") toggleSide();
  else if (e.key === "b") cycleBg();
  else if (e.key === "r") refresh();
  else if (e.key === "o") openFolder();
  else if (e.key === "t") toggleView();
});

// ---------------------------------------------------------------- start

(function init() {
  $("sort").value = prefs.get("sort", "name");
  setView(prefs.get("view", "grid"));
  const tile = prefs.get("tile", "180");
  $("tile").value = tile;
  document.documentElement.style.setProperty("--tile", tile + "px");
  document.body.dataset.bg = prefs.get("bg", "white");
  if (prefs.get("side", "1") === "0") document.body.classList.add("no-side");
  route();
})();
