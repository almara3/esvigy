"use strict";
// Decodes one TIFF with UTIF.js, off the page's thread (a big scan takes a while).
// {id, open: url, maxPixels} -> {id, sizes: [{w, h}, ...]} (one per page); {id, page: n} -> {id, w, h, rgba}.
// Errors come back as {id, error, tooLarge}.
importScripts("vendor/utif/pako_inflate.min.js", "vendor/utif/UTIF.js", "tiffcheck.js");

let buf = null, pages = [];
let maxPixels = 300e6;  // set by the page; RGBA needs 4 bytes per pixel
let budget = 0;         // bytes the page being decoded may inflate to

// UTIF looks these up each time it uses them, so they can be guarded from here (see tiffcheck.js).
pako.inflate = (src) => {
  const inf = new pako.Inflate();
  const onData = inf.onData;
  let total = 0;
  inf.onData = function (chunk) {
    total += chunk.length;
    if (total > budget) throw new tiffLimits.TooLarge("compressed TIFF data expands beyond the image size");
    onData.call(this, chunk);
  };
  inf.push(src, true);
  if (inf.err) throw new Error(inf.msg || "bad Deflate data");
  return inf.result;
};
const parseJpeg = UTIF.JpegDecoder.prototype.parse;
UTIF.JpegDecoder.prototype.parse = function (data) {
  tiffLimits.checkJpeg(data, maxPixels);
  return parseJpeg.call(this, data);
};
const losslessJpeg = UTIF.LosslessJpegDecode;
UTIF.LosslessJpegDecode = (data) => {
  tiffLimits.checkJpeg(data, maxPixels);
  return losslessJpeg(data);
};

self.onmessage = async ({ data }) => {
  const { id } = data;
  try {
    if (data.open) {
      if (data.maxPixels) maxPixels = data.maxPixels;
      const r = await fetch(data.open);
      if (!r.ok) throw new Error(r.status + " " + r.statusText);
      buf = await r.arrayBuffer();
      tiffLimits.checkStructure(buf);
      // Pages are the full images; reduced-resolution ones (NewSubfileType bit 0) are previews.
      pages = UTIF.decode(buf).filter((ifd) => ifd.t256 && ifd.t257 && !(ifd.t254 && ifd.t254[0] & 1));
      if (!pages.length) throw new Error("no image in this TIFF");
      postMessage({ id, sizes: pages.map((ifd) => ({ w: ifd.t256[0], h: ifd.t257[0] })) });
    } else {
      const ifd = pages[data.page - 1];
      budget = tiffLimits.checkPage(ifd, maxPixels);
      UTIF.decodeImage(buf, ifd, pages);
      const rgba = UTIF.toRGBA8(ifd);
      delete ifd.data;  // keep only the RGBA copy, which is handed over below
      postMessage({ id, w: ifd.width, h: ifd.height, rgba: rgba.buffer }, [rgba.buffer]);
    }
  } catch (err) {
    const tooLarge = err instanceof tiffLimits.TooLarge;
    postMessage({ id, error: tooLarge ? err.message : String(err), tooLarge });
  }
};
