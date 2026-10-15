# pdf.js (vendored)

Mozilla pdf.js, from the npm package `pdfjs-dist@6.3.289` (Apache-2.0, see LICENSE;
the decoders in `wasm/` carry their own licences).

Copied unmodified: `build/pdf.min.mjs`, `build/pdf.worker.min.mjs`, `cmaps/`,
`standard_fonts/`, `iccs/` and the image/colour decoders from `wasm/`.
Left out on purpose: `quickjs-eval.*` (runs JavaScript embedded in PDFs; esvigy never does),
the viewer UI, source maps, types and the legacy build.

To update: download the new tarball from the npm registry, check it against the
registry's `dist.integrity` hash, and copy the same files.
