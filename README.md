# Autonomous PDF → LaTeX Pipeline

Turn a **scanned, non-OCR, multi-page PDF exam paper** — handwritten or printed math, circuits,
graphs and all — into a **publication-quality, typeset LaTeX PDF** with the original diagrams
extracted at high resolution and embedded back in place.

```
input/Exam.pdf  ──►  [ scan → slice → typeset → compile ]  ──►  output/Exam_LaTeX_compiled.pdf
```

The pipeline is **free-tier aware** and **resumable**: it rations a tight Google AI Studio request
budget across a three-model fallback chain, caches every milestone, and can self-correct broken
LaTeX by reading the compiler log. A crash, a failed compile, or a hit rate limit never re-spends an
API call you already paid for.

---

## How it works

The work is split into five phases. Only two of them ever touch the network.

| Phase | Module | API calls | What it does |
|------:|--------|:---------:|--------------|
| 1 | `preflight.py` | 0 | Detects the OS, installs missing Python deps, and locates `pdflatex`/`latexmk` and Poppler's `pdftoppm` (prints OS-specific install hints if absent). |
| 2 | `extractor.py` | 1 | Uploads the PDF once and, in a **single** structured-JSON request, maps the whole paper: header, sections, questions, marks, OR-alternatives, and `[ymin, xmin, ymax, xmax]` bounding boxes for every diagram. |
| 3 | `processor.py` | 0 | **Offline.** Renders the needed pages at 300 DPI and crops each diagram losslessly into `cache/images/`. |
| 4 | `compiler.py` | 1 + up to 3 | Generates the `.tex` from the cached structure + cropped images, compiles with `pdflatex`, and on failure feeds the parsed log errors **plus the full current source** back to the model for a corrected document — up to 3 correction loops. |
| 5 | `evaluator.py` | 0 | **Offline.** Compares the compiled output PDF against the original PDF via text extraction to ensure high semantic fidelity (preventing LLM hallucinations/dropped questions). |

A full run therefore costs **at most 1 + 1 + 3 = 5 generation requests** (plus one upload, which draws
on a separate quota).

### The model fallback chain

Each model on the Google AI Studio free tier is metered **separately, per API key**. Instead of
riding one model to its daily ceiling and stopping, the pipeline defines an ordered chain
(`config.MODEL_CHAIN`) and falls through it:

| Order | Model | Requests/day | Requests/min | Context (in / out) |
|:-----:|-------|:------------:|:------------:|:-------------------|
| 1 | `gemini-3.5-flash` | 20 | 5 | 1,048,576 / 65,536 |
| 2 | `gemini-3-flash-preview` | 20 | 5 | 1,048,576 / 65,536 |
| 3 | `gemma-4-31b-it` | 1,500 | 15 (TPM unlimited) | 262,144 / 32,768 |

`gemini.generate()` tries tier 1 first. It moves to the next tier when the active model is
**unavailable** (404/403), **rejects the request** (400), or **hits its limit** — the local ledger
says 0 left, the server returns a per-day `429`, or a per-minute rate error persists past ~2–3
attempts. When **every** tier is exhausted it stops and tells you to wait ~24 h for the limits to
reset or supply a fresh API key.

Requests are throttled per model (`ceil(60 / RPM)` → 12 s for the Flash models, 4 s for Gemma) so
each stays under its own rate limit. The two Flash models run with high thinking + high media
resolution and strict JSON schemas; Gemma is treated conservatively (no thinking / system
instruction / schema), so those settings are folded into the prompt instead — the pipeline adapts
automatically and never needs to know which model answered.

---

## Requirements

- **Python 3.10+**
- **A LaTeX engine** — `pdflatex` (or `latexmk`), e.g. from MiKTeX or TeX Live.
- **Poppler** — provides `pdftoppm`, which `pdf2image` needs to rasterize pages.
- **A Google AI Studio API key** — free tier is enough. Get one at <https://aistudio.google.com/apikey>.

Python packages (`google-genai`, `pdf2image`, `pillow`, `pypdf`) are listed in `requirements.txt`
and are also auto-installed by the preflight phase.

---

## Installation

```bash
# 1. Python dependencies (preflight will also do this for you)
pip install -r requirements.txt
```

**2. External binaries** — these are *not* bundled and must be on your `PATH`:

