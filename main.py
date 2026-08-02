"""Orchestrator / CLI runner for the autonomous PDF -> LaTeX pipeline.

Sequences the four phases, each independently resumable from the per-file cache.
Safe to re-run: completed phases are skipped, so a crash, a failed compile, or a
hit free-tier ceiling never costs a repeated API request.

Usage:
    python main.py                     # process the first PDF in input/
    python main.py --input Exam.pdf    # process a specific file
    python main.py --force             # ignore cache, re-run every phase
    python main.py --preflight-only    # just verify the environment
"""
from __future__ import annotations

import argparse
import getpass
import os
import sys
from pathlib import Path

import compiler
import config
import extractor
import gemini
import preflight
import processor


def _obtain_api_key() -> str:
    """Get the Google AI Studio key without persisting it anywhere.

    Non-interactive runs may pass it via the GEMINI_API_KEY environment variable
    (not a file); otherwise we prompt without echo. The key lives only in memory.
    """
    env_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if env_key:
        print("  • API key taken from the GEMINI_API_KEY environment variable.\n")
        return env_key
    try:
        key = getpass.getpass("Enter your Google AI Studio API key (input hidden): ").strip()
    except (EOFError, KeyboardInterrupt):
        sys.exit("\nNo API key provided.")
    if not key:
        sys.exit("No API key provided.")
    print()
    return key


def _find_pdf(explicit: str | None) -> Path:
    if explicit:
        candidate = Path(explicit)
        if not candidate.is_absolute():
            candidate = config.INPUT_DIR / explicit
        if not candidate.exists():
            sys.exit(f"Input PDF not found: {candidate}")
        return candidate

    config.INPUT_DIR.mkdir(parents=True, exist_ok=True)
    pdfs = sorted(config.INPUT_DIR.glob("*.pdf"))
    if not pdfs:
        sys.exit(f"No PDF in {config.INPUT_DIR}. Drop your exam paper there and re-run.")
    if len(pdfs) > 1:
        print(f"Multiple PDFs found; using the first: {pdfs[0].name}")
    return pdfs[0]


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Autonomous PDF->LaTeX pipeline (Gemini free-tier aware)."
    )
    ap.add_argument("--input", help="PDF filename in input/ or a path. Default: first PDF in input/.")
    ap.add_argument("--force", action="store_true", help="Ignore cache and re-run every phase.")
    ap.add_argument("--skip-preflight", action="store_true", help="Skip the environment checks.")
    ap.add_argument("--preflight-only", action="store_true", help="Run preflight, then exit.")
    args = ap.parse_args()

    config.ensure_dirs()

    if not args.skip_preflight:
        passed = preflight.run()
        if args.preflight_only:
            sys.exit(0 if passed else 1)
        if not passed:
            sys.exit("Resolve the preflight issues above, then re-run.")
    elif args.preflight_only:
        return

    # Key is requested only after the environment checks out, and is never stored.
    gemini.set_api_key(_obtain_api_key())

    pdf_path = _find_pdf(args.input)
    original_name = pdf_path.stem
    print(f"=== Pipeline for '{pdf_path.name}' ===")
    print(f"  Model budget today →  {gemini.budget_summary()}\n")

    try:
        extractor.run(pdf_path, original_name, force=args.force)   # Phase 2 (1 request)
        processor.run(pdf_path, original_name, force=args.force)   # Phase 3 (0 requests)
        result = compiler.run(pdf_path, original_name, force=args.force)  # Phase 4 (<=1+3 requests)
        if result.get("success"):
            import evaluator
            eval_result = evaluator.run(pdf_path, original_name, force=args.force)
            if not eval_result.get("success"):
                print(f"\n⚠ WARNING: Semantic Evaluation Failed (Score: {eval_result.get('score', 0.0):.1%}). Hallucination risk detected!")
    except gemini.BudgetExceeded as exc:
        sys.exit(f"\n⏸ {exc}")
    except KeyboardInterrupt:
        sys.exit("\nInterrupted — progress is cached; re-run to resume.")

    if result.get("success"):
        served_by = gemini.active_model()
        suffix = f"  (via {served_by})" if served_by else ""
        print(f"🎉 Done: {result['pdf']}{suffix}")
    else:
        print(
            f"⚠ LaTeX did not compile cleanly. Inspect {result.get('tex')} and its .log, "
            "then re-run to retry the self-correction loop."
        )


if __name__ == "__main__":
    main()
