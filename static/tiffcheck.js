"use strict";
// Limits for untrusted TIFFs, checked before UTIF.js gets to see them (used by tiffworker.js).
// UTIF believes whatever a file claims: it loops over the value count of every tag, follows the
// chain of image directories until it ends (even if it loops back), and sizes its buffers from
// width × height, the tile size and the dimensions inside embedded JPEGs; Deflate data is
// inflated without a limit. Any of these lets a file of a few bytes hang the decoder or ask
// for many GB of memory.

const tiffLimits = (() => {
  class TooLarge extends Error {
    constructor(message) {
      super(message);
      this.name = "TooLarge";
    }
  }

  // Bytes per value of the TIFF field types UTIF reads (it ignores the others).
  const TYPE_SIZE = { 1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 7: 1, 8: 2, 9: 4, 10: 8, 11: 4, 12: 8 };
  const MAX_DIRS = 4096;  // pages, sub-images and maker notes together; real files have far fewer
  const MAX_DEPTH = 8;
  const mp = (px) => (px / 1e6).toFixed(px < 1e7 ? 1 : 0) + " MP";
  const isOffset = (v) => Number.isInteger(v) && v >= 0;

  // Walk every directory UTIF.decode would read, as it reads them: the main chain, sub-images
  // (tag 330), EXIF (34665), DNG private data (50740) and maker notes (37500, where a Nikon note
  // is a whole TIFF of its own). Throws if a tag's values reach past the end of the file (UTIF
  // would loop over all of them) or the directories loop. Afterwards UTIF's work is bounded by
  // the file's size.
  function checkStructure(buf, state = { dirs: 0 }, nesting = 0) {
    const n = buf.byteLength, d = new DataView(buf);
    if (n < 8) throw new Error("not a TIFF");
    if (nesting > 2) throw new TooLarge("TIFF nested too deeply");
    const le = d.getUint16(0) === 0x4949;
    // UTIF reads anything outside the file as 0.
    const inside = (o, k) => isOffset(o) && o + k <= n;
    const u8 = (o) => (inside(o, 1) ? d.getUint8(o) : 0);
    const u16 = (o) => (inside(o, 2) ? d.getUint16(o, le) : 0);
    const u32 = (o) => (inside(o, 4) ? d.getUint32(o, le) : 0);
    const READ = {
      1: u8, 7: u8, 3: u16, 4: u32,
      8: (o) => (inside(o, 2) ? d.getInt16(o, le) : 0),
      9: (o) => (inside(o, 4) ? d.getInt32(o, le) : 0),
      5: (o) => u32(o) / u32(o + 4),
      10: (o) => (inside(o, 8) ? d.getInt32(o, le) / d.getInt32(o + 4, le) : NaN),
      11: (o) => (inside(o, 4) ? d.getFloat32(o, le) : 0),
      12: (o) => (inside(o, 8) ? d.getFloat64(o, le) : 0),
    };
    const seen = new Set();

    // A tag's values as UTIF._readIFD builds them (`bytes`: whether UTIF keeps them as raw bytes).
    function values(type, num, e) {
      const size = TYPE_SIZE[type] * num;
      // In the entry if they fit in 4 bytes; UTIF always follows the offset for rationals and floats.
      const at = size <= 4 && ![5, 10, 11, 12].includes(type) ? e + 8 : u32(e + 8);
      if (at + size > n) throw new Error("TIFF tag points outside the file");
      if (type === 2) {  // text, unless it starts with a byte >= 128
        return u8(at) < 128 ? { at, vals: [NaN], bytes: false }
                            : { at, vals: Array.from({ length: Math.max(0, num - 1) }, (_, j) => u8(at + j)), bytes: true };
      }
      const k = TYPE_SIZE[type], read = READ[type];
      return { at, vals: Array.from({ length: num }, (_, j) => read(at + k * j)), bytes: type === 1 || type === 7 };
    }

    function dir(off, depth) {
      if (!inside(off, 2)) return;  // UTIF reads zeros there: an empty directory
      if (seen.has(off)) throw new Error("TIFF directories loop");
      seen.add(off);
      if (++state.dirs > MAX_DIRS || depth > MAX_DEPTH) throw new TooLarge("too many TIFF directories");
      const cnt = u16(off);
      for (let i = 0, e = off + 2; i < cnt && e + 12 <= n; i++, e += 12) {  // entries past the end read as 0
        const tag = u16(e), type = u16(e + 2), num = u32(e + 4);
        if (!TYPE_SIZE[type]) continue;  // UTIF skips types it doesn't know
        const { at, vals, bytes } = values(type, num, e);
        if (tag === 330 || tag === 34665) {
          for (const v of vals) if (isOffset(v)) dir(v, depth + 1);
        } else if (tag === 50740) {
          // UTIF reads the first four values as one offset (bin.readUint on the array)
          const b = [0, 1, 2, 3].map((j) => (vals[j] ?? 0) & 255);
          const o = (le ? b[0] | b[1] << 8 | b[2] << 16 | b[3] << 24 : b[0] << 24 | b[1] << 16 | b[2] << 8 | b[3]) >>> 0;
          if (u16(o) < 300) dir(o, depth + 1);
        }
        if (tag === 37500) {
          const nikon = bytes && String.fromCharCode(...vals.slice(0, 5)) === "Nikon";
          if (nikon) {
            if (num > 10) checkStructure(buf.slice(at + 10, at + num), state, nesting + 1);
          } else if (u16(u32(e + 8)) < 300) {
            dir(u32(e + 8), depth + 1);
          }
        }
      }
      return off + 2 + 12 * cnt;
    }

    // The main chain, as UTIF.decode walks it: until a "next directory" offset of 0.
    let off = u32(4);
    do {
      const end = dir(off, 0);
      off = end === undefined ? 0 : u32(end);
    } while (off !== 0);
  }

  // Check one page (a directory from UTIF.decode) before UTIF.decodeImage allocates for it.
  // Returns how many bytes its decoded data may take (the budget for inflating it).
  function checkPage(ifd, maxPixels) {
    const w = ifd.t256[0], h = ifd.t257[0], maxBytes = maxPixels * 4;  // as much as the RGBA copy
    if (!(w * h <= maxPixels)) throw new TooLarge(`image too large: ${mp(w * h)} (limit ${mp(maxPixels)})`);
    // bits per pixel, worked out as UTIF.decodeImage does
    let bipp = ifd.t258 ? Math.min(32, ifd.t258[0]) * ifd.t258.length : (ifd.t277 ? ifd.t277[0] : 1);
    const cmpr = ifd.t259 ? ifd.t259[0] : 1;
    if (cmpr === 1 && ifd.t279 && ifd.t278 && ifd.t262 && ifd.t262[0] === 32803) {
      bipp = Math.round((ifd.t279[0] * 8) / (w * ifd.t278[0]));
    }
    const bytes = h * Math.ceil((w * bipp) / 8);
    if (!(bytes <= maxBytes)) throw new TooLarge("image data too large");
    if (ifd.t322) {
      const tw = ifd.t322[0], th = ifd.t323 ? ifd.t323[0] : 0;
      if (!(tw * th <= maxPixels && Math.ceil((tw * th * bipp) / 8) <= maxBytes)) {
        throw new TooLarge("TIFF tiles too large");
      }
    }
    return bytes;
  }

  // Check the frame size of a JPEG embedded in a TIFF before UTIF's JPEG decoders allocate for it.
  // Every start-of-frame marker anywhere in the data counts: the decoders skip unknown bytes to find
  // markers, so a strict walk could be fooled. (Real JPEG data never contains one by accident.)
  function checkJpeg(u8, maxPixels) {
    for (let i = 0; i + 9 < u8.length; i++) {
      if (u8[i] !== 0xFF) continue;
      const m = u8[i + 1];
      if (m < 0xC0 || m > 0xCF || m === 0xC4 || m === 0xC8 || m === 0xCC) continue;
      const h = (u8[i + 5] << 8) | u8[i + 6], w = (u8[i + 7] << 8) | u8[i + 8], comps = u8[i + 9];
      // The decoders pad to whole blocks and keep each component separately.
      if (w * h > maxPixels || (w + 128) * (h + 128) * Math.max(1, comps) > maxPixels * 4) {
        throw new TooLarge(`embedded JPEG too large: ${mp(w * h)}`);
      }
    }
  }

  return { TooLarge, checkStructure, checkPage, checkJpeg };
})();
