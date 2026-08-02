"""The single chokepoint for every Gemini API interaction.

Centralizing all access here makes the free-tier guarantees enforceable in one
place. The headline feature is a three-tier model FALLBACK CHAIN (config.MODEL_CHAIN):

    Gemini 3.5 Flash  →  Gemini 3 Flash  →  Gemma 4 31B

`generate()` tries each model in order. It moves to the next model when the active
one is unavailable (404/403), is rejected for that model (400), or hits its limit —
either our local per-key/per-model daily ledger says 0 left, or the server returns a
per-day 429, or a per-minute 429 persists after a few attempts. When every model in
the chain is exhausted it raises BudgetExceeded telling the user to wait ~24h for the
limits to reset or supply a fresh API key.

Each model is metered independently: the ledger keys requests by model id, and the
RPM throttle keeps its own per-model timer. Phase modules never touch the client
directly and never need to know which model answered.

The API key is injected at runtime via set_api_key() and is NEVER written to disk.
The ledger keys its counts by a one-way SHA-256 fingerprint of the key (16 hex chars)
— enough to tell keys apart and reset the budget for a fresh key, while storing
nothing from which the key could be recovered.
"""
from __future__ import annotations

import hashlib
import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from config import (
    MAX_MODEL_ATTEMPTS,
    MEDIA_RESOLUTION,
    MODEL_CHAIN,
    THINKING_LEVEL,
    USAGE_LEDGER,
    ModelSpec,
)

_client = None
_api_key: str | None = None
_api_keys: list[str] = []
_key_index: int = 0
_last_call_monotonic: dict[str, float] = {}  # per-model request spacing (RPM guard)
_active_model: ModelSpec | None = None        # the model that answered the last generate()

_SPECS_BY_ID = {m.model_id: m for m in MODEL_CHAIN}

# Transient server/rate conditions worth retrying within a model before falling back.
# A retry never spends daily budget — only a successful response is recorded.
_RETRYABLE_CODES = {429, 500, 502, 503, 504}
_RETRY_BACKOFFS = [15, 30, 60]  # seconds before successive retries of the same model


class BudgetExceeded(RuntimeError):
    """Raised only when EVERY model in the fallback chain is unavailable/exhausted."""


class _ModelUnavailable(Exception):
    """Internal: the active model can't serve this request — fall back to the next."""


def _status_of(exc: Exception) -> int | None:
    for attr in ("code", "status_code"):
        val = getattr(exc, attr, None)
        if isinstance(val, int):
            return val
    return None


def _msg(exc: Exception) -> str:
    return (getattr(exc, "message", None) or str(exc))[:200]


def _is_per_day_quota(exc: Exception) -> bool:
    """Best-effort: does a 429 name a per-DAY quota? (No point retrying those — the
    minute-window backoff can't recover a daily ceiling, so we switch models now.)"""
    text = (getattr(exc, "message", "") or str(exc)).lower()
    return any(tok in text for tok in ("perday", "per day", "per-day", "daily", "requests per day"))


def _with_retry(fn, label: str):
    """Call fn(), retrying transient 429/5xx errors with backoff. Used for uploads,
    which are model-agnostic (separate quota) and not part of the fallback chain."""
    from google.genai import errors as genai_errors

    attempts = len(_RETRY_BACKOFFS) + 1
    for i in range(attempts):
        try:
            return fn()
        except genai_errors.APIError as exc:
            code = _status_of(exc)
            if code in _RETRYABLE_CODES and i < attempts - 1:
                wait = _RETRY_BACKOFFS[min(i, len(_RETRY_BACKOFFS) - 1)]
                print(
                    f"  ⚠ {label}: transient {code} from Gemini — retrying in {wait}s "
                    f"(attempt {i + 1}/{attempts}) …"
                )
                time.sleep(wait)
                continue
            raise


def set_api_key(key: str) -> None:
    """Inject the key for this process only. Never persisted."""
    global _api_key, _client
    _api_key = (key or "").strip()
    _client = None  # force rebuild with the new key


def _fingerprint() -> str:
    """One-way, non-reversible tag for the active key (16 hex chars)."""
    if not _api_key:
        raise RuntimeError("No API key set. Call set_api_key() first.")
    return hashlib.sha256(_api_key.encode("utf-8")).hexdigest()[:16]


def get_client():
    global _client
    if _client is None:
        if not _api_key:
            raise RuntimeError("No API key set. Call set_api_key() first.")
        from google import genai

        _client = genai.Client(api_key=_api_key)
    return _client


