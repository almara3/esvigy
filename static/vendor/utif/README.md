# UTIF.js + pako (vendored)

TIFF decoding for esvigy (Firefox can't display TIFF itself). Used only inside
`static/tiffworker.js`, so decoding never blocks the UI.

- `UTIF.js` from the npm package `utif@3.1.0` (MIT, see LICENSE), copied unmodified.
- `pako_inflate.min.js` from `pako@1.0.11` (MIT and Zlib, see LICENSE.pako), copied
  unmodified. UTIF needs it for Deflate-compressed TIFFs.

To update: download the new tarballs from the npm registry, check them against the
registry's `dist.integrity` hashes, and copy the same files.
