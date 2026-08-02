"""Phase 3 — offline lossless slicing (consumes ZERO API requests).

Reads the cached extraction, renders only the pages that actually contain
diagrams to 300 DPI bitmaps via Poppler/pdf2image, and crops each diagram to
cache/images/ using the filename layout:
    <name>_page_<n>_q_<qid>_alt_<a>_pos_<k>.png
Boxes are Gemini's normalized 0-1000 [ymin, xmin, ymax, xmax]; they're converted
to pixel coordinates against each rendered page's true dimensions. Stale crops
from a previous run of the same file are purged first so re-runs never leave
orphaned images behind.
"""
from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from config import DPI, IMAGES_DIR
from extractor import iter_diagrams
from state import load_state, save_state

CROP_KEY = "cropped_images"


def _poppler_path() -> str | None:
    """pdf2image needs Poppler; pass its bin dir explicitly so a PATH that's fine
    in the shell but not inherited by the child process still works on Windows."""
    exe = shutil.which("pdftoppm")
    return str(Path(exe).parent) if exe else None


def _safe(s: Any) -> str:
    cleaned = "".join(c if c.isalnum() else "_" for c in str(s)).strip("_")
    return cleaned or "x"


def _purge_existing(original_name: str) -> int:
    """Delete crops left by a prior run of this file (keeps cache/images/ clean)."""
    removed = 0
    for old in IMAGES_DIR.glob(f"{original_name}_page_*.png"):
        try:
            old.unlink()
            removed += 1
        except OSError:
            pass
    return removed


def run(pdf_path: Path, original_name: str, force: bool = False) -> list[dict[str, Any]]:
    state = load_state(original_name)
    if not force and state.get(CROP_KEY) is not None:
        print("✓ Phase 3 skipped — crops already cached.\n")
        return state[CROP_KEY]

    extraction = state.get("extraction_result")
    if not extraction:
        raise RuntimeError("No extraction_result in cache — run Phase 2 first.")

    from pdf2image import convert_from_path

    IMAGES_DIR.mkdir(parents=True, exist_ok=True)
    purged = _purge_existing(original_name)
    if purged:
        print(f"  🧹 purged {purged} stale crop(s) from a previous run.")
    poppler = _poppler_path()

    # Group diagrams by page so each page is rendered at most once.
    by_page: dict[int, list[tuple[str, int, dict[str, Any]]]] = {}
    for qid, ai, d in iter_diagrams(extraction):
        pno = int(d.get("page", 0) or 0)
        if pno >= 1:
            by_page.setdefault(pno, []).append((qid, ai, d))

    crops: list[dict[str, Any]] = []
    print("→ Phase 3: cropping diagrams offline (0 API requests)…")
    for pno in sorted(by_page):
        rendered = convert_from_path(
            str(pdf_path), dpi=DPI, first_page=pno, last_page=pno, poppler_path=poppler
        )
        if not rendered:
            print(f"  ! could not render page {pno}; skipping.")
            continue
        img = rendered[0]
        width, height = img.size

        for qid, ai, d in by_page[pno]:
            pos = int(d.get("position", len(crops) + 1))
            box = d.get("box_2d") or []
            if len(box) != 4:
                print(f"  ! page {pno} {qid}: bad box {box}; skipping.")
                continue
            ymin, xmin, ymax, xmax = box
            raw_left = min(xmin, xmax) / 1000 * width
            raw_right = max(xmin, xmax) / 1000 * width
            raw_top = min(ymin, ymax) / 1000 * height
            raw_bottom = max(ymin, ymax) / 1000 * height

            box_width = raw_right - raw_left
            box_height = raw_bottom - raw_top
            pad_x = box_width * 0.05
            pad_y = box_height * 0.05

            left = max(0, int(raw_left - pad_x))
            right = min(width, int(raw_right + pad_x))
            top = max(0, int(raw_top - pad_y))
            bottom = min(height, int(raw_bottom + pad_y))
            if right - left < 2 or bottom - top < 2:
                print(f"  ! page {pno} {qid}: degenerate box {box}; skipping.")
                continue

            fname = f"{original_name}_page_{pno}_q_{_safe(qid)}_alt_{ai}_pos_{pos}.png"
            out_path = IMAGES_DIR / fname
            img.crop((left, top, right, bottom)).save(out_path)
            crops.append(
                {
                    "page": pno,
                    "question_id": qid,
                    "alt": ai,
                    "position": pos,
                    "caption": d.get("caption", ""),
                    "path": str(out_path),
                }
            )
            print(f"  ✂ {fname}")

    state[CROP_KEY] = crops
    save_state(original_name, state)
    print(f"✓ Phase 3 done — {len(crops)} diagrams cropped (0 API requests).\n")
    return crops
