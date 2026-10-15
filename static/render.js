"use strict";
// Hidden helper page. It is loaded in an iframe from a different origin than the UI
// (localhost vs 127.0.0.1), so Firefox runs it in its own process: drawing a huge SVG
// here cannot freeze the UI. Requests come in via postMessage, results go to the thumb cache.
const PARENT = location.hostname === "localhost" ? location.origin.replace("localhost", "127.0.0.1")
                                                  : location.origin.replace("127.0.0.1", "localhost");

async function render({ url, key, w, h, max }) {
  const src = new Image();
  try {
    src.src = url;
    await src.decode();
    const s = Math.min(max / w, max / h);
    const cw = Math.max(1, Math.round(w * s)), ch = Math.max(1, Math.round(h * s));
    const canvas = document.createElement("canvas");
    canvas.width = cw;
    canvas.height = ch;
    canvas.getContext("2d").drawImage(src, 0, 0, cw, ch);
    const blob = await new Promise((r) => canvas.toBlob(r, "image/webp", 0.85));
    if (!blob) throw new Error("encode failed");
    let cached = false;
    if (blob.type === "image/webp") {
      const r = await fetch("../thumb/" + key + ".webp", { method: "PUT", body: blob, headers: { "X-Esvigy": "1" } });
      cached = r.ok;
    }
    return cached ? { cached } : { cached, blob };
  } finally {
    src.src = "";
  }
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
