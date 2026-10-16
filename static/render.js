"use strict";
// Hidden helper page. It is loaded in an iframe from a different origin than the UI
// (localhost vs 127.0.0.1), so Firefox runs it in its own process: drawing a huge SVG
// here cannot freeze the UI. Requests come in via postMessage, results go to the thumb cache.
const PARENT = location.hostname === "localhost" ? location.origin.replace("localhost", "127.0.0.1")
                                                  : location.origin.replace("127.0.0.1", "localhost");

const put = (name, body) => fetch("../thumb/" + name, { method: "PUT", body, headers: { "X-Esvigy": "1" } });

// SVGs and the raster images Firefox decodes itself. SVGs are scaled up to fill the thumbnail,
// rasters only down. A raster's real size (after EXIF rotation) is stored for the listing.
async function drawImage({ url, kind, key, w, h, max }) {
  const src = new Image();
  try {
    src.src = url;
    await src.decode();
    let meta;
    if (kind === "image") {
      ({ naturalWidth: w, naturalHeight: h } = src);
      meta = { w, h, pages: 1 };
      await put(key + ".json", JSON.stringify(meta));
    }
    const s = Math.min(max / w, max / h, kind === "image" ? 1 : Infinity);
    const canvas = document.createElement("canvas");
    canvas.width = Math.max(1, Math.round(w * s));
    canvas.height = Math.max(1, Math.round(h * s));
    const ctx = canvas.getContext("2d");
    ctx.imageSmoothingQuality = "high";
    ctx.drawImage(src, 0, 0, canvas.width, canvas.height);
    return { canvas, meta };
  } finally {
    src.src = "";
  }
}

// First page of a PDF or TIFF, plus its size and page count (stored, so the listing knows them next time).
async function drawPaged({ url, kind, key, max }) {
  const docs = kind === "tiff" ? tiffDocs : pdfDocs;
  const doc = await docs.open(url);
  try {
    const page = await doc.getPage(1);
    const { w, h } = docs.size(page);
    const { canvas, task } = docs.draw(page, Math.min(max / w, max / h), { background: true });
    await task.promise;
    const meta = { w, h, pages: doc.numPages };
    await put(key + ".json", JSON.stringify(meta));
    return { canvas, meta };
  } finally {
    doc.loadingTask.destroy();
  }
}

async function render(msg) {
  const paged = msg.kind === "pdf" || msg.kind === "tiff";
  const { canvas, meta } = await (paged ? drawPaged(msg) : drawImage(msg));
  const blob = await new Promise((r) => canvas.toBlob(r, "image/webp", 0.85));
  if (!blob) throw new Error("encode failed");
  let cached = false;
  if (blob.type === "image/webp") cached = (await put(msg.key + ".webp", blob)).ok;
  return cached ? { cached, meta } : { cached, blob, meta };
}

window.addEventListener("message", async (e) => {
  if (e.origin !== PARENT || !e.data || e.data.type !== "render") return;
  const { id } = e.data;
  let reply;
  try { reply = { id, ...(await render(e.data)) }; }
  catch (err) { reply = { id, error: String(err) }; }
  e.source.postMessage(reply, e.origin);
});
parent.postMessage({ ready: true }, PARENT);
