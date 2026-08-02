"""Phase 2 — scan & coordinate extraction (exactly ONE generate request).

Uploads the source PDF once, then asks Gemini to map the entire paper in a single
structured-JSON response: the header/metadata block, Part A/B sections, every
question with its shared intro and per-subpart marks, OR-alternatives, and a
normalized [ymin, xmin, ymax, xmax] box (0-1000, with its page number) for each
diagram. If the cache already holds the result this phase is skipped entirely —
it is the most expensive single moment in the pipeline's daily budget.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import gemini
from state import has_milestone, load_state, save_state

EXTRACTION_KEY = "extraction_result"

PROMPT = (
    "You are an expert document layout analyst. Analyze EVERY page of the attached "
    "scanned PDF in a single pass and return JSON matching the provided schema. "
    "Capture the document COMPLETELY and faithfully — do NOT solve anything.\n\n"
    "STRUCTURE:\n"
    "Represent the entire document as a sequential array of `blocks`.\n"
    "For each block, determine its semantic `type` (e.g., 'title', 'metadata', 'section_heading', "
    "'paragraph', 'question', 'list_item', 'code', 'math').\n"
    "If the block has a printed prefix (like '1.', 'a)', '•', 'Part A'), put it in `label`.\n"
    "If the block has an allocated score (e.g., in an exam), put it in `marks` as an integer.\n\n"
    "MATH & CODE: Render ALL mathematics as inline LaTeX (e.g. $\\varphi(n)$, "
    "$$A_1 : 5\\times 10$$). Transcribe code blocks verbatim, preserving line breaks.\n\n"
    "DIAGRAMS: For EVERY figure, circuit, graph, or hand-drawn diagram, output its `page` "
    "(1-based PDF page number), a `position` (1-based order on that page), a `caption` if "
    "printed, and a bounding box as normalized integers 0-1000 in [ymin, xmin, ymax, xmax] "
    "order. Be generous so the crop fully contains the diagram and its labels. Attach each "
    "diagram to the `diagrams` array of the block that refers to or precedes it. "
    "Do NOT invent diagrams for plain text, coordinate lists, or code."
)


def _schema():
    """Strict response schema (built lazily so importing this module needs no SDK)."""
    from google.genai import types

    T = types.Type
    box = types.Schema(
        type=T.ARRAY,
        items=types.Schema(type=T.INTEGER),
        description="Normalized [ymin, xmin, ymax, xmax], each 0-1000.",
    )
    diagram = types.Schema(
        type=T.OBJECT,
        properties={
            "page": types.Schema(type=T.INTEGER, description="1-based PDF page number"),
            "position": types.Schema(type=T.INTEGER, description="1-based order on that page"),
            "box_2d": box,
            "caption": types.Schema(type=T.STRING),
        },
        required=["page", "position", "box_2d"],
    )
    block = types.Schema(
        type=T.OBJECT,
        properties={
            "type": types.Schema(type=T.STRING, description="Semantic type, e.g. 'title', 'section_heading', 'paragraph', 'question', 'list_item', 'code'"),
            "label": types.Schema(type=T.STRING, description="Printed prefix/numbering, e.g. '1.', 'a)', 'Part A'. Omit if none."),
            "text": types.Schema(type=T.STRING, description="Faithful transcription; math in LaTeX"),
            "marks": types.Schema(type=T.INTEGER, description="Allocated marks/score. Omit if none"),
            "diagrams": types.Schema(type=T.ARRAY, items=diagram),
        },
        required=["type", "text"],
    )
    return types.Schema(
        type=T.OBJECT,
        properties={
            "blocks": types.Schema(type=T.ARRAY, items=block),
        },
        required=["blocks"],
    )


def _parse_json(raw: str) -> dict[str, Any]:
    raw = (raw or "").strip()
    if raw.startswith("```"):  # defensive: strip stray markdown fences
        start, end = raw.find("{"), raw.rfind("}")
        if start != -1 and end != -1:
            raw = raw[start : end + 1]
    return json.loads(raw)


def iter_diagrams(result: dict[str, Any]):
    """Yield (block_id, diagram_index, diagram) across the whole document."""
    for bi, block in enumerate(result.get("blocks", [])):
        for di, d in enumerate(block.get("diagrams", [])):
            yield f"B{bi}", di, d


def run(pdf_path: Path, original_name: str, force: bool = False) -> dict[str, Any]:
    state = load_state(original_name)
    if not force and has_milestone(state, EXTRACTION_KEY):
        print("✓ Phase 2 skipped — extraction already cached.\n")
        return state[EXTRACTION_KEY]

    print("→ Phase 2: uploading PDF and extracting layout (1 API request)…")
    uploaded = gemini.upload_pdf(pdf_path)
    response = gemini.generate([uploaded, PROMPT], json_schema=_schema())
    result = _parse_json(response.text)

    state[EXTRACTION_KEY] = result
    state["source_pdf"] = pdf_path.name
    save_state(original_name, state)

    blocks = result.get("blocks", [])
    n_diag = sum(1 for _ in iter_diagrams(result))
    print(f"✓ Phase 2 done — {len(blocks)} block(s) and {n_diag} diagrams mapped.\n")
    return result
