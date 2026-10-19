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
// `v` (the file's cache key, which changes with the file) keeps the browser from showing a stale copy.
const fileUrl = (dir, f, fix) => BASE + "file/" + encPath(joinPath(dir, f.name)) + "?v=" + f.key + (fix ? "&fix" : "");
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
// Short format label ("PNG", "PDF", …) from the extension; the grid colours badges by it.
const TYPE_LABELS = { jpeg: "JPG", tif: "TIFF" };
const fileType = (f) => {
  const ext = f.name.slice(f.name.lastIndexOf(".") + 1).toLowerCase();
  return TYPE_LABELS[ext] || ext.toUpperCase();
};
// PDFs and TIFFs can have pages; they are drawn into a canvas, one page at a time.
const isPaged = (f) => f.kind === "pdf" || f.kind === "tiff";
const docsFor = (f) => (f.kind === "tiff" ? tiffDocs : pdfDocs);
const fmtDims = (f) => {
  if (f.pages === null) return fileType(f);  // size unknown until first rendered
  const d = Math.round(f.w) + "×" + Math.round(f.h);
  const pages = " · " + f.pages + (f.pages === 1 ? " page" : " pages");
  return f.kind === "pdf" || f.pages > 1 ? d + pages : d;
};

// The browser measured a file (size, page count): remember it and refresh the card.
function updateMeta(f, meta) {
  if (!meta) return;
  Object.assign(f, meta);
  const dims = cards.get(f)?.querySelector(".dims");
  if (dims) dims.textContent = fmtDims(f);
}

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

// Subfolders only (for the tree): cheap for the server, which then needn't read any file.
// A full listing already loaded for the grid does as well.
const dirCache = new Map();
function listDirs(dir) {
  if (listCache.has(dir)) return listCache.get(dir);
  if (!dirCache.has(dir)) {
    const p = fetch(BASE + "api/list?dirs&dir=" + encodeURIComponent(dir)).then((r) => {
      if (!r.ok) throw new Error(r.status + " " + r.statusText);
      return r.json();
    });
    p.catch(() => dirCache.delete(dir));
    dirCache.set(dir, p);
  }
  return dirCache.get(dir);
}

const state = {
  dir: null,        // currently shown directory ("" = root)
  files: [],        // all SVGs in dir (sorted)
  view: [],         // files after filter
  current: -1,      // index into view while the viewer is open
  cmp: -1,          // index of the comparison file (compare mode), else -1
  sig: null,        // fingerprint of `dir` when it was listed (see checkFolder)
  path: "", sep: "/",  // absolute path of dir and the OS's separator (for copying paths)
  openedByPush: false,
};

// ---------------------------------------------------------------- routing
// URL hash: #dir=<path>&file=<name>[&vs=<name>][&page=<n>][&vspage=<n>]. Opening a file
// pushes history, so Back closes it.

function readHash() {
  const p = new URLSearchParams(location.hash.slice(1));
  const page = (k) => Math.max(1, parseInt(p.get(k), 10) || 1);
  return { dir: p.get("dir") || "", file: p.get("file"), vs: p.get("vs"),
           page: page("page"), vspage: page("vspage") };
}
function writeHash(dir, file, push, vs, page = 1, vspage = 1) {
  const p = new URLSearchParams();
  if (dir) p.set("dir", dir);
  if (file) p.set("file", file);
  if (file && vs) p.set("vs", vs);
  if (file && page > 1) p.set("page", page);
  if (file && vs && vspage > 1) p.set("vspage", vspage);
  const h = "#" + p.toString().replace(/\+/g, "%20");
  if (push) location.hash = h;
  else { history.replaceState(null, "", h); route(); }
}

