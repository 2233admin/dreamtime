"""memory_keeper.preference — Preference classification → pending-rules.md candidates.

Takes the `preferences` list extracted by distill_sessions, classifies each as
accept/reject/neutral via LLM, and appends rule candidates to ~/.omc/pending-rules.md.

Public API:
    run_preference(preferences) -> dict[str, int]
"""
from __future__ import annotations

import json
from pathlib import Path

import memory_keeper._config as _cfg_mod
from memory_keeper._config import PENDING_RULES
from memory_keeper._utils import _SeenHashes, _atomic_write, llm, llm_json

BATCH_SIZE = 10
_SEEN_PATH = Path.home() / ".claude" / "memory-store" / ".cursor" / "pref-seen.json"

_SYSTEM = (
    "Classify each item into exactly one label: accept, reject, or neutral.\n"
    "accept = user explicitly approved/confirmed assistant behavior ('yes exactly', 'perfect', '对', '就这样').\n"
    "reject = user corrected/refused/stopped assistant behavior ('不要', '别', '停', '不对', 'no not that').\n"
    "neutral = informational, ambiguous, or not a reusable behavior rule.\n"
    'Return JSON list in same order: [{"id": 1, "label": "accept|reject|neutral"}]'
)

__all__ = ["run_preference"]


def _chunks(items: list[str], size: int) -> list[list[str]]:
    return [items[i : i + size] for i in range(0, len(items), size)]


def _labels_from_result(raw: dict | list, expected: int) -> list[str]:
    rows = raw if isinstance(raw, list) else []
    if isinstance(raw, dict):
        for key in ("items", "results", "classifications", "data"):
            if isinstance(raw.get(key), list):
                rows = raw[key]
                break
    labels: dict[int, str] = {}
    for idx, row in enumerate(rows):
        if not isinstance(row, dict):
            continue
        pos = row.get("id") or row.get("index") or (idx + 1)
        label = str(row.get("label") or row.get("type") or row.get("classification") or "").strip().lower()
        if isinstance(pos, int) and 1 <= pos <= expected:
            labels[pos - 1] = label if label in {"accept", "reject", "neutral"} else "neutral"
    return [labels.get(i, "neutral") for i in range(expected)]


def _classify_batch(batch: list[str]) -> list[str]:
    payload = [{"id": i + 1, "text": text} for i, text in enumerate(batch)]
    prompt = _SYSTEM + "\n\nClassify these preference strings:\n" + json.dumps(payload, ensure_ascii=False, indent=2)
    raw_text = llm(prompt=prompt, max_tokens=800)
    # llm_json only handles dicts; LLM returns a JSON array — parse manually
    raw: dict | list | None = None
    text = raw_text.strip()
    # Try array first, fall back to dict
    for start_char, find_char in (("[", "["), ("{", "{")):
        idx = text.find(find_char)
        if idx != -1:
            try:
                obj, _ = json.JSONDecoder().raw_decode(text, idx)
                raw = obj
                break
            except Exception:
                continue
    return _labels_from_result(raw, len(batch))


def run_preference(preferences: list[str]) -> dict[str, int]:
    """Classify preferences and append accept/reject candidates to pending-rules.md.

    Args:
        preferences: Raw preference strings from distill_sessions().

    Returns:
        dict with keys: classified, candidates, appended.
    """
    cleaned = [" ".join(str(p).split()).strip() for p in preferences]
    cleaned = [t for t in cleaned if t]
    if not cleaned:
        return {"classified": 0, "candidates": 0, "appended": 0}

    seen = _SeenHashes(_SEEN_PATH)
    existing: set[str] = set()
    if PENDING_RULES.exists():
        existing = {
            line.strip()
            for line in PENDING_RULES.read_text(encoding="utf-8").splitlines()
            if line.strip().startswith("- [")
        }

    candidates: list[str] = []
    emitted: set[str] = set()
    for batch in _chunks(cleaned, BATCH_SIZE):
        try:
            labels = _classify_batch(batch)
        except Exception as exc:
            print(f"  [preference] batch classify failed: {exc}")
            labels = ["neutral"] * len(batch)
        for text, label in zip(batch, labels):
            if label not in {"accept", "reject"}:
                continue
            candidate = f"- [{label}] {text}"
            if candidate not in emitted:
                emitted.add(candidate)
                candidates.append(candidate)

    fresh = [line for line in candidates if line not in existing and seen.add(line)]
    appended = 0
    if fresh and not _cfg_mod.DRY_RUN:
        PENDING_RULES.parent.mkdir(parents=True, exist_ok=True)
        current = PENDING_RULES.read_text(encoding="utf-8") if PENDING_RULES.exists() else ""
        sep = "" if not current or current.endswith("\n") else "\n"
        _atomic_write(PENDING_RULES, current + sep + "\n".join(fresh) + "\n")
        seen.save()
        appended = len(fresh)

    print(f"  [preference] {len(cleaned)} classified, {len(candidates)} candidates, {appended} appended")
    return {"classified": len(cleaned), "candidates": len(candidates), "appended": appended}
