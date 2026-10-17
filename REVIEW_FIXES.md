# Code Review & Fixes

**Project:** a Python CLI that converts a scanned PDF exam into typeset LaTeX. Its phases are: preflight →
one Gemini extraction request (structured JSON + diagram boxes) → offline 300 DPI cropping →
LaTeX generation and pdflatex compile with up to 3 LLM correction loops → offline text-fidelity check.
It has a per-key, per-model free-tier request ledger and a fallback chain of three models.

No secret values were copied anywhere. The `.env` file is gitignored, and `.env.example` holds only placeholders.

## Issues found and fixed

| # | Severity | File | Issue | Fix |
|---|----------|------|-------|-----|
| 1 | High | `config.py` | `.env` was loaded with `override=True` (and the fallback parser always assigned). An exported `GEMINI_API_KEY` was therefore silently replaced by the old `.env` key. That breaks the documented recovery path ("set a NEW API key via GEMINI_API_KEY"). | `override=False` / `os.environ.setdefault`, so the shell environment wins. |
| 2 | High | `.env.example` | Set `GEMINI_MODEL=gemini-3.6-flash`. Following the README (`cp .env.example .env`) pinned the chain to one model and **disabled the fallback chain** (20 requests/day instead of ~14k). | Commented out, with an explanation. |
| 3 | High | `compiler.py` | Any `Overfull \hbox` (even 0.1pt, which is very common and cosmetic) counted as a compile failure. Each one spent a scarce correction request and could mark a valid PDF as failed. The code comment said "if significant". | Fail only when an overfull box is > 10pt (`OVERFULL_LIMIT_PT`). |
| 4 | Medium | `compiler.py` | The initial LaTeX generation was not recorded until the whole compile loop finished. A crash or Ctrl-C during compilation re-spent that request on the next run. | Save the `generated` milestone right after the `.tex` is written. |
| 5 | Medium | `evaluator.py` | Character-coverage metric used `list.remove` inside a loop, which is O(n·m). On a multi-page text PDF that is billions of operations, so it effectively hangs. | `Counter` multiset intersection gives the identical result in O(n). Checked against the old loop. |
| 6 | Medium | `preflight.py` | Preflight passed with only `latexmk` present, but `compiler.py` invokes `pdflatex` directly (FileNotFoundError). It also never checked `pdftotext`, which `evaluator.py` hard-requires (RuntimeError *after* all API spend). | Require `pdflatex`, `pdftoppm` and `pdftotext`. |
| 7 | Medium | `extractor.py` | `_parse_json` only stripped text around the JSON when the reply began with a code fence. Gemma (no schema support) often adds prose → `JSONDecodeError` traceback. Replies that are not objects, or lack `blocks`, crashed later with unclear errors. | Slice `{…}` whenever the reply doesn't start with `{`; raise a clear `RuntimeError` on invalid or missing `blocks`. |
| 8 | Medium | `processor.py` | Model-supplied `page`/`position`/`box_2d` were trusted. `position: null` → `int(None)` TypeError; string or non-numeric coords crashed the phase. | Each diagram is parsed in try/except; bad entries are skipped with a message. |
| 9 | Low | `extractor.py` | `iter_diagrams` crashed on `"diagrams": null` or non-dict entries. | Tolerates them. |
| 10 | Low | `processor.py` | The stale-crop purge glob used the raw filename. Names containing `[`/`]`/`*` purged the wrong files or none. | `glob.escape`. |
| 11 | Low | `gemini.py` | The usage ledger was written non-atomically. A torn write parses as `{}`, which silently resets the daily budget. | Write to a temp file, then `os.replace`. |
| 12 | Low | `config.py` | `GEMINI_MAX_ATTEMPTS=0` made `_attempt_model` return `None` → `AttributeError` on `.text`. | Clamped to ≥ 1. |
| 13 | Low | `stress_eval_loop.py` | Crashed with FileNotFoundError when pdflatex was missing. Left `_rubric_dummy.*` files behind on failure. Checked `latexmk` inconsistently. Had 5 unused imports. | Catch `OSError`, always clean up, check the same binaries as preflight, remove dead imports. |
| 14 | Low | `gemini.py` | Dead globals `_api_keys`, `_key_index`. The module docstring listed an outdated model chain. | Removed / corrected. |
| 15 | Low | `main.py`, `evaluator.py` | `evaluator` was imported lazily inside `try`. `config.ROOT` was imported but unused. The docstring said the key is "not a file" although `.env` is supported. | Top-level import, unused import removed, docstring corrected. |
| 16 | Low (docs) | `README.md` | Referenced a non-existent `batch_stress_runner.py` (twice). Showed the wrong crop filename format (`_q_Q1a_pos_1` vs the actual `_q_B<n>_alt_<k>_pos_<p>`). Said "four phases" and "pdflatex (or latexmk)". Omitted `pdftotext`. Didn't say that pinning `GEMINI_MODEL` disables fallback. | All corrected. `stress_eval_loop.py` is documented instead. |