let routeToken = 0;
async function route() {
  const token = ++routeToken;
  const { dir, file, vs, page, vspage } = readHash();
  if (dir !== state.dir) {
    closeViewer();
    await loadDir(dir);
    if (token !== routeToken) return;
  }
  const i = file ? state.view.findIndex((f) => f.name === file) : -1;
  const j = vs ? state.view.findIndex((f) => f.name === vs) : -1;
  if (i >= 0) openViewer(i, j, page, vspage);
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
  const node = { li, row, ul, twisty, loaded: false, open: false, hasDirs: null };
  treeNodes.set(path, node);
  // A click opens the folder; on the folder already shown it folds it (and its files) away.
  row.addEventListener("click", () => {
    if (path !== state.dir) writeHash(path, null, true);
    else node.open ? collapse(path) : expand(path);
  });
  twisty.addEventListener("click", (e) => {
    e.stopPropagation();
    node.open ? collapse(path) : expand(path);
  });
  return node;
}

// The node for `path`, created along with its ancestors if the tree hasn't been loaded that
// far yet (their other subfolders are filled in by expand() when their listing arrives).
function ensureNode(path) {
  let node = treeNodes.get(path);
  if (node || !path) return node;
  const i = path.lastIndexOf("/");
  const parent = ensureNode(i < 0 ? "" : path.slice(0, i));
  if (!parent) return null;
  node = makeNode(path, path.slice(i + 1));
  parent.ul.append(node.li);
  return node;
}

function showOpen(node) {
  node.open = true;
  node.li.classList.remove("closed");
  node.twisty.textContent = "▾";
  updateTwisty(node);
}

// A folder can be folded if it has subfolders or (the current one) files; unknown counts as yes.
function updateTwisty(node) {
  const files = node.li.querySelector(":scope > ul.files > li");
  node.twisty.style.visibility = node.hasDirs === false && !files ? "hidden" : "";
}

async function expand(path) {
  const node = treeNodes.get(path);
  if (!node) return;
  if (!node.loaded) {
    let data;
    try { data = await listDirs(path); } catch { return; }
    node.loaded = true;
    const dirs = data.dirs.slice().sort(collator.compare);
    // Reuse nodes that exist already (the path to the current folder), so their files stay.
    node.ul.replaceChildren(...dirs.map((d) => {
      const p = joinPath(path, d);
      return (treeNodes.get(p) ?? makeNode(p, d)).li;
    }));
    node.hasDirs = dirs.length > 0;
  }
  showOpen(node);
}

function collapse(path) {
  const node = treeNodes.get(path);
  node.open = false;
  node.li.classList.add("closed");
  node.twisty.textContent = "▸";
}

// Show `dir` in the tree: its path right away, the parents' other subfolders as they load.
async function revealInTree(dir) {
  const parts = dir ? dir.split("/") : [];
  const paths = parts.map((_, i) => parts.slice(0, i + 1).join("/"));
  ensureNode(dir);
  for (const n of treeNodes.values()) n.row.classList.remove("sel");
  for (const p of ["", ...paths]) showOpen(treeNodes.get(p));
  const node = treeNodes.get(dir);
  node.row.classList.add("sel");
  node.row.scrollIntoView({ block: "nearest" });
  await Promise.all(["", ...paths].map(expand));
}

// ---------------------------------------------------------------- files in the sidebar
// The current folder's files, one line each with a tiny thumbnail, listed under its tree node
// (after its subfolders, in their own list, so loading subfolders can't wipe them).

const sideRows = new Map(); // file object -> row
let sideList = null;

const sideObserver = new IntersectionObserver((entries) => {
  for (const e of entries) {
    const mini = e.target.querySelector(".mini");
    if (e.isIntersecting && e.target._file.thumb && !mini.hasAttribute("src")) mini.src = thumbUrl(e.target._file);
  }
}, { root: $("tree"), rootMargin: "200px 0px" });

function renderSideFiles() {
  sideList?.remove();
  sideList = null;
  sideRows.clear();
  sideObserver.disconnect();
  const node = treeNodes.get(state.dir);
  if (!node) return;  // loadDir() builds it (revealInTree) before the grid is rendered
  const ul = el("ul", "files");
  for (const f of state.view) {
    const row = el("div", "row file");
    const mini = el("img", "mini");
    mini.alt = "";
    mini.decoding = "async";
    row.append(el("span", "twisty"), mini, el("span", "label", f.name), el("span", "ab"));
    row._file = f;
    const li = el("li");
    li.append(row);
    ul.append(li);
    sideRows.set(f, row);
  }
  node.li.append(ul);
  sideList = ul;
  updateTwisty(node);
  for (const row of sideRows.values()) sideObserver.observe(row);
  markSideFiles(false);
}

