# Second-pass deep review: notes

This builds on `REVIEW_FIXES.md` (first pass) and `FRONTEND_NOTES.md`. None of their fixes were redone.
No secret values were copied anywhere. Nothing was committed or pushed.

**Superpowers skills used:** `superpowers:test-driven-development` (every bug fix below started as a
failing test, and I watched it fail for the right reason before fixing), `superpowers:systematic-debugging`
(I reproduced each bug and traced it to its root cause before changing code), and
`superpowers:verification-before-completion` (fresh full test, lint and CLI runs before writing this).

## What I examined

- Every module on the critical path, line by line:
  - `gemini.py`: fallback chain, retry/backoff, ledger and fingerprinting, capability adaptation
  - `extractor.py`, `processor.py`, `compiler.py` (generation and correction loop, `_clean`, overfull logic)
  - `evaluator.py`, `state.py`, `main.py`
  - `frontend/server.py` and the page's JS (upload → list → run flow)
- Real `google-genai` `APIError` objects: confirmed `.code` and `.message` behave as the fallback logic assumes.
- Python-version claims: README says 3.10+.
- README, CONTRIBUTING and `.env.example`, checked against the code.

## Bugs fixed (each with a failing test first)

| # | Severity | File | Problem | Fix |
|---|----------|------|---------|-----|
| 1 | **High** | `processor.py:112` | `f"... {d.get("box_2d")} ..."` reuses the enclosing quote inside an f-string. That is only legal from Python 3.12 (PEP 701), so on Python 3.10/3.11 (which the README says are supported) `main.py` died with a `SyntaxError` at import, before doing anything. The first pass introduced it (commit 6e0e6d5), and its 3.12 venv couldn't see it. | Use single quotes inside. A test now tokenizes every `.py` file and fails on any same-quote reuse inside an f-string. |
| 2 | Medium | `compiler.py` `_clean` | Only stripped a code fence when the reply *started* with one. Gemma (the fallback tier, which has no system-instruction support) often replies "Here is the LaTeX: ```latex … ```". That prose was written into the `.tex`, the compile failed, and scarce correction requests were burned on a document that was fine. | Slice from `\documentclass` to the last `\end{document}`. The old fence strip is kept as the fallback. Slicing (rather than fence regex) keeps ```` ``` ```` inside `lstlisting` code intact, and a test covers that. |
| 3 | Medium | `main.py` + `extractor.py` + `state.py` | The cache is keyed by filename only. If you replaced `input/Exam.pdf` with a different exam of the same name (which the web UI makes easy, since uploads overwrite), the next run silently returned the **old** exam's LaTeX. | Extraction now records `source_sha256`. `main` compares it with the current file's hash and, if it differs, prints a notice and runs as `--force`. Old state files without a hash behave as before. |
| 4 | Medium | `frontend/server.py`, `main.py` | Scanners commonly write `Scan.PDF`. The UI accepted the upload, but `list_files` globbed `*.pdf`, which is case-sensitive on Linux and macOS, so the file never appeared and couldn't be run. `python main.py` (no `--input`) also ignored it. | Match the suffix case-insensitively in both places. |
| 5 | Low | `main.py` `_find_pdf` | `--input some/dir/x.pdf` (a relative path) was always re-rooted under `input/` and reported "not found". | Use the path as given if it is a file; otherwise look the name up in `input/`. |
| 6 | Low | `main.py` | `RuntimeError`s that phases raise with user-facing messages (invalid model JSON, missing extraction, `pdftotext` missing) surfaced as raw tracebacks. | Caught and printed as a clean message saying cached steps are kept. |
| 7 | Low | `frontend/server.py` | `GET /api/job?since=abc` raised `ValueError` in the handler, which dropped the connection. | A non-numeric `since` is treated as 0. |

## Tests added

- **New `tests/test_pipeline.py`: 40 tests**, offline (no API calls, no pdflatex/Poppler; the SDK, pdf2image
  and subprocess are faked, and Pillow is used for real). Coverage:
  - **Fallback chain (10):**
    - primary success is recorded once
    - a per-day 429 switches models without retry and without recording
    - 5xx is retried, then falls back; 404 falls back
    - a transient error followed by success stays on the same model
    - all models down raises `BudgetExceeded`
    - an unexpected 401 propagates
    - a full local ledger skips the model
    - stale or corrupt ledger entries and an unparseable ledger are ignored
    - a new key gets a fresh budget, and the raw key is never on disk
    - Gemma gets the system instruction and JSON directive folded into the prompt, with no schema, thinking or system config
  - **Compiler loop (6):**
    - one request on success
    - correction requests are capped at 1 + `MAX_CORRECTION_LOOPS`
    - a resume reuses the generated `.tex` (0 new requests)
    - a cached success skips the phase
    - the overfull 10pt threshold
    - error-block extraction
  - **Processor (2):**
    - real crop pixel maths with padding
    - swapped corners, junk and degenerate boxes skipped
    - the stale-crop purge with a glob-metacharacter name doesn't touch other files
  - **Plus:**
    - extractor JSON parsing and `iter_diagrams` junk tolerance
    - evaluator pass/fail/scanned/missing-output
    - state round-trip and corrupt-file handling
    - `_clean` (6)
    - `_find_pdf` (4)
    - source-change detection (3)
    - clean `RuntimeError` exit
    - the Python 3.10 f-string guard
- **`frontend/test_server.py`: +2 tests** (uppercase `.PDF` listed; bad `since` param), 3 in total.
- 11 bug-reproducing tests (9 pipeline, 2 frontend) were seen failing before their fixes. The
  backticks-in-`lstlisting` test was added afterwards to guard against `_clean` mangling code. The coverage tests passed on the first run, so I
  **mutation-checked** four of them: I disabled the per-day-429 switch, widened the correction loop bound,
  removed `glob.escape`, and removed `_record_request`. The corresponding test failed every time, and the
  originals were restored afterwards.
- **Final result (fresh run):** `python -m unittest discover tests` → **Ran 40 tests, OK**.
  `python -m unittest frontend/test_server.py` → **Ran 3 tests, OK**. `pyflakes` on all `.py` files is clean.

## Dependency updates

`requirements.txt` is unpinned, so there is nothing to bump: a fresh install already gets the latest. I
installed into a clean venv and got google-genai 2.31.0, pdf2image 1.17.0, pillow 13.0.0 and pypdf 6.21.1.
The full suite passes on those versions, and the SDK calls the code depends on
(`ThinkingConfig(thinking_level=…)`, `GenerateContentConfig(media_resolution=…)`, `APIError.code/.message`)
were exercised against them. I did not add pins, for the same reason as the first pass: the minimum
google-genai version is unverified.

## Docs corrected

- README: `--input` accepts a name in `input/` **or a path** (it used to say an absolute path only).
  - Added a **Tests** section with both commands, and `tests/` in the project-structure table.
  - Documented the new source-hash cache invalidation.
- CONTRIBUTING: the PR checklist now runs the tests as well as preflight.
- Verified as accurate: install commands, preflight behaviour (`--preflight-only` exits 1 with install hints
  on this host), `stress_eval_loop.py` (runs; 40% here because there is no TeX, Poppler or key, the same as
  in the first pass), and the `.env` precedence.

## Intentionally left alone

- **No real end-to-end run.** This host has no pdflatex or Poppler and no sudo to install them, and a live
  run would spend API quota. The pdflatex/pdftoppm/pdftotext calls are faked in the tests.
- **Tested on 3.12 only.** No 3.10/3.11 interpreter is available here. The tokenizer test guards the one
  3.12-only construct found, and I grepped for other 3.11+ features (`tomllib`, `except*`, `Self`, …)
  and found none. All modules use `from __future__ import annotations`, so `X | None` hints are safe on 3.10.
- **An extraction reply that isn't valid JSON is not cached.** A re-run re-spends that request. Caching
  the raw reply plus a repair path is a design change.
- **Special characters in input filenames** (`%`, `#`, `&`) end up in `\includegraphics` paths and break
  compilation. Renaming crops would change the documented file layout. I'm noting it rather than changing it.
- **Network-level errors** (DNS or connection failures from httpx) still surface as tracebacks. They are not
  `APIError`s, and catching them broadly could hide real bugs.
- **Ledger entries that are naive datetimes or non-strings** would raise `TypeError`. Only a hand-edited
  ledger can contain them, because the code always writes aware ISO strings (checked in git history).
- **`--input` now prefers a cwd-relative file over `input/<name>`** when both exist. The web UI runs with
  cwd = repo root, which holds no PDFs, so in practice the two never collide.
- Everything listed under "Deliberately not fixed" in `REVIEW_FIXES.md` (no pdflatex timeout, evaluator
  weakness on scanned PDFs, sample JPGs, etc.) still applies. Those reasons were not revisited.
