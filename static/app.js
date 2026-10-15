"use strict";

// ---------------------------------------------------------------- utilities

const $ = (id) => document.getElementById(id);
const el = (tag, cls, text) => {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text != null) e.textContent = text;
  return e;
};
const joinPath = (dir, name) => (dir ? dir + "/" + name : name);
const encPath = (p) => p.split("/").map(encodeURIComponent).join("/");
const fileUrl = (dir, f, fix) => "/file/" + encPath(joinPath(dir, f.name)) + (fix ? "?fix" : "");
const thumbUrl = (f) => "/thumb/" + f.key + ".webp";
const collator = new Intl.Collator(undefined, { numeric: true, sensitivity: "base" });

function fmtSize(n) {
  if (n < 1024) return n + " B";
  const u = ["KB", "MB", "GB"];
  let i = -1;
  do { n /= 1024; i++; } while (n >= 1024 && i < u.length - 1);
  return n.toFixed(n < 10 ? 1 : 0) + " " + u[i];
}
const fmtDims = (f) => Math.round(f.w) + "×" + Math.round(f.h);

const prefs = {
  get(k, d) { try { return localStorage.getItem("esvigy." + k) ?? d; } catch { return d; } },
  set(k, v) { try { localStorage.setItem("esvigy." + k, v); } catch {} },
};

// ---------------------------------------------------------------- data