// A thumbnail was just made for `f` (the grid does that): show it in the sidebar too.
function setSideThumb(f, src) {
  const mini = sideRows.get(f)?.querySelector(".mini");
  if (mini) mini.src = src;
}

// Highlight the file(s) open in the viewer; in compare mode mark which side shows which.
function markSideFiles(scroll = true) {
  const a = viewer.hidden ? null : state.view[state.current];
  const b = viewer.hidden || state.cmp < 0 ? null : state.view[state.cmp];
  for (const [f, row] of sideRows) {
    row.classList.toggle("cur", f === a || f === b);
    row.querySelector(".ab").textContent = b ? [f === a && "A", f === b && "B"].filter(Boolean).join(" ") : "";
  }
  if (scroll && a) sideRows.get(b ?? a)?.scrollIntoView({ block: "nearest" });
}

$("tree").addEventListener("mouseover", (e) => {  // full name on hover, with what's known by now
  const row = e.target.closest(".row.file");
  if (row) row.title = row._file.name + "\n" + fmtDims(row._file) + " · " + fmtSize(row._file.size);
});

$("tree").addEventListener("click", (e) => {
  const row = e.target.closest(".row.file");
  if (!row) return;
  const i = state.view.indexOf(row._file);
  if (i < 0) return;
  if (state.cmp >= 0 && !viewer.hidden) {
    // compare: click shows the file on side B, Shift+click on side A; the other side keeps its page
    const [pa, pb] = [panes[0].pageNo, panes[1].pageNo];
    return e.shiftKey ? go(i, state.cmp, 1, pb) : go(state.current, i, pa, 1);
  }
  if (!viewer.hidden) return go(i, -1);  // like stepping: no new history entry
  state.openedByPush = true;
  writeHash(state.dir, row._file.name, true);
});

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
    state.path = "";
    $("copy-path").hidden = true;  // nothing to copy
    return;
  }
  if (state.dir !== dir) return;
  if (!treeNodes.size) {
    const ul = el("ul");
    ul.append(makeNode("", data.root).li);
    $("tree").append(ul);
    document.title = "esvigy — " + data.root;
  }
  revealInTree(dir);  // builds the path to `dir` at once, so renderGrid() can list its files there
  renderCrumbs(dir, data.root);
  state.path = data.path;
  state.sep = data.sep;
  $("copy-path").hidden = false;
  state.files = data.files;
  state.sig = data.sig;
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

// File names may contain any character but "/", including newlines and terminal escape codes
// (archives and downloads can create such names). Pasted into a terminal, those would run the rest
// of the name as a command (an escape code can even end the terminal's bracketed-paste protection),
// so such paths are never copied. Replacing the characters wouldn't help: it would name another file.
const CONTROL_CHARS = /[\x00-\x1f\x7f-\x9f]/;

// Copy buttons: put full paths (one per line) on the clipboard and show ✓ for a moment, or ✗ if a
// path has control characters (the tooltip says why) or the browser refused.
async function copyPath(btn, paths) {
  const unsafe = paths.some((p) => CONTROL_CHARS.test(p));
  let ok = !unsafe;
  if (ok) {
    try { await navigator.clipboard.writeText(paths.join("\n")); } catch { ok = false; }
  }
  clearTimeout(btn._reset);
  btn._title ??= btn.title;
  btn.textContent = ok ? "✓" : "✗";
  if (unsafe) btn.title = "Not copied: the name contains a line break or terminal control code, "
                          + "which could run as a command if pasted into a terminal";
  btn._reset = setTimeout(() => {
    btn.textContent = "⧉";
    btn.title = btn._title;
    btn._title = undefined;
  }, unsafe ? 4000 : 1200);
}
const filePath = (f) => state.path.replace(/[\\/]$/, "") + state.sep + f.name;
$("copy-path").addEventListener("click", (e) => copyPath(e.currentTarget, [state.path]));
$("v-copy").addEventListener("click", (e) => {
  const files = state.cmp >= 0 ? [state.view[state.current], state.view[state.cmp]] : [state.view[state.current]];
  copyPath(e.currentTarget, files.map(filePath));
});

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
    pic.dataset.type = fileType(f);
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
  renderSideFiles();
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

