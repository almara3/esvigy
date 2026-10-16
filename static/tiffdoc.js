"use strict";
// TIFFs, which Firefox can't display, decoded by the bundled UTIF.js in a worker (one per
// document). Same interface as pdfDocs (open/size/draw), so multi-page TIFFs get the PDF
// viewer's page bar and the thumbnail renderer treats both alike.

const tiffDocs = (() => {
  const WORKER = new URL("tiffworker.js", document.currentScript.src).href;
  // Canvas limits, as in pdfDocs.
  const MAX_SIDE = 8192, MAX_AREA = 48e6;
  // Largest page the worker decodes (it needs 4 bytes a pixel, ~1.2 GB here; see tiffcheck.js).
  // A0 at 600 dpi is ~560 MP and is refused with a message. Tests lower it.
  const api = { maxPixels: 300e6 };

  async function open(url) {
    const worker = new Worker(WORKER);
    const waiting = new Map();
    let nextId = 1;
    const call = (msg) => new Promise((resolve, reject) => {
      const id = nextId++;
      waiting.set(id, { resolve, reject });
      worker.postMessage({ id, ...msg });
    });
    const failAll = (err) => {
      for (const w of waiting.values()) w.reject(err);
      waiting.clear();
    };
    worker.onmessage = ({ data }) => {
      const w = waiting.get(data.id);
      waiting.delete(data.id);
      if (data.error) {
        const err = new Error(data.error);
        if (data.tooLarge) err.name = "TooLarge";  // shown to the user as it is
        w?.reject(err);
      }
      else w?.resolve(data);
    };
    worker.onerror = (e) => failAll(new Error(e.message || "TIFF worker failed"));

    // Only the page shown last is kept decoded; a long scan would otherwise fill the memory.
    let current = null;  // {n, page: Promise}
    const destroy = () => {
      worker.terminate();
      failAll(new Error("closed"));
      current?.page.then((p) => p.bitmap.close(), () => {});
      current = null;
    };

    let sizes;
    try {
      ({ sizes } = await call({ open: new URL(url, location.href).href, maxPixels: api.maxPixels }));
    } catch (e) {
      destroy();
      throw e;
    }
    return {
      numPages: sizes.length,
      loadingTask: { destroy },  // as in pdf.js
      getPage(n) {
        if (current?.n !== n) {
          current?.page.then((p) => p.bitmap.close(), () => {});
          const page = call({ page: n }).then(async ({ w, h, rgba }) => {
            const bitmap = await createImageBitmap(new ImageData(new Uint8ClampedArray(rgba), w, h));
            return { w, h, bitmap };
          });
          current = { n, page };
        }
        return current.page;
      },
    };
  }

  // Page size in CSS px: one image pixel each, as browsers show images at 100 %.
  const size = (page) => ({ w: page.w, h: page.h });

  // A canvas with `page` scaled to `scale`, never above its own pixels (CSS enlarges it).
  // Returns {canvas, task} like pdfDocs.draw; drawing is immediate here.
  function draw(page, scale) {
    const { w, h } = page;
    const k = Math.min(scale, 1, MAX_SIDE / w, MAX_SIDE / h, Math.sqrt(MAX_AREA / (w * h)));
    const canvas = document.createElement("canvas");
    canvas.width = Math.max(1, Math.round(w * k));
    canvas.height = Math.max(1, Math.round(h * k));
    if (!page.bitmap.width) {  // closed: another page was opened meanwhile
      const e = new Error("page closed");
      e.name = "RenderingCancelledException";  // as pdf.js names it, so callers ignore it
      return { canvas, task: { promise: Promise.reject(e), cancel() {} } };
    }
    const ctx = canvas.getContext("2d");
    ctx.imageSmoothingQuality = "high";
    ctx.drawImage(page.bitmap, 0, 0, canvas.width, canvas.height);
    return { canvas, task: { promise: Promise.resolve(), cancel() {} } };
  }

  return Object.assign(api, { open, size, draw });
})();
