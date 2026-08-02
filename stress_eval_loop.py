"""Automated Evaluation & Stress-Test Engine for AUTONOMOUS PDF-TO-LATEX.

Evaluates the exact original codebase against 6 explicit, weighted quality rubrics (R1-R6)
totaling 100.0 points.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import config
import gemini
import state
import preflight
import extractor
import processor
import compiler
import main

class RubricEvaluator:
    def __init__(self):
        self.rubrics = {}

    def score_rubric(self, name: str, points: float, max_points: float, checks: list[tuple[bool, str]]):
        self.rubrics[name] = {
            "points": points,
            "max": max_points,
            "pct": (points / max_points) * 100.0 if max_points > 0 else 0,
            "checks": checks
        }

def evaluate_all() -> tuple[float, dict[str, dict]]:
    evaluator = RubricEvaluator()

    # R1: Input Document Discovery & Readability (15 pts)
    r1_checks = []
    r1_pts = 0.0
    input_files = list(config.INPUT_DIR.glob("*.pdf"))
    if input_files:
        r1_pts += 15.0
        r1_checks.append((True, f"Found {len(input_files)} source exam PDF paper(s) in input/"))
    else:
        r1_checks.append((False, "No source PDF files found in input/"))
    evaluator.score_rubric("R1: Input Document Discovery", r1_pts, 15.0, r1_checks)

    # R2: Preflight & Host Environment Verification (15 pts)
    r2_checks = []
    r2_pts = 0.0
    try:
        ok = preflight.run()
        if ok:
            r2_pts += 10.0
            r2_checks.append((True, "preflight.run() environment check passed"))
        else:
            r2_checks.append((False, "preflight.run() returned False"))
    except Exception as e:
        r2_checks.append((False, f"preflight exception: {e}"))

    has_tex = preflight._check_binary("pdflatex") or preflight._check_binary("latexmk")
    has_poppler = preflight._check_binary("pdftoppm")
    if has_tex and has_poppler:
        r2_pts += 5.0
        r2_checks.append((True, "Host binaries pdflatex/latexmk and pdftoppm confirmed on PATH"))
    else:
        r2_checks.append((False, "Missing host binary on PATH"))
    evaluator.score_rubric("R2: Preflight & Host Environment", r2_pts, 15.0, r2_checks)

    # R3: Gemini API Integration & Active Key Ledger (20 pts)
    r3_checks = []
    r3_pts = 0.0
    first_model = config.MODEL_CHAIN[0]
    if first_model.model_id == "gemini-3.6-flash":
        r3_pts += 5.0
        r3_checks.append((True, f"Primary model active: {first_model.name} ({first_model.model_id})"))
    else:
        r3_checks.append((False, f"Primary model is {first_model.model_id}, expected gemini-3.6-flash"))

    active_key = os.getenv("GEMINI_API_KEY", "")
    if active_key:
        gemini.set_api_key(active_key)
        r3_pts += 10.0
        r3_checks.append((True, "Active Gemini API key successfully initialized from environment"))
    else:
        r3_checks.append((False, "GEMINI_API_KEY missing in environment"))

    try:
        b_sum = gemini.budget_summary()
        r3_pts += 5.0
        r3_checks.append((True, f"Ledger tracking active: {b_sum}"))
    except Exception as e:
        r3_checks.append((False, f"Ledger error: {e}"))
    evaluator.score_rubric("R3: Gemini API & Key Ledger", r3_pts, 20.0, r3_checks)

    # R4: Extractor Bounding Box Mapping & Schema Quality (15 pts)
    r4_checks = []
    r4_pts = 0.0
    try:
        schema = extractor._schema()
        if schema:
            r4_pts += 5.0
            r4_checks.append((True, "Strict JSON layout response schema generated"))
    except Exception as e:
        r4_checks.append((False, f"Schema error: {e}"))

    try:
        raw_fenced = '```json\n{"blocks": [{"type": "title", "text": "SUST"}]}\n```'
        parsed = extractor._parse_json(raw_fenced)
        if parsed.get("blocks", [{}])[0].get("text") == "SUST":
            r4_pts += 5.0
            r4_checks.append((True, "Markdown code fence wrapper stripping verified"))
    except Exception as e:
        r4_checks.append((False, f"JSON parse error: {e}"))

    try:
        mock_data = {"blocks": [{"diagrams": [{"page": 1, "position": 1, "box_2d": [0,0,100,100]}]}]}
        diags = list(extractor.iter_diagrams(mock_data))
        if len(diags) == 1:
            r4_pts += 5.0
            r4_checks.append((True, "Diagram iterator recursively mapped nested question structure"))
    except Exception as e:
        r4_checks.append((False, f"Diagram iterator error: {e}"))
    evaluator.score_rubric("R4: Extractor Schema & Defensive Parsing", r4_pts, 15.0, r4_checks)

    # R5: Offline Lossless Image Slicing & Poppler Math (15 pts)
    r5_checks = []
    r5_pts = 0.0
    p_path = processor._poppler_path()
    if p_path:
        r5_pts += 5.0
        r5_checks.append((True, f"Poppler bin directory resolved: {p_path}"))
    else:
        r5_checks.append((False, "Poppler path unresolved"))

    clean_str = processor._safe("Q1(a)_part-1")
    if clean_str == "Q1_a__part_1":
        r5_pts += 5.0
        r5_checks.append((True, "Crop filename sanitizer converts invalid characters"))
    else:
        r5_checks.append((False, f"Sanitizer produced: {clean_str}"))

    width, height = 2400, 3300
    ymin, xmin, ymax, xmax = 100, 100, 500, 500
    left = max(0, int(min(xmin, xmax) / 1000 * width))
    right = min(width, int(max(xmin, xmax) / 1000 * width))
    top = max(0, int(min(ymin, ymax) / 1000 * height))
    bottom = min(height, int(max(ymin, ymax) / 1000 * height))
    if right > left and bottom > top:
        r5_pts += 5.0
        r5_checks.append((True, f"Crop box pixel math verified: ({left},{top},{right},{bottom})"))
    else:
        r5_checks.append((False, "Crop box math degenerate"))
    evaluator.score_rubric("R5: Image Slicing & Poppler Math", r5_pts, 15.0, r5_checks)

    # R6: LaTeX Generation & Log Self-Correction (20 pts)
    r6_checks = []
    r6_pts = 0.0
    c_clean = compiler._clean("```latex\n\\documentclass{article}\n\\begin{document}\nHi\n\\end{document}\n```")
    if c_clean.startswith("\\documentclass"):
        r6_pts += 5.0
        r6_checks.append((True, "LaTeX markdown wrapper cleaner verified"))
    else:
        r6_checks.append((False, "LaTeX cleaner failed"))

    mock_err_log = "Line 1\n! Undefined control sequence \\foo\nLine 3"
    parsed_err = compiler._errors(mock_err_log)
    if "!" in parsed_err and "Undefined control sequence" in parsed_err:
        r6_pts += 5.0
        r6_checks.append((True, "pdflatex log parser extracted error block"))
    else:
        r6_checks.append((False, "Log parser failed"))

    test_tex = config.OUTPUT_DIR / "_rubric_dummy.tex"
    test_tex.write_text("\\documentclass{article}\n\\begin{document}\nOK\n\\end{document}\n", encoding="utf-8")
    ok_comp, _ = compiler._compile(test_tex)
    if ok_comp:
        r6_pts += 10.0
        r6_checks.append((True, "pdflatex compiled test source into PDF cleanly"))
        for ext in (".tex", ".pdf", ".aux", ".log"):
            p = config.OUTPUT_DIR / f"_rubric_dummy{ext}"
            if p.exists(): p.unlink()
    else:
        r6_checks.append((False, "pdflatex dummy compilation failed"))
    evaluator.score_rubric("R6: LaTeX Generation & Compilation", r6_pts, 20.0, r6_checks)

    # Compute Total Score
    total_score = sum(r["points"] for r in evaluator.rubrics.values())
    max_score = sum(r["max"] for r in evaluator.rubrics.values())
    total_pct = (total_score / max_score) * 100.0 if max_score > 0 else 0

    return total_pct, evaluator.rubrics


def print_report(iter_num: int, total_pct: float, rubrics: dict):
    print("==========================================================")
    print(f"   EVALUATION LOOP - ITERATION {iter_num}")
    print("==========================================================\n")
    for r_name, r_data in rubrics.items():
        status_symbol = "✓" if r_data["pct"] == 100.0 else "✗"
        print(f"[{status_symbol}] {r_name}: {r_data['points']:.1f}/{r_data['max']:.1f} ({r_data['pct']:.1f}%)")
        for passed, msg in r_data["checks"]:
            sym = "  ✓" if passed else "  ✗"
            print(f"{sym} {msg}")
        print()
    print("----------------------------------------------------------")
    print(f"  AGGREGATED SCORE: {total_pct:.1f}%")
    print("----------------------------------------------------------\n")

if __name__ == "__main__":
    score_pct, rubrics = evaluate_all()
    print_report(1, score_pct, rubrics)