# --- per-key, per-model rolling 24h request ledger -------------------------------
# On-disk shape:  { "<key_fp>": { "<model_id>": ["<iso8601>", …] } }
# An older flat shape ({ "<key_fp>": ["<iso8601>", …] }) is migrated on read to the
# primary model so previously-recorded usage is not silently forgotten.
def _load_ledger() -> dict[str, dict[str, list[str]]]:
    if not USAGE_LEDGER.exists():
        return {}
    try:
        data = json.loads(USAGE_LEDGER.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    if not isinstance(data, dict):
        return {}
    primary = MODEL_CHAIN[0].model_id
    out: dict[str, dict[str, list[str]]] = {}
    for fp, val in data.items():
        if isinstance(val, list):  # legacy flat format → attribute to the primary model
            out[fp] = {primary: [t for t in val if isinstance(t, str)]}
        elif isinstance(val, dict):
            out[fp] = {m: ts for m, ts in val.items() if isinstance(ts, list)}
    return out


def _recent(timestamps: list[str]) -> list[str]:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    out: list[str] = []
    for ts in timestamps:
        try:
            if datetime.fromisoformat(ts) >= cutoff:
                out.append(ts)
        except ValueError:
            continue
    return out


def requests_remaining(model_id: str) -> int:
    """Requests left in the last 24h for the ACTIVE key on `model_id`. A fresh key
    (fingerprint absent from the ledger) gets the model's full daily_limit."""
    spec = _SPECS_BY_ID.get(model_id)
    limit = spec.daily_limit if spec else 0
    ledger = _load_ledger()
    used = len(_recent(ledger.get(_fingerprint(), {}).get(model_id, [])))
    return max(0, limit - used)


def budget_summary() -> str:
    """One-line per-model remaining/limit summary for the active key (banner use)."""
    return "  ·  ".join(
        f"{m.name} {requests_remaining(m.model_id)}/{m.daily_limit}" for m in MODEL_CHAIN
    )


def active_model() -> str | None:
    """Friendly name of the model that answered the most recent generate(), if any."""
    return _active_model.name if _active_model else None


def _record_request(model_id: str) -> None:
    fp = _fingerprint()
    ledger = _load_ledger()
    per_model = ledger.setdefault(fp, {})
    per_model[model_id] = _recent(per_model.get(model_id, []))  # prune this model's old entries
    per_model[model_id].append(datetime.now(timezone.utc).isoformat())
    # Keep the ledger small: drop models/keys that have gone fully cold.
    for key in list(ledger.keys()):
        ledger[key] = {m: ts for m, ts in ledger[key].items() if _recent(ts)}
        if not ledger[key] and key != fp:
            del ledger[key]
    USAGE_LEDGER.parent.mkdir(parents=True, exist_ok=True)
    USAGE_LEDGER.write_text(json.dumps(ledger, indent=2), encoding="utf-8")


def _throttle(spec: ModelSpec) -> None:
    """Block until at least spec.throttle_seconds have elapsed since the previous call
    to THIS model, guaranteeing no more than spec.rpm requests/minute for it."""
    now = time.monotonic()
    last = _last_call_monotonic.get(spec.model_id)
    if last is not None:
        wait = spec.throttle_seconds - (now - last)
        if wait > 0:
            print(f"  ⏳ {spec.name}: throttling {wait:.0f}s to stay under {spec.rpm}/min …")
            time.sleep(wait)
    _last_call_monotonic[spec.model_id] = time.monotonic()


# --- API calls ------------------------------------------------------------------
def upload_pdf(path: Path):
    """Upload a source file via the Files API. Throttled (on the primary model's
    timer) for RPM, but NOT counted against any model's daily generateContent budget —
    uploads draw on a separate quota."""
    client = get_client()
    print(f"  ↑ uploading {path.name} via Files API …")
    _throttle(MODEL_CHAIN[0])
    return _with_retry(lambda: client.files.upload(file=str(path)), "upload")


def _build_config(spec: ModelSpec, json_schema: Any, system_instruction: str | None):
    """Assemble a GenerateContentConfig using only the knobs `spec` actually supports."""
    from google.genai import types

    kwargs: dict[str, Any] = {}
    if spec.supports_media_resolution and MEDIA_RESOLUTION:
        kwargs["media_resolution"] = MEDIA_RESOLUTION
    if spec.supports_thinking and THINKING_LEVEL:
        kwargs["thinking_config"] = types.ThinkingConfig(thinking_level=THINKING_LEVEL)
    if spec.supports_system_instruction and system_instruction:
        kwargs["system_instruction"] = system_instruction
    if spec.supports_response_schema and json_schema is not None:
        kwargs["response_mime_type"] = "application/json"
        kwargs["response_schema"] = json_schema
    return types.GenerateContentConfig(**kwargs)


def _adapt_contents(
    spec: ModelSpec, contents: Any, system_instruction: str | None, wants_json: bool
) -> Any:
    """Fold capabilities the model lacks back into the prompt text: a system
    instruction it can't take as config, and a JSON directive when it can't be given a
    response schema. Leaves `contents` untouched when no adaptation is needed."""
    prefix = ""
    if system_instruction and not spec.supports_system_instruction:
        prefix += system_instruction.strip() + "\n\n"
    if wants_json and not spec.supports_response_schema:
        prefix += (
            "Respond with ONLY a single valid JSON object — no markdown fences, "
            "no commentary before or after.\n\n"
        )
    if not prefix:
        return contents
    if isinstance(contents, str):
        return prefix + contents
    if isinstance(contents, list):
        return [prefix, *contents]  # leading text part; file handles stay in place
    return contents


def _attempt_model(
    spec: ModelSpec, contents: Any, json_schema: Any, system_instruction: str | None
):
    """Try one model up to MAX_MODEL_ATTEMPTS times. Returns the response on success;
    raises _ModelUnavailable to signal 'fall back to the next model'. A genuinely
    unexpected (non-API) error propagates."""
    from google.genai import errors as genai_errors

    client = get_client()
    config = _build_config(spec, json_schema, system_instruction)
    call_contents = _adapt_contents(spec, contents, system_instruction, json_schema is not None)

    for attempt in range(1, MAX_MODEL_ATTEMPTS + 1):
        remaining = requests_remaining(spec.model_id)
        if remaining <= 0:
            raise _ModelUnavailable("daily budget exhausted (local ledger)")

        print(
            f"  ⚡ {spec.name}: call ({remaining} of {spec.daily_limit} left today, "
            f"attempt {attempt}/{MAX_MODEL_ATTEMPTS}) …"
        )
        _throttle(spec)
        try:
            response = client.models.generate_content(
                model=spec.model_id, contents=call_contents, config=config
            )
            _record_request(spec.model_id)
            return response
        except genai_errors.APIError as exc:
            code = _status_of(exc)
            if code in (404, 403):  # model missing / no access for this key → switch now
                raise _ModelUnavailable(f"unavailable ({code}: {_msg(exc)})")
            if code == 400:  # request rejected for this model (e.g. unsupported config)
                raise _ModelUnavailable(f"request rejected ({code}: {_msg(exc)})")
            if code == 429 and _is_per_day_quota(exc):  # daily ceiling → no point retrying
                raise _ModelUnavailable(f"daily quota reached server-side ({_msg(exc)})")
            if code in _RETRYABLE_CODES and attempt < MAX_MODEL_ATTEMPTS:
                wait = _RETRY_BACKOFFS[min(attempt - 1, len(_RETRY_BACKOFFS) - 1)]
                print(f"  ⚠ {spec.name}: transient {code} — retrying in {wait}s …")
                time.sleep(wait)
                continue
            if code in _RETRYABLE_CODES:  # out of attempts on a rate/transient error
                raise _ModelUnavailable(
                    f"rate/limit error after {MAX_MODEL_ATTEMPTS} attempts ({code})"
                )
            raise  # unexpected API error — surface it rather than masking a real bug


def generate(contents: Any, *, json_schema: Any = None, system_instruction: str | None = None):
    """One logical generation, served by the first model in the chain that can.

    Walks config.MODEL_CHAIN: skips models whose local daily budget is spent, tries the
    rest (with per-model RPM throttle, capability-aware config, and up to
    MAX_MODEL_ATTEMPTS retries on transient/rate errors), and returns the first success.
    Only a success is recorded against the daily ledger.

    `contents` may be a string, or a list mixing an uploaded File handle and text.
    Pass `json_schema` (a types.Schema) to request strict-JSON output — it is enforced
    on models that support response schemas and degraded to a plain-text JSON directive
    on those that don't. `system_instruction` is sent as config where supported and
    folded into the prompt otherwise.

    Raises BudgetExceeded when no model in the chain can serve the request.
    """
    global _active_model

    failures: dict[str, str] = {}
    for spec in MODEL_CHAIN:
        if requests_remaining(spec.model_id) <= 0:
            print(f"  ⏭ {spec.name}: daily budget exhausted — trying next model.")
            failures[spec.name] = "daily budget exhausted (local ledger)"
            continue
        try:
            response = _attempt_model(spec, contents, json_schema, system_instruction)
        except _ModelUnavailable as exc:
            print(f"  ⏭ {spec.name}: {exc} — trying next model.")
            failures[spec.name] = str(exc)
            continue
        _active_model = spec
        if spec is not MODEL_CHAIN[0]:
            print(f"  ✓ served by fallback model: {spec.name}")
        return response

    raise BudgetExceeded(_all_exhausted_message(failures))


def _all_exhausted_message(failures: dict[str, str]) -> str:
    lines = [f"    - {name}: {reason}" for name, reason in failures.items()]
    detail = "\n".join(lines) if lines else "    (no models configured)"
    return (
        "Every model in the fallback chain is currently unavailable or out of budget:\n"
        f"{detail}\n"
        "  → Wait ~24 hours for your free-tier limits to reset, or set a NEW API key via "
        "the GEMINI_API_KEY environment variable and re-run.\n"
        "  Cached progress is preserved — completed steps will not be repeated."
    )
