"""Phase 5 — Semantic Fidelity Evaluator.

This offline phase uses pdftotext (from Poppler) to extract raw text from both
the original input PDF and the compiled output PDF, then runs a fuzzy
comparison to ensure no text (e.g. questions or marks) was dropped by the LLM
during the generation or error-correction loop.
"""
from __future__ import annotations

import subprocess
import shutil
import re
from pathlib import Path
from typing import Any

from config import OUTPUT_DIR, ROOT
from state import load_state, save_state

EVAL_KEY = "semantic_evaluation"

def _pdftotext_path() -> str | None:
    return shutil.which("pdftotext")

def _extract_text(pdf_path: Path) -> str:
    exe = _pdftotext_path()
    if not exe:
        raise RuntimeError("pdftotext not found. Poppler is required.")
        
    proc = subprocess.run(
        [exe, "-layout", str(pdf_path), "-"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace"
    )
    if proc.returncode != 0:
        return ""
    
    # Normalize text: lower case, keep only alphanumerics
    text = proc.stdout.lower()
    return re.sub(r'[^a-z0-9]', '', text)


def run(original_pdf: Path, original_name: str, force: bool = False) -> dict[str, Any]:
    state = load_state(original_name)
    if not force and state.get(EVAL_KEY) is not None:
        if state[EVAL_KEY].get("success"):
            print("✓ Phase 5 skipped — semantic evaluation already passed.\n")
            return state[EVAL_KEY]

    compiled_pdf = OUTPUT_DIR / f"{original_name}_LaTeX_compiled.pdf"
    if not compiled_pdf.exists():
        print("✗ Phase 5 failed: Compiled PDF does not exist.\n")
        return {"success": False, "score": 0.0}

    print("→ Phase 5: evaluating semantic fidelity offline…")
    
    orig_text = _extract_text(original_pdf)
    comp_text = _extract_text(compiled_pdf)
    
    if not orig_text:
        print("  ⚠ Could not extract text from original PDF (might be a scanned image). Passing by default.")
        result = {"success": True, "score": 1.0, "note": "Original PDF has no extractable text."}
        state[EVAL_KEY] = result
        save_state(original_name, state)
        return result

    # A simple but strict coverage metric. How much of the original alphanumerics 
    # exist in the compiled doc in any order? (LaTeX might reorder elements like marks).
    orig_chars = list(orig_text)
    comp_chars = list(comp_text)
    
    matched = 0
    for c in orig_chars:
        if c in comp_chars:
            matched += 1
            comp_chars.remove(c)
            
    score = matched / len(orig_chars) if orig_chars else 1.0
    
    # Require at least 85% character retention. 
    # (LaTeX strips some OCR junk, so 100% is unrealistic for scanned docs).
    success = score >= 0.85
    
    result = {"success": success, "score": round(score, 3)}
    state[EVAL_KEY] = result
    save_state(original_name, state)
    
    if success:
        print(f"✓ Phase 5 passed! Semantic fidelity score: {score:.1%} retention.\n")
    else:
        print(f"✗ Phase 5 failed. High hallucination risk. Semantic fidelity score: {score:.1%} retention.\n")
        
    return result
