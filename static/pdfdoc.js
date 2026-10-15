"use strict";
// Shared helper around the bundled pdf.js, used by the UI and by the thumbnail renderer.
// pdf.js is only loaded the first time a PDF is opened.

const pdfDocs = (() => {
  const VENDOR = new URL("vendor/pdfjs/", document.currentScript.src).href;
  // PDF points -> CSS px, so 100 % means real size, as in other PDF viewers.
  const CSS_PER_PT = 96 / 72;
  // Canvas limits: bigger bitmaps cost too much memory (and Firefox refuses huge ones).
  const MAX_SIDE = 8192, MAX_AREA = 48e6;
  let lib = null;

  function load() {
    lib ??= import(VENDOR + "pdf.min.mjs").then((pdfjs) => {
      pdfjs.GlobalWorkerOptions.workerSrc = VENDOR + "pdf.worker.min.mjs";
      // One parser worker for all documents of this page instead of one each.
      return { pdfjs, worker: new pdfjs.PDFWorker() };
    });
    return lib;
  }

  async function open(url) {
    const { pdfjs, worker } = await load();
    return pdfjs.getDocument({
      url,
      worker,
      cMapUrl: VENDOR + "cmaps/",
      cMapPacked: true,
      standardFontDataUrl: VENDOR + "standard_fonts/",
      wasmUrl: VENDOR + "wasm/",
      iccUrl: VENDOR + "iccs/",
      enableXfa: false,
    }).promise;
  }

  // Page size in CSS px.
  function size(page) {
    const v = page.getViewport({ scale: CSS_PER_PT });
    return { w: v.width, h: v.height };
  }

  // Start drawing `page` scaled to `scale` CSS px per CSS px of page size, on a new canvas,
  // shrunk if needed to stay within the canvas limits. Returns {canvas, task}; await task.promise.
  // `background`: the "display" intent draws in slices, one per animation frame, so the page
  // stays responsive. Hidden frames get no animation frames, so the thumbnail renderer (in
  // its own process anyway) uses the "print" intent, which draws without waiting.
  function draw(page, scale, { background = false } = {}) {
    const { w, h } = size(page);
    let k = scale;
    k = Math.min(k, MAX_SIDE / w, MAX_SIDE / h, Math.sqrt(MAX_AREA / (w * h)));
    const viewport = page.getViewport({ scale: CSS_PER_PT * k });
    const canvas = document.createElement("canvas");
    canvas.width = Math.max(1, Math.floor(viewport.width));
    canvas.height = Math.max(1, Math.floor(viewport.height));
    const task = page.render({ canvas, canvasContext: canvas.getContext("2d"), viewport,
                               intent: background ? "print" : "display" });
    return { canvas, task };
  }

  return { open, size, draw };
})();
