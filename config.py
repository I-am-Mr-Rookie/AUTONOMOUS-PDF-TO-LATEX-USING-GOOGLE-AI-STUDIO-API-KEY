"""Central configuration: filesystem layout, the free-tier limits that matter,
and the model fallback chain. Every other module imports from here.

What we ration: Google AI Studio's free tier meters each model SEPARATELY by
requests-per-day (RPD) and requests-per-minute (RPM), per API key. Rather than
ride a single model to its ceiling and stop, we define an ordered fallback chain
(MODEL_CHAIN) and move to the next model when the active one is unavailable or its
daily limit is reached. Input tokens-per-minute is generous enough that we do NOT
track it. The API key is supplied at runtime and never written to disk.
"""
from __future__ import annotations

import math
import os
import sys
from dataclasses import dataclass
from pathlib import Path

# Windows consoles default to a legacy code page (e.g. cp1252) that cannot encode
# the status glyphs used throughout the pipeline. Force UTF-8 on the standard
# streams so console output never crashes with UnicodeEncodeError. Done here
# because every module imports config, so this covers every entry point.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError, OSError):
        pass

# --- Filesystem layout (the original filename is preserved across all of these) ---
ROOT = Path(__file__).resolve().parent

def _load_env_file() -> None:
    env_file = ROOT / ".env"
    if env_file.exists():
        try:
            from dotenv import load_dotenv
            load_dotenv(env_file, override=True)
        except ImportError:
            for line in env_file.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    key = k.strip()
                    val = v.strip().strip("'\"")
                    if val:
                        os.environ[key] = val

_load_env_file()

INPUT_DIR = ROOT / "input"
OUTPUT_DIR = ROOT / "output"
CACHE_DIR = ROOT / "cache"
IMAGES_DIR = CACHE_DIR / "images"
USAGE_LEDGER = CACHE_DIR / "api_usage.json"  # per-key rolling-24h request ledger

# --- Gemini model fallback chain + the free-tier ceilings we enforce ---------------
# The pipeline tries each model in order. When the active model is unavailable or its
# daily limit is reached (after a few attempts), it falls back to the next one. Each
# model on the free tier has its OWN per-key daily quota, so the chain multiplies the
# work possible in a day. RPD = requests/day, RPM = requests/minute; context windows
# are informational. gemini.py enforces RPD (per key, per model) and RPM (throttle).
@dataclass(frozen=True)
class ModelSpec:
    name: str                          # friendly label for logs
    model_id: str                      # the API model string
    daily_limit: int                   # RPD ceiling enforced locally (per key, per model)
    rpm: int                           # requests/minute ceiling (drives the throttle)
    ctx_in: int                        # input token window  (informational)
    ctx_out: int                       # output token window (informational)
    supports_thinking: bool            # thinking_config(thinking_level=…)
    supports_system_instruction: bool  # config.system_instruction
    supports_response_schema: bool     # response_mime_type=json + response_schema
    supports_media_resolution: bool    # media_resolution=…

    @property
    def throttle_seconds(self) -> int:
        """Minimum spacing between calls that keeps us at/under this model's RPM."""
        return math.ceil(60 / self.rpm)


# Tier 1 → 2 → 3. The two Gemini models are full-capability multimodal models.
# Gemma is the last-resort, high-volume tier: conservatively flagged as NOT supporting
# thinking / system instructions / structured output / media-resolution, because Gemma
# on the Gemini API historically rejects those config fields. gemini.py adapts each
# request to the active model's capabilities (folding a system instruction into the
# prompt, or asking for JSON in plain text when schemas aren't available). If you
# confirm Gemma accepts any of these, flip the flag here — no other code changes needed.
_DEFAULT_CHAIN = [
    ModelSpec("Gemini 3.6 Flash", "gemini-3.6-flash",
              daily_limit=20, rpm=5, ctx_in=1_048_576, ctx_out=65_536,
              supports_thinking=True, supports_system_instruction=True,
              supports_response_schema=True, supports_media_resolution=True),
    ModelSpec("Gemini 3.5 Flash", "gemini-3.5-flash",
              daily_limit=20, rpm=5, ctx_in=1_048_576, ctx_out=65_536,
              supports_thinking=True, supports_system_instruction=True,
              supports_response_schema=True, supports_media_resolution=True),
    ModelSpec("Gemma 4 31B", "gemma-2-27b-it",
              daily_limit=14400, rpm=30, ctx_in=262_144, ctx_out=32_768,
              supports_thinking=False, supports_system_instruction=False,
              supports_response_schema=False, supports_media_resolution=False),
]

# Escape hatch: GEMINI_MODEL pins the chain to a single model id (parity with the old
# single-model behavior). A known id reuses its spec; an unknown id assumes full
# Gemini capabilities.
_PIN = os.getenv("GEMINI_MODEL", "").strip()
if _PIN:
    _match = next((m for m in _DEFAULT_CHAIN if m.model_id == _PIN), None)
    MODEL_CHAIN = [_match] if _match else [
        ModelSpec(_PIN, _PIN, daily_limit=20, rpm=5, ctx_in=1_048_576, ctx_out=65_536,
                  supports_thinking=True, supports_system_instruction=True,
                  supports_response_schema=True, supports_media_resolution=True)
    ]
else:
    MODEL_CHAIN = list(_DEFAULT_CHAIN)

# Per-model attempts on rate/transient errors before falling back to the next model
# ("switch after 2-3 attempts"). A per-day quota (daily 429) switches immediately.
MAX_MODEL_ATTEMPTS = int(os.getenv("GEMINI_MAX_ATTEMPTS", "3"))
MAX_CORRECTION_LOOPS = 3   # compiler self-correction attempts after the first compile

# --- Rendering / model knobs (max accuracy; applied only when the model supports them) ---
DPI = 300
MEDIA_RESOLUTION = os.getenv("GEMINI_MEDIA_RESOLUTION", "MEDIA_RESOLUTION_HIGH")
THINKING_LEVEL = os.getenv("GEMINI_THINKING_LEVEL", "HIGH")  # HIGH is the ceiling for the Flash models

# import name -> pip name, used by preflight to bootstrap a bare environment
REQUIRED_PACKAGES = {
    "google.genai": "google-genai",
    "pdf2image": "pdf2image",
    "PIL": "pillow",
    "pypdf": "pypdf",
}


def ensure_dirs() -> None:
    """Create the pipeline directories if they do not already exist."""
    for d in (INPUT_DIR, OUTPUT_DIR, CACHE_DIR, IMAGES_DIR):
        d.mkdir(parents=True, exist_ok=True)
