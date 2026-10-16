# esvigy

A fast SVG viewer for folders of (large) SVG files.

Classic image viewers (xviewer, eog, …) render SVG with librsvg, which gets very
slow on big files. Firefox's renderer is much faster, so esvigy uses Firefox as
its engine: a tiny local Python server (stdlib only) serves the folder, and the
UI runs in a Firefox window.

![The thumbnail grid](docs/grid.png)

![Compare mode: two files side by side with linked pan and zoom](docs/compare.png)

## Usage

    ./esvigy.py [DIR | FILE.svg]      # opens its own Firefox window
    ./esvigy.py ~/drawings -b firefox # normal Firefox window instead of the app window
    ./esvigy.py ~/drawings -b none    # just print the URL
    ./esvigy.py -h

Tip: `ln -s "$PWD/esvigy.py" ~/.local/bin/esvigy`

**Firefox is recommended.** esvigy is built and tested only with Firefox: its fast SVG
renderer is the reason esvigy exists, and only the app window (Firefox's own profile)
gets esvigy's security settings, such as no scripts in PDFs opened via "raw ↗".
Other browsers (`-b default`) should work, but are slower on big SVGs and untested.

### macOS and Windows

esvigy is developed on Linux; macOS and Windows are supported but less tested. Python 3.9+
is all you need. Run it as `python3 esvigy.py` (macOS) or `py esvigy.py` (Windows). Firefox
is found in its usual install location (`/Applications`, `~/Applications`, `Program Files`)
even when it isn't on `PATH`. On macOS, closing the window doesn't quit Firefox, so quit it
with Cmd+Q (or press Ctrl+C in the terminal) to stop esvigy.

## Features

- Folder tree in the sidebar, thumbnail grid in the main panel
- Thumbnails are rendered once by the browser in a hidden frame (its own process, so the UI never freezes), two at a time, visible ones first,
  and cached in `~/.cache/esvigy/thumbs` (keyed on path + mtime + size), so a
  folder you've seen before opens instantly
- Viewer shows the cached thumbnail immediately, then swaps in the full render
- Pan (drag), zoom at cursor (wheel), double-click toggles fit / 100 %
- **Compare**: Ctrl-click two thumbnails and press *Compare* (or `c`), or press `c` in the viewer.
  Side by side or stacked (`l`), with pan/zoom linked between both (`y`). Each pane has its own ‹ › in its label; ←/→ step B,
  Shift+←/→ step A, and ↑/↓ or the top-bar ‹ › step both together. `c` goes back to a single file. The URL (`#file=a.svg&vs=b.svg`) is shareable.
- Table view (`t`): full file names, dimensions, size and date; click a column header to sort, click again to reverse
- File names get two lines in the grid (full name on hover)
- **PDF** (page 1 for now): drawn by the bundled pdf.js (`static/vendor/pdfjs`, Mozilla, Apache-2.0),
  with thumbnails, zoom and compare like SVGs. Page size and page count show up once a PDF has
  been rendered (they are cached next to the thumbnail). PDF scripts never run in esvigy's viewer.
- Natural sort, filter, sort by size/date, `.svgz` support
- **Open…** (or `o`) picks another folder with the native dialog (zenity or kdialog on Linux; built in on macOS and Windows)
- Back button / Esc returns to the grid; URL hash keeps folder and file

## Keys

| Key | Grid | Viewer |
| --- | --- | --- |
| arrows | move between thumbnails | previous / next |
| Enter / Space | open | next (Space) |
| Esc / Backspace | | back to grid |
| f / 0, 1, + / - | | fit, 100 %, zoom |
| / | filter | |
| o / s / b / r | open folder / toggle sidebar / background / reload | same |

## Security

- Binds to 127.0.0.1 only and only serves files below the given directory
  (symlinks leading out of it are neither listed nor served).
- Every URL starts with a random secret (`http://127.0.0.1:PORT/<token>/`), new on each start.
  Other users on the machine and web pages that find the port get `403`. Treat the
  printed URL like a password; with `-b none`, don't paste it anywhere public.
- Requests that change something (Open…, thumbnail upload) also need an `X-Esvigy` header,
  which other websites can't send. Uploaded thumbnails must be WebP.
- SVGs are shown via `<img>`, so scripts inside them never run; opened directly ("raw ↗")
  they get a CSP sandbox that blocks scripts and external loads. The UI forbids inline
  scripts and being framed by other sites.
- PDFs opened via "raw ↗" are shown by the browser's own PDF viewer, which ignores the CSP. In the
  app window (default) Firefox's PDF scripting is switched off; with `-b firefox`/`default` your
  browser's own setting applies (in Firefox: `pdfjs.enableScripting` in `about:config`).
- Programs esvigy starts (Firefox, the folder dialog) are run by full path, found only in
  absolute `PATH` entries or their usual install locations, never in the current folder. On
  Windows only `.exe` files count, so a planted `firefox.bat` next to your files can't run.
- Thumbnails (`~/.cache/esvigy/thumbs`) and the Firefox profile are created `0700`. Thumbnails
  are kept forever; delete the folder to clear them.

## Own window

By default esvigy opens Firefox in a dedicated profile (`~/.local/share/esvigy/firefox-profile`)
with the tab bar, address bar and bookmarks hidden, window class `esvigy` (Linux), and quits
when you close the window. Your normal Firefox profile is untouched. Only one esvigy
window at a time can use that profile.

## Benchmark

How long does a big SVG take to get on screen at 1600×1200? librsvg is what xviewer, eog and most
file-manager thumbnailers use; it is called here the same way they do (GdkPixbuf). Firefox is what
esvigy uses (`<img>` + `decode()`, as in its viewer). Median of 3 runs.

| file | size | librsvg (eog, xviewer) | Firefox (esvigy) | speed-up |
| --- | ---: | ---: | ---: | ---: |
| filters-blur.svg (blur, mask, shadow) | 0.0 MB | 2.55 s | 0.24 s | 11× |
| paths-3000.svg (3000 polylines) | 2.7 MB | 0.92 s | 0.29 s | 3× |
| clipped-7000.svg (7000 clipped paths) | 5.3 MB | 2.37 s | 1.38 s | 2× |
| scatter-250k.svg (250 000 circles) | 18.7 MB | 3.44 s | 3.59 s | 1× |

The gain depends on what is in the file: big with filters, modest with many paths, none with
a plain pile of circles. These generated files are not the worst case, though: a real 6.9 MB matplotlib
figure (a shaded `pcolormesh`: about 3 600 gradients and 7 400 paths) took **35 s in librsvg and 0.56 s in
Firefox**. I could not reproduce that with a generated file (not shared; it is private work data), so
treat it as one data point rather than a typical result.

Where esvigy helps beyond the raw render time: the render happens in a separate Firefox process, so the
UI never freezes, and thumbnails are cached, so a folder you've seen before opens instantly.

Measured on an AMD Ryzen 5 5500U, Linux Mint 21.2, librsvg 2.52.5, Firefox 155 (headless).
Reproduce with:

    python3 benchmark/gen.py heavy /tmp/heavy
    .venv/bin/python benchmark/bench.py /tmp/heavy   # needs the system python3 with PyGObject

The screenshots above come from `.venv/bin/python docs/screenshots.py`.

## Tests

    python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
    .venv/bin/pytest                 # everything (~10 s)
    .venv/bin/pytest -m "not e2e"    # no browser (~1 s)

- `tests/test_svg.py`: SVG size parsing and header rewriting
- `tests/test_server.py`: the HTTP server against a temp folder: listing, file serving,
  path-traversal/symlink/Host-header protection, thumbnail cache, the Open dialog (with a fake `zenity`)
- `tests/test_security.py`: one check per attack from the section above: missing/wrong token,
  cross-site writes, DNS rebinding, cache poisoning, malformed requests, escaping the root,
  security headers, file permissions
- `tests/test_e2e.py`: the UI in headless Firefox via Selenium (grid, viewer, compare, table view).
  Also checks in a real browser that malicious SVGs and PDFs can't run scripts and that the CSP doesn't break the UI.
  The browser gets the same preferences as esvigy's app profile.
  Skipped if Firefox or selenium is missing; geckodriver is downloaded on first run.

Tests never touch your real thumbnail cache; it can also be redirected with `ESVIGY_CACHE=/some/dir`.
