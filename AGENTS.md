# AGENTS.md

This file provides guidance to Codex (Codex.ai/code) when working with code in this repository.

## Project Status

**Implemented — first working build.** All four phases, the orchestrator, and three supporting
modules exist and pass offline verification: `python main.py --preflight-only` is green, and a
trivial document compiles through the real `pdflatex` path. The only code path not yet exercised is
the live Gemini `generate` calls — deliberately, to preserve the daily request budget (and there
is no input PDF yet). `Plan.txt` remains the authoritative architecture spec; read it before
changing any module.

The goal: take a scanned, non-OCR, multi-page PDF exam paper (`input/[Original_Name].pdf`) with
handwritten/printed math and diagrams, and produce a publication-quality typeset LaTeX PDF
(`output/[Original_Name]_LaTeX_compiled.pdf`) with high-resolution extracted figures embedded.

### Module layout

- **Phase modules:** `preflight.py`, `extractor.py`, `processor.py`, `compiler.py`.
- **Orchestrator:** `main.py` — CLI that sequences the phases with cache-resume.
- **Supporting modules (shared infrastructure, not phases):**
  - `config.py` — paths, per-model free-tier limits, the **`MODEL_CHAIN`** fallback spec, UTF-8
    console setup, and `.env` loading (uses `python-dotenv` when present, else a built-in fallback
    parser so a bare environment still boots before deps are installed).
  - `state.py` — atomic per-file cache state machine (`cache/[Original_Name]_state.json`).
  - `gemini.py` — the single chokepoint for ALL API access: client factory, the per-model RPM
    throttle, the **three-tier model fallback chain**, and a per-key **per-model** rolling-24h
    request ledger (`cache/api_usage.json`) that enforces each model's daily ceiling.
- **Secret handling:** the Gemini API key lives in `.env` (gitignored), never in source. The
  `Code from AI Studio(*.py)` files are vendor reference snippets — one per model
  (`(3.5 Flash)`, `(3 Flash)`, `(Gemma 4 31B)`) — and are gitignored.

The model ids are confirmed against the Google AI Studio references on `google-genai` 2.7.0. For
the two Flash models, a string `media_resolution`, `thinking_config(thinking_level=...)`, and a
`response_schema` built from `types.Schema` all validate. **Gemma (`gemma-4-31b-it`) is flagged
conservatively** as not supporting those config knobs (the Gemini API historically rejects
`system_instruction` / schemas / thinking for Gemma); `gemini.py` adapts each request to the active
model's declared capabilities, so being wrong in either direction degrades gracefully rather than
hard-failing.

## The Constraint That Drives Everything

The architecture survives Google AI Studio's free-tier ceilings by treating model access as a
**three-tier fallback chain** (`config.MODEL_CHAIN`), each tier metered **separately per API key**:

| Order | Model | RPD (daily) | RPM | Context in / out |
|-------|-------|-------------|-----|------------------|
| 1 | `gemini-3.5-flash` | 20 | 5 | 1,048,576 / 65,536 |
| 2 | `gemini-3-flash-preview` | 20 | 5 | 1,048,576 / 65,536 |
| 3 | `gemma-4-31b-it` | 1,500 | 15 (TPM unlimited) | 262,144 / 32,768 |

`gemini.generate()` tries tier 1 first; when the active model is unavailable (404/403), rejects the
request (400), or hits its limit (local ledger says 0 left, a server-side per-day 429, or a
per-minute rate error that persists past `MAX_MODEL_ATTEMPTS` ≈ 2–3 attempts), it falls back to the
next tier. **When every tier is exhausted it raises `BudgetExceeded`** telling the user to wait ~24h
for limits to reset or supply a new API key. Throttling is per-model (`ceil(60/RPM)` seconds: 12s for
the Flash models, 4s for Gemma) so each model stays under its own RPM.

Still spend calls frugally: a full run budgets ~1 extraction + 1 generation + up to 3
self-correction calls. Treat each request as scarce — the chain widens the daily envelope, it does
not make calls free. The call signatures (`gemini.generate`, `gemini.upload_pdf`) are unchanged, so
phase modules never need to know which model answered; `gemini.active_model()` reports it afterward.

## Core Architecture: Cache-First Local Slicing State Machine

The pipeline is a **resumable state machine** keyed on the source filename. Every milestone is
written to `cache/[Original_Name]_state.json`. Any phase must check the cache and **skip work that
is already recorded** rather than re-issuing API calls or recomputation. If a later step (e.g.
local LaTeX compilation) fails, a re-run resumes from cache state — it must not repeat completed
API transactions. This is the mechanism that makes the daily request budget workable across retries.

Two invariants hold across the whole codebase:

