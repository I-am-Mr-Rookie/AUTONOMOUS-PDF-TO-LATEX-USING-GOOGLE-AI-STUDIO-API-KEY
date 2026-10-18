# Frontend notes

## What existed before

No frontend. The project was a command-line Python pipeline only (`main.py` plus the phase
modules). There were no web pages, templates, static assets or JS tooling, and no web framework
among the dependencies.

## What was added

Everything lives in `frontend/`. No existing file changed except one short section in `README.md`.
No new dependencies: the server uses only the Python standard library, and the page is a single
HTML file with no build step and no external requests (no CDN, no web fonts).

| File | Purpose |
|------|---------|
| `frontend/server.py` | Local HTTP server (`http.server`). Serves the page, saves uploaded PDFs to `input/`, runs `main.py --input <file> [--force]` as a subprocess (one run at a time), relays its console output, and serves the compiled `.pdf` / `.tex` from `output/`. |
| `frontend/index.html` | The UI: upload or drag-and-drop a PDF, pick from the PDFs in `input/`, enter the API key (optional), choose “ignore cache”, run, watch the five phases and the live log, then open the compiled PDF or `.tex`. |
| `frontend/test_server.py` | Offline self-check (stdlib `unittest`). It runs the server against a temp folder with a stub `main.py`, so it never calls the API or the real pipeline. |

Design: a warm "technical paper" look. Serif headings, system sans for the UI, and a monospace
font for the log (Source Code Pro if it's installed). Hairline borders, one green accent, and an
automatic dark mode. The layout is two columns on desktop and one column below 820px.
Accessibility: every control has a real label, the drop zone is a keyboard-reachable file input,
status updates are announced through `aria-live`, focus outlines are visible, and
`prefers-reduced-motion` is respected.

The pipeline itself is unchanged. The UI only runs `main.py` exactly as the CLI does, so caching,
resume, preflight and the model fallback chain all behave the same.

### Safety

- The server binds to **127.0.0.1 only**.
- POST requests are refused unless the `Host` and `Origin` headers are `127.0.0.1:<port>` or
  `localhost:<port>`. This stops other websites (CSRF) and DNS-rebinding attacks from spending
  your API budget.
- The API key is sent once, in the run request, and placed only in the child process's
  environment. It is never written to disk or logged. Leave the field blank to use
  `GEMINI_API_KEY` / `.env` as usual.
- Uploads are reduced to a bare `*.pdf` filename, must start with `%PDF`, and are capped at
  100 MB. Downloads are limited to `.pdf` / `.tex` files directly inside `output/`.
- An upload with the same name as an existing PDF in `input/` overwrites it.

## How to run

```bash
pip install -r requirements.txt        # same prerequisites as the CLI (pdflatex, Poppler, key)
python frontend/server.py              # then open http://127.0.0.1:8000
python frontend/server.py --port 9000  # another port
```

Self-check (offline):

```bash
python -m unittest frontend/test_server.py
```

## Verification done

- `python -m unittest frontend/test_server.py` passes. It covers upload validation, path
  traversal, file listing, the output download, the cross-origin refusal, and a run with the key
  and `--force` passed through.
- The page's JavaScript passes `node --check`. The real server answers `/`, `/api/files` and
  `/api/job`.
- **Not done:** I couldn't check the page visually in a browser, because the headless Chromium in
  this environment is missing system libraries. A full pipeline run through the UI wasn't done
  either: `pdflatex` and the Python dependencies aren't installed here.
