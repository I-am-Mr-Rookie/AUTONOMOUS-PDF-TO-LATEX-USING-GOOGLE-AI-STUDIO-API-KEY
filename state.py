"""Per-file cache state machine with atomic JSON persistence.

cache/<name>_state.json is the single source of truth for resumption. Each phase
checks it and skips work already recorded, so a crash or a failed local compile
never re-spends a Gemini request. Writes are atomic (temp file + os.replace) so an
interrupted run cannot leave a half-written, unparseable state file behind.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from config import CACHE_DIR


def state_file(original_name: str) -> Path:
    return CACHE_DIR / f"{original_name}_state.json"


def load_state(original_name: str) -> dict[str, Any]:
    path = state_file(original_name)
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            # Corrupt/half-written state: start clean rather than crash the run.
            return {}
    return {}


def save_state(original_name: str, state: dict[str, Any]) -> None:
    path = state_file(original_name)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, ensure_ascii=False)
        os.replace(tmp, path)  # atomic on both Windows and POSIX
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def has_milestone(state: dict[str, Any], key: str) -> bool:
    return state.get(key) is not None