1. **Filename preservation** — `[Original_Name]` (the input PDF's base name) propagates verbatim
   through `output/`, `cache/`, and `cache/images/` filenames. Nothing is renamed mid-pipeline.
2. **API vs. offline separation** — phases are explicitly classed as API-consuming or offline.
   Offline phases (slicing) must consume **zero** API requests.

## The Four Phases

Each phase is one module; `main.py` is the orchestrator/CLI runner that sequences them. All API
access funnels through `gemini.py`, shared state through `state.py`, and constants through
`config.py`.

1. **`preflight.py` — environment verification (no API).** Detect host platform. Use
   `shutil.which()` to locate `pdflatex` (or `latexmk`) and Poppler's `pdftoppm`; if missing, print
   OS-specific install instructions. Silently `pip install` `google-genai`, `pdf2image`, `pillow`,
   `pypdf`, `python-dotenv`. Authenticate/validate the Gemini API key and store keys + binary paths
   in a secure config file.
2. **`extractor.py` — scan & coordinate extraction (1 API call).** If the cache already has
   `extraction_result`, **skip entirely**. Otherwise upload the PDF via the Gemini Files API
   (`client.files.upload()`) and, in a **single** request with a strict JSON output schema, map the
   layout of all pages at once: questions, raw text, and relative bounding boxes
   `[ymin, xmin, ymax, xmax]` for diagrams. Save the result to the state JSON.
3. **`processor.py` — offline lossless slicing (0 API).** Read cached bounding boxes. Render target
   pages to 300 DPI PIL images in-memory via `pdf2image`, crop diagrams losslessly, and save to
   `cache/images/` using the filename layout `[Original_Name]_page_N_q_QID_pos_K.png`.
4. **`compiler.py` — LaTeX generation, compilation & self-correction (API + local loop).** Pass
   structured text + cropped image paths to Gemini to generate the `.tex` (the model is told the
   exact absolute, forward-slashed image paths to use in `\includegraphics`). Compile locally, then
   parse the `! ` error blocks from the `.log`; on failure, send those errors **plus the current
   full source** back to Gemini for a complete corrected document, and recompile. **Maximum 3
   correction loops** — so a run costs at most 1 + 3 generation requests.

   _Deviation from `Plan.txt`:_ the plan suggested returning only the broken snippet. We send the
   whole document instead, because LaTeX failures are frequently non-local (a missing preamble
   package, an unbalanced brace spanning regions) and re-splicing a snippet at the right line is
   fragile. A full LaTeX source is plain text (tens of KB), comfortably under the 250K TPM ceiling,
   so the extra input tokens are cheap relative to the robustness gained.

Local compile command:
```
pdflatex -interaction=nonstopmode -output-directory=output output/[Original_Name]_LaTeX_compiled.tex
```

## Commands

```bash
# Install runtime dependencies (also bootstrapped automatically by preflight.py)
pip install -r requirements.txt

# Verify the environment only — no API calls, safe to run anytime
python main.py --preflight-only

# Run the full pipeline on the first PDF in input/  (drop your scanned exam there first)
python main.py

# Useful flags
python main.py --input Exam.pdf   # pick a specific file in input/ (or an absolute path)
python main.py --force            # ignore cache and re-run every phase
python main.py --skip-preflight   # skip the environment checks
```

Offline verification used during development (spends no API budget): import every module, run
`--preflight-only`, and compile a trivial `.tex` through `compiler._compile`. There is no formal
test suite or linter configured yet. If you add one, document the invocation here.

## Cross-Platform Risks to Mitigate

The target host is **Windows 11** (paths use backslashes, the project dir name contains a space:
`AUTONOMOUS PDF-TO-LATEX`), but the pipeline must also run on macOS/Linux:

- **External binaries are not bundled.** `pdflatex`/`latexmk` and Poppler (`pdftoppm`) must be
  discovered at runtime via `shutil.which()`; never hard-code paths. `pdf2image` needs Poppler on
  the system PATH — this is the most common Windows breakage.
- **Quote/normalize paths.** The working directory contains a space; build paths with `os.path` /
  `pathlib` and quote them in any shell-out, especially the `-output-directory` argument.
- **State-file concurrency.** The state JSON is the single source of truth for resumption; write it
  atomically (temp file + replace) so an interrupted run can't leave it half-written.
- **Console encoding.** Windows consoles default to cp1252 and raise `UnicodeEncodeError` on the
  status glyphs (→ ✓ ✗ ⚡). `config.py` reconfigures `stdout`/`stderr` to UTF-8 at import time —
  keep that, and prefer it over stripping the glyphs.
- **`.env` is loaded by explicit path** from the project root (not the CWD), so the pipeline finds
  the key regardless of where it's launched — important given the space in the directory name.
- **`pdf2image` gets an explicit `poppler_path`.** `processor.py` derives Poppler's bin directory
  from `shutil.which("pdftoppm")` and passes it in, so a PATH that works in the shell but isn't
  inherited by the child process still resolves on Windows.
