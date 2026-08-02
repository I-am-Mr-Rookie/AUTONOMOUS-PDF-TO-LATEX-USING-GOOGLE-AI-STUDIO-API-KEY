"""Phase 4 — LaTeX generation, local compilation, and self-correction.

Generates the .tex from the cached structured extraction + cropped images
(1 request), compiles it with pdflatex, and on failure feeds the parsed error
plus the current full source back to Gemini for a patched full document, then
recompiles. Hard cap of MAX_CORRECTION_LOOPS correction requests, so a
pathological document can cost at most 1 + MAX_CORRECTION_LOOPS requests.
"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any

import gemini
from config import MAX_CORRECTION_LOOPS, OUTPUT_DIR, ROOT
from state import load_state, save_state

LATEX_KEY = "compilation"

SYSTEM = (
    "You are a LaTeX typesetting expert. Output ONLY compilable LaTeX source for "
    "pdflatex — no markdown fences, no prose, no explanation."
)


def _gen_prompt(extraction: dict[str, Any], crops: list[dict[str, Any]]) -> str:
    lines = [
        "Typeset this document as a complete, publication-quality LaTeX document that "
        "reproduces the ORIGINAL faithfully.",
        "",
        "Document requirements:",
        r"- \documentclass[11pt]{article}; load amsmath, amssymb, graphicx, geometry, "
        "float, enumitem, listings, xcolor, booktabs. Use 1in margins.",
        "- Interpret the semantic `type` and `label` of each block to apply appropriate LaTeX formatting:",
        "  * Use appropriate section headings (\\section*, \\subsection*, etc.) for titles and section headings.",
        "  * Use \\begin{enumerate} or \\begin{itemize} for lists and questions with labels (like '1.', 'a)').",
        "  * Keep all mathematics in proper LaTeX. Render code using lstlisting with a small monospace style.",
        "  * If a block has `marks`, show them right-aligned in bold, e.g. \\hfill\\textbf{[5]}.",
        r"- FIGURES: embed each diagram with \includegraphics[width=0.6\linewidth]{ABSOLUTE_PATH} "
        "inside a figure[H] with its caption, using EXACTLY the absolute paths listed below "
        "(already forward-slashed). Place each near the block it belongs to.",
        "",
        "## Diagram images (use these exact paths):",
    ]
    if crops:
        for c in crops:
            abs_path = str(Path(c["path"]).resolve()).replace("\\", "/")
            cap = f' — caption "{c["caption"]}"' if c.get("caption") else ""
            lines.append(
                f'- block {c["question_id"]} (diagram index {c.get("alt", 0)}), '
                f'position {c["position"]}{cap}: {abs_path}'
            )
    else:
        lines.append("- (none)")
    lines += [
        "",
        "## Extracted Document JSON:",
        json.dumps(extraction, ensure_ascii=False, indent=1),
    ]
    return "\n".join(lines)


def _clean(text: str) -> str:
    text = (text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text).strip()
    return text


def _compile(tex_path: Path) -> tuple[bool, str]:
    """Run pdflatex from the project root, emitting artifacts into output/."""
    proc = subprocess.run(
        [
            "pdflatex",
            "-interaction=nonstopmode",
            "-halt-on-error",
            f"-output-directory={OUTPUT_DIR}",
            str(tex_path),
        ],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    log_path = OUTPUT_DIR / (tex_path.stem + ".log")
    log_text = log_path.read_text(encoding="utf-8", errors="replace") if log_path.exists() else ""
    pdf_path = OUTPUT_DIR / (tex_path.stem + ".pdf")
    ok = proc.returncode == 0 and pdf_path.exists()
    
    # Treat Overfull \hbox as a failure if it's significant (e.g., more than a few pts, or just generally)
    if ok and "Overfull \\hbox" in log_text:
        ok = False
        
    return ok, (log_text or proc.stdout or proc.stderr)


def _errors(log_text: str) -> str:
    """Extract pdflatex error blocks and Overfull warnings for a focused fix prompt."""
    lines = log_text.splitlines()
    blocks = []
    for i, ln in enumerate(lines):
        if ln.startswith("!") or "Overfull \\hbox" in ln:
            blocks.append("\n".join(lines[i : i + 6]))
    return "\n---\n".join(blocks[:5]) or log_text[-1500:]


def run(pdf_path: Path, original_name: str, force: bool = False) -> dict[str, Any]:
    state = load_state(original_name)
    prior = state.get(LATEX_KEY)
    if not force and prior and prior.get("success"):
        print("✓ Phase 4 skipped — already compiled successfully.\n")
        return prior

    extraction = state.get("extraction_result")
    if not extraction:
        raise RuntimeError("No extraction_result in cache — run Phase 2 first.")
    crops = state.get("cropped_images", [])

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    tex_path = OUTPUT_DIR / f"{original_name}_LaTeX_compiled.tex"

    # Generate initial LaTeX unless we're resuming with an existing .tex.
    if force or not tex_path.exists() or not (prior and prior.get("generated")):
        print("→ Phase 4: generating initial LaTeX (1 API request)…")
        resp = gemini.generate(_gen_prompt(extraction, crops), system_instruction=SYSTEM)
        tex_path.write_text(_clean(resp.text), encoding="utf-8")
    else:
        print("→ Phase 4: reusing cached .tex; recompiling…")

    pdf_out = OUTPUT_DIR / f"{original_name}_LaTeX_compiled.pdf"

    success = False
    attempts = 0
    for attempt in range(1, MAX_CORRECTION_LOOPS + 2):  # 1 initial compile + N corrections
        attempts = attempt
        print(f"  ⚙ pdflatex attempt {attempt}…")
        ok, log_text = _compile(tex_path)
        if ok:
            success = True
            break
        if attempt > MAX_CORRECTION_LOOPS:
            print("  ✗ exhausted correction loops.")
            break

        err = _errors(log_text)
        print(f"  ! compile failed; sending error to Gemini (correction {attempt})…")
        broken = tex_path.read_text(encoding="utf-8", errors="replace")
        fix_prompt = (
            "The following LaTeX fails to compile under pdflatex. Fix ONLY what is "
            "necessary and return the COMPLETE corrected document.\n\n"
            f"## pdflatex errors:\n{err}\n\n## Current source:\n{broken}"
        )
        try:
            resp = gemini.generate(fix_prompt, system_instruction=SYSTEM)
        except gemini.BudgetExceeded as exc:
            print(f"  ⏸ {exc}")
            break
        tex_path.write_text(_clean(resp.text), encoding="utf-8")

    result = {
        "success": success,
        "attempts": attempts,
        "generated": True,
        "tex": str(tex_path),
        "pdf": str(pdf_out) if success else None,
    }
    state[LATEX_KEY] = result
    save_state(original_name, state)

    if success:
        print(f"✓ Phase 4 done — compiled in {attempts} attempt(s): {pdf_out}\n")
    else:
        log_hint = OUTPUT_DIR / f"{original_name}_LaTeX_compiled.log"
        print(f"✗ Phase 4 failed after {attempts} attempt(s). Inspect {log_hint}\n")
    return result
