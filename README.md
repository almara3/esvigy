# esvigy

A fast SVG viewer for folders of (large) SVG files.

Classic image viewers (xviewer, eog, …) render SVG with librsvg, which gets very
slow on big files. Firefox's renderer is much faster, so esvigy uses Firefox as
its engine: a tiny local Python server (stdlib only) serves the folder, and the
UI runs in a Firefox window.

## Usage

    ./esvigy.py [DIR | FILE.svg]      # opens its own Firefox window
    ./esvigy.py ~/drawings -b firefox # normal Firefox window instead of the app window
    ./esvigy.py ~/drawings -b none    # just print the URL
    ./esvigy.py -h

Tip: `ln -s "$PWD/esvigy.py" ~/.local/bin/esvigy`

## Features

- Folder tree in the sidebar, thumbnail grid in the main panel
- Thumbnails are rendered once by the browser in a hidden frame (its own process, so the UI never freezes), two at a time, visible ones first,
  and cached in `~/.cache/esvigy/thumbs` (keyed on path + mtime + size), so a
  folder you've seen before opens instantly
- Viewer shows the cached thumbnail immediately, then swaps in the full render
- Pan (drag), zoom at cursor (wheel), double-click toggles fit / 100 %
- **Compare**: Ctrl-click two thumbnails and press *Compare* (or `c`), or press `c` in the viewer.
  Side by side or stacked (`l`), with pan/zoom linked between both (`y`). Arrows step B,
  Shift+arrows step A, `c` goes back to a single file. The URL (`#file=a.svg&vs=b.svg`) is shareable.
- Table view (`t`): full file names, dimensions, size and date; click a column header to sort, click again to reverse
- File names get two lines in the grid (full name on hover)
- Natural sort, filter, sort by size/date, `.svgz` support
- **Open…** (or `o`) picks another folder with the native dialog (needs zenity or kdialog)
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
- Thumbnails (`~/.cache/esvigy/thumbs`) and the Firefox profile are created `0700`. Thumbnails
  are kept forever; delete the folder to clear them.

## Own window

By default esvigy opens Firefox in a dedicated profile (`~/.local/share/esvigy/firefox-profile`)
with the tab bar, address bar and bookmarks hidden, window class `esvigy`, and quits
when you close the window. Your normal Firefox profile is untouched. Only one esvigy
window at a time can use that profile.

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
  Also checks in a real browser that a malicious SVG can't run scripts and that the CSP doesn't break the UI.
  Skipped if Firefox or selenium is missing; geckodriver is downloaded on first run.

Tests never touch your real thumbnail cache; it can also be redirected with `ESVIGY_CACHE=/some/dir`.