// ---------------------------------------------------------------- files that change while shown
// Every few seconds, ask for the folder's fingerprint (names, modification times, sizes; no file
// is read). If it changed, list the folder again and refresh only what changed: new thumbnails,
// and the viewer reloads a file it shows, keeping zoom and page.

const WATCH_MS = 2000;
let watching = false;

async function checkFolder() {
  const dir = state.dir;
  if (dir === null || !state.sig || document.hidden || watching) return;
  watching = true;
  try {
    const r = await fetch(BASE + "api/sig?dir=" + encodeURIComponent(dir));
    if (!r.ok || state.dir !== dir) return;
    const { sig } = await r.json();
    if (sig && sig !== state.sig) await reloadFolder(dir);
  } catch (e) {
    console.warn("folder check failed", e);
  } finally {
    watching = false;
  }
}
setInterval(checkFolder, WATCH_MS);
document.addEventListener("visibilitychange", checkFolder);

async function reloadFolder(dir) {
  listCache.delete(dir);
  const data = await list(dir);
  if (state.dir !== dir) return;
  state.sig = data.sig;
  const node = treeNodes.get(dir);  // subfolders may have come or gone
  if (node) {
    node.loaded = false;
    dirCache.delete(dir);
    if (node.open) expand(dir);
  }
  const old = new Map(state.files.map((f) => [f.name, f]));
  if (data.files.length !== old.size || !data.files.every((f) => old.has(f.name))) {
    // Files added or removed: rebuild, keeping the scroll position and what the viewer shows.
    const top = grid.scrollTop;
    state.files = data.files;
    sortFiles();
    renderGrid();
    grid.scrollTop = top;
    if (!viewer.hidden) route();
    return;
  }
  for (const nf of data.files) {
    const f = old.get(nf.name);
    if (f.key === nf.key) continue;
    Object.assign(f, nf);  // the same object: its card, sidebar row and panes stay attached
    refreshFile(f);
  }
}