## Deliberately not fixed

- **Committed sample inputs are JPG/JPEG, but the pipeline only accepts PDFs** (`input/*.pdf`, `pdf2image`). The samples are never used. Adding image input would be a feature, not a fix. Also consider whether these exam scans should be in a public repo at all.
- **The Phase 5 evaluator always passes for scanned PDFs.** No text can be extracted from them, and scanned PDFs are the project's stated main use case. The metric is also a bag-of-characters, so it cannot detect reordering or meaning changes. A real fix needs OCR or an LLM comparison, which is a design change.
- **pdflatex has no explicit `-no-shell-escape` and no timeout.** TeX Live and MiKTeX already default to restricted shell escape. The flag spelling differs between distros, and the main target is Windows/MiKTeX, so adding it risks breaking compiles. A hung pdflatex is unlikely with `-interaction=nonstopmode -halt-on-error`.
- **An invalid API key (HTTP 400) falls through every model** and ends with the "wait ~24h" message. The actual 400 reason is printed per model, so the user can still see it. Treating it specially would need fragile message matching.
- **Uploaded Files API objects are never deleted** (Google removes them after 48h), and the upload is not cached across runs. It uses a separate quota, so the cost is minor.
- **Repo paths with spaces in `\includegraphics`.** Modern graphicx handles them, but very old TeX installs may not. Not changed.
- **`requirements.txt` is unpinned.** `ThinkingConfig(thinking_level=…)` needs a recent `google-genai`. I did not confirm the exact minimum version, so I did not guess a pin.
- **`Plan.txt`** is the original design prompt. It says the API key goes in a "secure config file", which contradicts the implementation. Left as a historical document.

## Build / test results

The repo has no test suite, no lint config and no build step. I verified in a throwaway venv at `/tmp/rv` (Python 3.12, `google-genai`, `pdf2image`, `pillow`, `pypdf`, `pyflakes`):

- `pyflakes *.py` → **clean**. It caught one regression I had introduced (a stale `box` name in `processor.py`), which is now fixed.
- `py_compile *.py` → **OK**.
- An ad-hoc behavior script, kept outside the repo, checked:
  - JSON parsing with prose, fences and invalid payloads
  - `iter_diagrams` on junk input
  - the overfull threshold
  - Counter metric = old loop
  - `processor.run` with a mocked renderer on bad boxes and a glob-metachar filename
  - the ledger's atomic write and decrement
  - `GEMINI_MAX_ATTEMPTS=0` clamping

  Result: **all passed**.
- `python main.py --preflight-only` → runs and correctly reports missing `pdflatex`/`pdftoppm`/`pdftotext` on this host (exit 1, as intended).
- `python stress_eval_loop.py` → completes (40%, because the host has no TeX/Poppler and no key). Before the fix it crashed with FileNotFoundError.
- **Not run:** a real end-to-end conversion. This host has no pdflatex or Poppler, and running it would spend live API quota.