const listCache = new Map();
function list(dir) {
  if (!listCache.has(dir)) {
    const p = fetch("/api/list?dir=" + encodeURIComponent(dir)).then((r) => {
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
  openedByPush: false,
};

// ---------------------------------------------------------------- routing
// URL hash: #dir=<path>&file=<name>. Opening a file pushes history, so Back closes it.

function readHash() {
  const p = new URLSearchParams(location.hash.slice(1));
  return { dir: p.get("dir") || "", file: p.get("file") };
}
function writeHash(dir, file, push) {
  const p = new URLSearchParams();
  if (dir) p.set("dir", dir);
  if (file) p.set("file", file);
  const h = "#" + p.toString().replace(/\+/g, "%20");
  if (push) location.hash = h;
  else { history.replaceState(null, "", h); route(); }
}

let routeToken = 0;
async function route() {
  const token = ++routeToken;
  const { dir, file } = readHash();
  if (dir !== state.dir) {
    closeViewer();
    await loadDir(dir);
    if (token !== routeToken) return;
  }
  const i = file ? state.view.findIndex((f) => f.name === file) : -1;
  if (i >= 0) openViewer(i);
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

function sortFiles() {
  const key = $("sort").value;
  const cmp = {
    name: (a, b) => collator.compare(a.name, b.name),
    size: (a, b) => b.size - a.size,
    mtime: (a, b) => b.mtime - a.mtime,
  }[key];
  state.files.sort(cmp);
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
  const frag = document.createDocumentFragment();
  state.view.forEach((f, i) => {
    const card = el("button", "card");
    card.dataset.i = i;
    const pic = el("div", "pic");
    const img = el("img");
    img.alt = "";
    img.decoding = "async";
    pic.append(img);
    card.append(pic, el("div", "name", f.name), el("div", "meta", fmtDims(f) + " · " + fmtSize(f.size)));
    card.title = f.name;
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

grid.addEventListener("click", (e) => {
  const card = e.target.closest(".card");
  if (!card) return;
  state.openedByPush = true;
  writeHash(state.dir, card._file.name, true);
});

// Arrow-key navigation between cards.
grid.addEventListener("keydown", (e) => {
  const card = e.target.closest(".card");
  if (!card || !["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown", "Home", "End"].includes(e.key)) return;
  e.preventDefault();
  const i = +card.dataset.i;
  const cols = getComputedStyle(grid).gridTemplateColumns.split(" ").length;
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
    while (!this.paused && this.running < 1 && this.pending.size) {
      const f = this.pending.values().next().value;
      this.pending.delete(f);
      this.running++;
      const gen = this.generation, dir = state.dir;
      renderThumb(f, dir, gen).finally(() => {
        this.running--;
        // Yield a frame so the UI stays responsive between heavy renders.
        requestAnimationFrame(() => this.pump());
      });
    }
  },
};

async function renderThumb(f, dir, gen) {
  const card = cards.get(f);
  card?.classList.add("busy");
  const src = new Image();
  try {
    src.src = fileUrl(dir, f, !f.intrinsic);
    await src.decode();
    const s = Math.min(THUMB_PX / f.w, THUMB_PX / f.h);
    const w = Math.max(1, Math.round(f.w * s)), h = Math.max(1, Math.round(f.h * s));
    const canvas = document.createElement("canvas");
    canvas.width = w;
    canvas.height = h;
    canvas.getContext("2d").drawImage(src, 0, 0, w, h);
    const blob = await new Promise((r) => canvas.toBlob(r, "image/webp", 0.85));
    if (!blob) throw new Error("encode failed");
    if (blob.type === "image/webp") {
      const r = await fetch(thumbUrl(f), { method: "PUT", body: blob });
      if (r.ok) f.thumb = true;
    }
    const img = cards.get(f)?.querySelector("img");
    if (img && gen === thumbs.generation) img.src = URL.createObjectURL(blob);
  } catch (e) {
    const pic = cards.get(f)?.querySelector(".pic");
    if (pic && !pic.querySelector(".note")) pic.append(el("span", "note", "⚠ cannot render"));
    console.warn("thumbnail failed", f.name, e);
  } finally {
    src.src = "";
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
// The layer is sized in CSS px to `committed` zoom, so Firefox rasterises the
// SVG at the right resolution. While wheel-zooming we only change a CSS scale
// on top of that (cheap, slightly blurry) and commit the real size once the
// user pauses, which triggers one crisp re-render.

const viewer = $("viewer"), stage = $("stage"), layer = $("layer");
const vImg = $("v-img"), vThumb = $("v-thumb"), vLoad = $("v-load");
const view = { s: 1, x: 0, y: 0, committed: 1, f: null, fitted: true };
const MIN_ZOOM = 0.01, MAX_ZOOM = 64;
let commitTimer = 0, loadToken = 0;

function openViewer(i) {
  const f = state.view[i];
  state.current = i;
  viewer.hidden = false;
  thumbs.pause(true);
  $("v-name").textContent = f.name;
  $("v-info").textContent = fmtDims(f) + " · " + fmtSize(f.size);
  $("v-pos").textContent = i + 1 + " / " + state.view.length;
  $("v-raw").href = fileUrl(state.dir, f);
  $("v-time").textContent = "";
  if (view.f === f) return;
  view.f = f;

  // Show the (blurry) thumbnail instantly, then swap in the real render.
  const cardImg = cards.get(f)?.querySelector("img");
  vThumb.src = cardImg?.getAttribute("src") || (f.thumb ? thumbUrl(f) : "");
  vThumb.hidden = !vThumb.getAttribute("src");
  vImg.classList.remove("ready");
  vImg.removeAttribute("src");
  vLoad.replaceChildren(el("span", "spinner"), " rendering…");
  vLoad.hidden = false;
  fit();

  const token = ++loadToken;
  const t0 = performance.now();
  vImg.src = fileUrl(state.dir, f);
  vImg.decode().then(() => {
    if (token !== loadToken) return;
    // decode() resolves once parsed; wait two frames so the paint is included.
    requestAnimationFrame(() => requestAnimationFrame(() => {
      if (token !== loadToken) return;
      vImg.classList.add("ready");
      vThumb.hidden = true;
      vLoad.hidden = true;
      $("v-time").textContent = Math.round(performance.now() - t0) + " ms";
      prefetch(i + 1);
    }));
  }).catch(() => {
    if (token !== loadToken) return;
    vLoad.hidden = false;
    vLoad.textContent = "⚠ cannot render this file";
  });
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
  loadToken++;
  vImg.removeAttribute("src");
  vThumb.removeAttribute("src");
  const f = view.f;
  view.f = null;
  state.current = -1;
  state.openedByPush = false;
  thumbs.pause(false);
  const card = f && cards.get(f);
  if (card) { card.focus({ preventScroll: true }); card.scrollIntoView({ block: "nearest" }); }
}

function leaveViewer() {
  if (state.openedByPush) { state.openedByPush = false; history.back(); }
  else writeHash(state.dir, null, false);
}

function step(d) {
  const n = state.view.length;
  if (!n) return;
  const i = (state.current + d + n) % n;
  writeHash(state.dir, state.view[i].name, false);
}

function apply() {
  const k = view.s / view.committed;
  layer.style.transform = `translate(${view.x}px, ${view.y}px) scale(${k})`;
  $("v-zoom").textContent = Math.round(view.s * 100) + "%";
}

function commit() {
  view.committed = view.s;
  layer.style.width = view.f.w * view.s + "px";
  layer.style.height = view.f.h * view.s + "px";
  layer.classList.remove("moving");
  apply();
}

function interact() {
  layer.classList.add("moving");
  clearTimeout(commitTimer);
  commitTimer = setTimeout(commit, 250);
}

function fit() {
  if (!view.f) return;
  const W = stage.clientWidth, H = stage.clientHeight, pad = 24;
  view.s = Math.max(MIN_ZOOM, Math.min((W - pad * 2) / view.f.w, (H - pad * 2) / view.f.h));
  center();
  view.fitted = true;
}

function actual(cx, cy) {
  if (cx == null) { cx = stage.clientWidth / 2; cy = stage.clientHeight / 2; }
  zoomAt(1 / view.s, cx, cy, true);
}

function center() {
  view.x = (stage.clientWidth - view.f.w * view.s) / 2;
  view.y = (stage.clientHeight - view.f.h * view.s) / 2;
  clearTimeout(commitTimer);
  commit();
}

function zoomAt(factor, cx, cy, now) {
  const s = Math.max(MIN_ZOOM, Math.min(MAX_ZOOM, view.s * factor));
  view.x = cx - (cx - view.x) * (s / view.s);
  view.y = cy - (cy - view.y) * (s / view.s);
  view.s = s;
  view.fitted = false;
  if (now) { clearTimeout(commitTimer); commit(); }
  else { apply(); interact(); }
}

stage.addEventListener("wheel", (e) => {
  e.preventDefault();
  const r = stage.getBoundingClientRect();
  const unit = e.deltaMode === 1 ? 16 : e.deltaMode === 2 ? 400 : 1;
  zoomAt(Math.exp(-e.deltaY * unit * 0.0015), e.clientX - r.left, e.clientY - r.top);
}, { passive: false });

let drag = null;
stage.addEventListener("pointerdown", (e) => {
  if (e.button !== 0) return;
  drag = { x: e.clientX, y: e.clientY };
  stage.setPointerCapture(e.pointerId);
  stage.classList.add("dragging");
});
stage.addEventListener("pointermove", (e) => {
  if (!drag) return;
  view.x += e.clientX - drag.x;
  view.y += e.clientY - drag.y;
  drag = { x: e.clientX, y: e.clientY };
  view.fitted = false;
  apply();
});
const endDrag = () => { drag = null; stage.classList.remove("dragging"); };
stage.addEventListener("pointerup", endDrag);
stage.addEventListener("pointercancel", endDrag);
stage.addEventListener("dblclick", (e) => {
  const r = stage.getBoundingClientRect();
  if (Math.abs(view.s - 1) < 0.01) fit();
  else actual(e.clientX - r.left, e.clientY - r.top);
});
new ResizeObserver(() => { if (view.f && view.fitted) fit(); }).observe(stage);

$("v-close").addEventListener("click", leaveViewer);
$("v-prev").addEventListener("click", () => step(-1));
$("v-next").addEventListener("click", () => step(1));
$("v-fit").addEventListener("click", fit);
$("v-100").addEventListener("click", () => actual());

// ---------------------------------------------------------------- controls & keys

$("filter").addEventListener("input", () => renderGrid());
$("sort").addEventListener("change", () => { prefs.set("sort", $("sort").value); sortFiles(); renderGrid(); });
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

document.addEventListener("keydown", (e) => {
  if (e.ctrlKey || e.altKey || e.metaKey) return;
  const typing = e.target.matches("input[type=search], input[type=text]");
  if (typing) {
    if (e.key === "Escape") { e.target.value = ""; e.target.blur(); renderGrid(); }
    if (e.key === "Enter") grid.querySelector(".card")?.focus();
    return;
  }
  if (!viewer.hidden) {
    const cx = stage.clientWidth / 2, cy = stage.clientHeight / 2;
    const keys = {
      Escape: leaveViewer, Backspace: leaveViewer,
      ArrowLeft: () => step(-1), ArrowRight: () => step(1), PageUp: () => step(-1), PageDown: () => step(1),
      " ": () => step(1),
      Home: () => writeHash(state.dir, state.view[0]?.name, false),
      End: () => writeHash(state.dir, state.view.at(-1)?.name, false),
      f: fit, "0": fit, "1": () => actual(),
      "+": () => zoomAt(1.25, cx, cy, true), "=": () => zoomAt(1.25, cx, cy, true),
      "-": () => zoomAt(0.8, cx, cy, true),
    };
    const k = keys[e.key];
    if (k) { e.preventDefault(); k(); return; }
  }
  if (e.key === "/") { e.preventDefault(); $("filter").focus(); }
  else if (e.key === "s") toggleSide();
  else if (e.key === "b") cycleBg();
  else if (e.key === "r") refresh();
});

// ---------------------------------------------------------------- start

(function init() {
  $("sort").value = prefs.get("sort", "name");
  const tile = prefs.get("tile", "180");
  $("tile").value = tile;
  document.documentElement.style.setProperty("--tile", tile + "px");
  document.body.dataset.bg = prefs.get("bg", "white");
  if (prefs.get("side", "1") === "0") document.body.classList.add("no-side");
  route();
})();