function refreshFile(f) {
  const card = cards.get(f);
  if (card) {
    const img = card.querySelector("img");
    img.removeAttribute("src");
    card.querySelector(".pic .note")?.remove();
    card.querySelector(".dims").textContent = fmtDims(f);
    card.querySelector(".size").textContent = fmtSize(f.size);
    card.querySelector(".date").textContent = fmtDate(f.mtime);
    // A card already in view gets no new visibility event, so queue it here.
    const r = card.getBoundingClientRect(), g = grid.getBoundingClientRect();
    if (f.thumb) img.src = thumbUrl(f);
    else if (r.bottom > g.top - 400 && r.top < g.bottom + 400) thumbs.add(f);
  }
  const mini = sideRows.get(f)?.querySelector(".mini");
  if (mini) {
    mini.removeAttribute("src");
    if (f.thumb) mini.src = thumbUrl(f);
  }
  for (const p of active()) {
    if (p.f !== f) continue;
    p.setFile(f, p.pageNo);  // keeps the pane's zoom and position
    if (p.fitted) fitGroup(p);
    else p.commit();
  }
}

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
// the *other* loopback hostname. That is a different site, which Firefox and Chromium put in their own
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
    const r = await renderer.render({ url: fileUrl(dir, f, !f.intrinsic), kind: f.kind, key: f.key,
                                      w: f.w, h: f.h, max: THUMB_PX });
    updateMeta(f, r.meta);
    if (r.error) throw new Error(r.error);
    if (r.cached) f.thumb = true;
    const img = cards.get(f)?.querySelector("img");
    const src = r.cached ? thumbUrl(f) : URL.createObjectURL(r.blob);
    if (img && gen === thumbs.generation) img.src = src;
    setSideThumb(f, src);
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
  constructor(idx) {
    this.idx = idx;  // 0 = A (left/top), 1 = B
    this.el = el("div", "pane");
    this.stage = el("div", "stage");
    this.layer = el("div", "layer");
    this.thumb = el("img", "thumb");
    this.img = el("img", "img");
    this.thumb.alt = this.img.alt = "";
    this.thumb.draggable = this.img.draggable = false;  // dragging pans; see bind()
    this.load = el("div", "load");
    // Label with the file name and this pane's own ‹ › (compare mode only).
    this.cap = el("div", "cap");
    this.info = el("span", "ci");
    const btn = (text, d, title) => {
      const b = el("button", null, text);
      b.title = title;
      b.addEventListener("click", () => step(d, this.idx));
      return b;
    };
    const keys = idx ? "←/→" : "Shift+←/→";
    this.cap.append(btn("‹", -1, `Previous file in this pane (${keys})`), this.info,
                    btn("›", 1, `Next file in this pane (${keys})`));
    // Page bar for multi-page PDFs and TIFFs.
    this.pbar = el("div", "pbar");
    this.pPrev = el("button", null, "‹");
    this.pNext = el("button", null, "›");
    this.pLabel = el("span");
    this.pPrev.title = "Previous page in this pane";
    this.pNext.title = "Next page in this pane";
    this.pPrev.addEventListener("click", () => pageStep(-1, this.idx));
    this.pNext.addEventListener("click", () => pageStep(1, this.idx));
    this.pbar.append(this.pPrev, this.pLabel, this.pNext);
    this.canvas = el("canvas", "img");  // PDFs and TIFFs are drawn here instead of in `img`
    this.layer.append(this.thumb, this.img, this.canvas);
    this.stage.append(this.layer, this.load);
    this.el.append(this.stage, this.cap, this.pbar);
    // w/h: size of what is shown (for a PDF: the current page) in CSS px at 100 %.
    Object.assign(this, { f: null, w: 1, h: 1, pageNo: 1, pages: 0,
                          s: 1, x: 0, y: 0, committed: 1, fitted: true, token: 0, timer: 0, drag: null });
    this.bind();
  }

  setFile(f, pageNo = 1) {
    this.closePdf();
    this.f = f;
    this.w = f.w;
    this.h = f.h;
    this.pageNo = pageNo;
    const token = ++this.token;
    const t0 = performance.now();
    this.timeEl = el("span");
    this.info.replaceChildren(el("b", null, f.name), el("span", null, fmtDims(f) + " · " + fmtSize(f.size)),
                              this.timeEl);
    $("v-time").textContent = "";

    // Show the (blurry) thumbnail instantly, then swap in the real render.
    const cardImg = cards.get(f)?.querySelector("img");
    this.thumb.src = cardImg?.getAttribute("src") || (f.thumb ? thumbUrl(f) : "");
    this.thumb.hidden = !this.thumb.getAttribute("src");
    this.img.classList.remove("ready");
    this.img.removeAttribute("src");
    this.load.replaceChildren(el("span", "spinner"), " rendering…");
    this.load.hidden = false;

    this.canvas.classList.remove("ready");
    const paged = isPaged(f);
    this.img.hidden = paged;
    this.canvas.hidden = !paged;

    const done = () => {
      this.thumb.hidden = true;
      this.load.hidden = true;
      const ms = Math.round(performance.now() - t0) + " ms";
      this.timeEl.textContent = ms;
      if (this === panes[0]) $("v-time").textContent = ms;
      if (!active()[1]) prefetch(state.current + 1);
    };
    const failed = (e) => {
      if (token !== this.token) return;
      this.load.textContent = "⚠ " + (e?.name === "TooLarge" ? e.message : "cannot render this file");
      console.warn("render failed", f.name, e);
    };
    if (paged) return this.openPdf(token, done).catch(failed);

    this.img.src = fileUrl(state.dir, f);
    this.img.decode().then(() => {
      if (token !== this.token) return;
      if (f.kind === "image") this.fixSize(this.img.naturalWidth, this.img.naturalHeight, 1);
      // decode() resolves once parsed; wait two frames so the paint is included.
      requestAnimationFrame(() => requestAnimationFrame(() => {
        if (token !== this.token) return;
        this.img.classList.add("ready");
        done();
      }));
    }).catch(failed);
  }

  // The listing may only have guessed the size (or page count); correct it once known.
  fixSize(w, h, pages) {
    const f = this.f;
    if (Math.abs(w - f.w) <= 0.5 && Math.abs(h - f.h) <= 0.5 && pages === f.pages) return;
    updateMeta(f, { w, h, pages });
    this.info.querySelector("span").textContent = fmtDims(f) + " · " + fmtSize(f.size);
    if (this === panes[0] && state.cmp < 0) $("v-info").textContent = fmtDims(f) + " · " + fmtSize(f.size);
    if (isPaged(f)) return;  // showPage() sizes the layer
    this.w = w;
    this.h = h;
    if (this.fitted) fitGroup(this);
    else this.commit();
  }

  // ---- PDF and TIFF: the current page is drawn into a canvas sized for the committed zoom.

  async openPdf(token, done) {
    const f = this.f;
    const docs = docsFor(f);
    const doc = await docs.open(fileUrl(state.dir, f));
    if (token !== this.token) return doc.loadingTask.destroy();
    this.doc = doc;
    this.docs = docs;
    this.pages = doc.numPages;
    const first = await doc.getPage(1);
    if (token !== this.token) return;
    const { w, h } = docs.size(first);
    this.fixSize(w, h, doc.numPages);
    await this.showPage(this.pageNo);
    if (token === this.token) done();
  }

  // Go to page `n` of the open document (or remember it until it is open).
  setPage(n) {
    if (!this.doc) this.pageNo = n;
    else if (n !== this.pageNo) this.showPage(n).catch((e) => console.warn("page failed", e));
  }

  async showPage(n) {
    const token = this.token;
    n = Math.max(1, Math.min(this.pages, n));
    this.pageNo = n;
    this.updatePageBar();
    const page = await this.doc.getPage(n);
    if (token !== this.token || this.pageNo !== n) return;
    this.task?.cancel();
    this.page = page;
    ({ w: this.w, h: this.h } = this.docs.size(page));  // pages can differ in size
    this.canvas.classList.remove("ready");  // don't show the previous page stretched
    if (this.thumb.hidden) this.load.hidden = false;
    if (this.fitted) fitGroup(this);  // calls commit(), which draws
    else this.commit();
    await this.drawing;
    if (token === this.token && this.pageNo === n && this.thumb.hidden) this.load.hidden = true;
  }

  updatePageBar() {
    const paged = this.pages > 1;
    this.el.classList.toggle("paged", paged);
    this.pLabel.textContent = this.pageNo + " / " + this.pages;
    this.pPrev.disabled = this.pageNo <= 1;
    this.pNext.disabled = this.pageNo >= this.pages;
  }

  // Redraw at the committed zoom. The old bitmap stays (stretched) until the new one is ready.
  drawPdf() {
    if (!this.page) return;
    this.task?.cancel();
    const { canvas, task } = this.docs.draw(this.page, this.committed * devicePixelRatio);
    this.task = task;
    const token = this.token;
    this.drawing = task.promise.then(() => {
      if (token !== this.token || this.task !== task) return;
      canvas.className = "img ready";
      this.canvas.replaceWith(canvas);
      this.canvas = canvas;
    }, (e) => {
      if (e?.name !== "RenderingCancelledException") throw e;
    });
    return this.drawing;
  }

  closePdf() {
    this.task?.cancel();
    this.doc?.loadingTask.destroy();
    this.task = this.doc = this.docs = this.page = this.drawing = null;
    this.pages = 0;
    this.updatePageBar();
    const blank = el("canvas", "img");
    this.canvas.replaceWith(blank);  // drop the old bitmap's memory
    this.canvas = blank;
  }

  clear() {
    this.token++;
    this.closePdf();
    this.f = null;
    this.img.removeAttribute("src");
    this.thumb.removeAttribute("src");
    this.info.replaceChildren();
  }

  apply() {
    this.layer.style.transform = `translate(${this.x}px, ${this.y}px) scale(${this.s / this.committed})`;
    showZoom();
  }

  commit() {
    this.committed = this.s;
    this.layer.style.width = this.w * this.s + "px";
    this.layer.style.height = this.h * this.s + "px";
    this.layer.classList.remove("moving");
    this.apply();
    if (this.page) this.drawPdf();
  }

  interact() {
    this.layer.classList.add("moving");
    clearTimeout(this.timer);
    this.timer = setTimeout(() => this.commit(), 250);
  }

  fitScale() {
    const pad = 24;
    return Math.max(MIN_ZOOM, Math.min((this.stage.clientWidth - pad * 2) / this.w,
                                       (this.stage.clientHeight - pad * 2) / this.h));
  }

  fit(s) {
    if (!this.f) return;
    this.s = s ?? this.fitScale();
    this.x = (this.stage.clientWidth - this.w * this.s) / 2;
    this.y = (this.stage.clientHeight - this.h * this.s) / 2;
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
    // Firefox would start its own "drag this image" on a mouse drag and cancel our pointer
    // events; `draggable = false` isn't enough everywhere, so refuse every drag in the stage.
    st.addEventListener("dragstart", (e) => e.preventDefault());
    st.addEventListener("pointerdown", (e) => {
      if (e.button !== 0) return;
      e.preventDefault();  // no drag or text selection starting from here
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
    const p = new Pane(panes.length);
    panes.push(p);
    panesEl.append(p.el);
  }
  panes.forEach((p, i) => {
    const hide = i >= n;
    if (hide && !p.el.hidden) p.clear();
    p.el.hidden = hide;
  });
}

// Put file `f` (at page `page`) into pane `p`, fitting it (and the neighbours if they are still fitted).
function place(p, f, page = 1) {
  if (p.f === f) return p.setPage(page);
  p.setFile(f, page);
  const others = active().filter((q) => q !== p && q.f);
  if (others.every((q) => q.fitted)) fitGroup(null);
  else if (cmp.link) p.fit(others[0].s);
  else p.fit();
}

function openViewer(i, j = -1, page = 1, vspage = 1) {
  const compare = j >= 0;
  viewer.hidden = false;
  viewer.classList.toggle("compare", compare);
  panesEl.classList.toggle("stack", cmp.layout === "stack");
  thumbs.pause(true);
  state.current = i;
  state.cmp = j;
  ensurePanes(compare ? 2 : 1);
  place(panes[0], state.view[i], page);
  if (compare) place(panes[1], state.view[j], vspage);

  const n = state.view.length, fa = state.view[i], fb = state.view[j];
  $("v-name").textContent = compare ? fa.name + "  ⇄  " + fb.name : fa.name;
  const copyTitle = compare ? "Copy both files' full paths (one per line)" : "Copy the file's full path";
  if ($("v-copy")._title === undefined) $("v-copy").title = copyTitle;
  else $("v-copy")._title = copyTitle;  // showing "not copied" right now: the new title comes after
  $("v-info").textContent = compare ? "" : fmtDims(fa) + " · " + fmtSize(fa.size);
  $("v-pos").textContent = compare ? `A ${i + 1} · B ${j + 1} / ${n}` : i + 1 + " / " + n;
  $("v-raw").href = fileUrl(state.dir, fa);
  $("v-cmp").classList.toggle("on", compare);
  $("v-link").classList.toggle("on", cmp.link);
  showZoom();
  markSideFiles();
}

function prefetch(i) {
  const f = state.view[i];
  if (!f || isPaged(f) || f.size > 64 * 1024 * 1024) return;
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
  markSideFiles(false);
  const card = f && cards.get(f);
  if (card) { card.focus({ preventScroll: true }); card.scrollIntoView({ block: "nearest" }); }
}

function leaveViewer() {
  if (state.openedByPush) { state.openedByPush = false; history.back(); }
  else writeHash(state.dir, null, false);
}

// Show the viewer state in the URL. `b` is the comparison file index or -1; pa/pb are pages.
function go(a, b, pa = 1, pb = 1) {
  writeHash(state.dir, state.view[a].name, false, b >= 0 ? state.view[b].name : null, pa, pb);
}

// Turn pages in pane `which` (0 or 1) or in every multi-page PDF shown ("both").
// Returns false if nothing could turn (first/last page, or no multi-page PDF).
function pageStep(d, which = "both") {
  const ps = active();
  const pages = ps.map((p, i) => (which === "both" || which === i) && p.pages > 1
    ? Math.max(1, Math.min(p.pages, p.pageNo + d)) : p.pageNo);
  if (pages.every((n, i) => n === ps[i].pageNo)) return false;
  go(state.current, state.cmp, pages[0], pages[1] ?? 1);
  return true;
}

// Page Up/Down: pages if a multi-page PDF is shown, otherwise files.
function pageKey(d) {
  if (active().some((p) => p.pages > 1)) pageStep(d);
  else step(d, 1);
}

// Step which file is shown. In compare mode `which` picks pane 0 (A), 1 (B) or "both";
// both may show the same file, e.g. to compare two parts of one figure.
function step(d, which = 0) {
  const n = state.view.length;
  if (!n) return;
  if (state.cmp < 0) return go((state.current + d + n) % n, -1);
  if (which === "both") return go((state.current + d + n) % n, (state.cmp + d + n) % n);
  const a = state.current, b = state.cmp;
  if (which) go(a, (b + d + n) % n);
  else go((a + d + n) % n, b);
}

function toggleCompare() {
  const n = state.view.length;
  if (state.current < 0) return;
  if (state.cmp >= 0) return go(state.current, -1);
  go(state.current, (state.current + 1) % n);  // the only file in a folder: compared with itself
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
$("v-prev").addEventListener("click", () => step(-1, "both"));
$("v-next").addEventListener("click", () => step(1, "both"));
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
// Sidebar width: drag its right edge; double-click the edge for the default width.
function setSideWidth(px) {
  if (px) document.documentElement.style.setProperty("--side-w", px + "px");
  else document.documentElement.style.removeProperty("--side-w");
  prefs.set("sidew", px || "");
}
(() => {
  const grip = $("side-grip");
  let start = null;
  grip.addEventListener("pointerdown", (e) => {
    if (e.button !== 0) return;
    e.preventDefault();
    grip.setPointerCapture(e.pointerId);
    start = $("side").getBoundingClientRect().left;
    document.body.classList.add("resizing");
  });
  grip.addEventListener("pointermove", (e) => {
    if (start === null) return;
    setSideWidth(Math.round(Math.max(140, Math.min(innerWidth * 0.6, e.clientX - start))));
  });
  const end = () => { start = null; document.body.classList.remove("resizing"); };
  grip.addEventListener("pointerup", end);
  grip.addEventListener("pointercancel", end);
  grip.addEventListener("dblclick", () => setSideWidth(null));
})();

function toggleSide() {
  document.body.classList.toggle("no-side");
  prefs.set("side", document.body.classList.contains("no-side") ? "0" : "1");
}
$("toggle-side").addEventListener("click", toggleSide);
function refresh() {
  listCache.clear();
  dirCache.clear();
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
      PageUp: () => pageKey(-1), PageDown: () => pageKey(1), " ": () => step(1, 1),
      ArrowUp: () => step(-1, "both"), ArrowDown: () => step(1, "both"),
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
  if (+prefs.get("sidew", "")) setSideWidth(+prefs.get("sidew", ""));
  route();
})();