| Tool | Windows | macOS | Linux (Debian/Ubuntu) |
|------|---------|-------|------------------------|
| LaTeX | Install [MiKTeX](https://miktex.org) or TeX Live | `brew install --cask mactex-no-gui` | `sudo apt-get install texlive-latex-extra latexmk` |
| Poppler | `winget install oschwartz10612.Poppler` then add its `bin/` to PATH | `brew install poppler` | `sudo apt-get install poppler-utils` |

Verify everything is wired up correctly — this spends **zero** API budget:

```bash
python main.py --preflight-only
```

---

## API key & Configuration

The API key can be provided via a `.env` file, an environment variable, or interactive runtime prompt:

```bash
# Option A — Use a .env file (recommended)
cp .env.example .env
# Edit .env and set GEMINI_API_KEY=your_gemini_api_key_here

# Option B — Environment variable
export GEMINI_API_KEY="your-key-here"

# Option C — Interactive prompt
python main.py
```

The usage ledger (`cache/api_usage.json`) tracks requests by a one-way SHA-256 fingerprint of the
key, so swapping in a **new** key resets the daily budget. Nothing from which the key could be
recovered is stored.


---

## Usage

Drop your scanned exam paper into `input/`, then run the pipeline:

```bash
# Process the first PDF found in input/
python main.py

# Pick a specific file (name in input/, or an absolute path)
python main.py --input Exam.pdf

# Ignore the cache and re-run every phase from scratch
python main.py --force

# Skip the environment checks
python main.py --skip-preflight

# Just verify the environment, then exit (no API calls)
python main.py --preflight-only

# Run the batch stress test on all PDFs in input/ directory
python batch_stress_runner.py
```

---

## Output & file naming

The original filename (`[Original_Name]`) is preserved verbatim across every directory:

```
AUTONOMOUS PDF-TO-LATEX/
├── input/
│   └── Exam.pdf                              # your source file
├── output/
│   ├── Exam_LaTeX_compiled.tex               # generated LaTeX source
│   └── Exam_LaTeX_compiled.pdf               # final typeset PDF
└── cache/
    ├── Exam_state.json                       # resumable state for this file
    ├── api_usage.json                        # per-key, per-model 24 h request ledger
    └── images/
        ├── Exam_page_1_q_Q1a_pos_1.png       # losslessly cropped diagrams
        └── Exam_page_4_q_Q3b_pos_2.png
```

## Caching & resumption

Every milestone is written to `cache/[Original_Name]_state.json`, the single source of truth for
resumption. Each phase checks it and **skips work already recorded**. So if local compilation fails
(or you Ctrl-C, or you hit the daily ceiling), just re-run `python main.py` — it picks up exactly
where it left off and **never repeats a completed API transaction**. Use `--force` to start clean.

---

## Project structure

| File | Role |
|------|------|
| `main.py` | Orchestrator / CLI. Sequences the four phases with cache-resume. |
| `preflight.py` | Phase 1 — environment verification + dependency bootstrap. |
| `extractor.py` | Phase 2 — uploads the PDF and extracts the full layout as structured JSON. |
| `processor.py` | Phase 3 — offline 300 DPI rendering + lossless diagram cropping. |
| `compiler.py` | Phase 4 — LaTeX generation, `pdflatex` compilation, log-driven self-correction. |
| `evaluator.py` | Phase 5 — Offline semantic fidelity evaluation to prevent text deletion. |
| `batch_stress_runner.py` | Orchestrator for batch evaluating multiple PDFs and rotating API keys. |
| `gemini.py` | The single chokepoint for all API access: client, per-model throttle, fallback chain, and the usage ledger. |
| `config.py` | Paths, the `MODEL_CHAIN` spec + per-model limits, and UTF-8 console setup. |
| `state.py` | Atomic per-file cache state machine. |

---

## Troubleshooting

- **`pdftoppm` / `Unable to get page count` / `poppler` errors** — the most common breakage. Poppler
  is installed but its `bin/` isn't on the `PATH` your shell exports to child processes. Re-run
  `python main.py --preflight-only`; if it can't find `pdftoppm`, add Poppler's `bin/` folder to PATH
  and reopen the terminal. (The pipeline also passes Poppler's path explicitly to `pdf2image`, so a
  successful preflight is the reliable signal.)
- **`pdflatex: command not found`** — install MiKTeX/TeX Live (see the table above) and reopen the
  shell. MiKTeX may prompt to install missing packages on first compile — allow it.
- **`UnicodeEncodeError` on the status glyphs (→ ✓ ✗ ⚡)** — handled automatically: `config.py`
  reconfigures stdout/stderr to UTF-8 at import. If you see it, you're on a very old Python — upgrade.
- **"Every model in the fallback chain is unavailable or out of budget"** — you've spent the daily
  free-tier quota across all three models. Wait ~24 h for it to reset, or set a new `GEMINI_API_KEY`.
  Your cached progress is preserved.

---

## Notes & limits

- Designed and tested on **Windows 11**, but written to run on macOS/Linux too — external binaries
  are discovered at runtime (never hard-coded) and all paths are built with `pathlib`.
- The live `generate` calls are the only paths that spend budget; everything else (preflight,
  slicing, compilation) is free and can be exercised offline.
- Tune behavior via environment variables: `GEMINI_MODEL` pins the chain to a single model,
  `GEMINI_MAX_ATTEMPTS` sets the per-model retry count, `GEMINI_MEDIA_RESOLUTION` / `GEMINI_THINKING_LEVEL`
  override the quality knobs.
