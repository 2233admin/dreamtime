"""memory_keeper.proposition — Proposition dataclass and audit pipeline.

Public API:
    Proposition            — atomic memory proposition with 5-dim scoring
    proposition_audit()    — full audit pipeline: decompose → score → pending
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

import memory_keeper._config as _cfg_mod
from memory_keeper._utils import llm_json
from memory_keeper.pending import append_pending, load_pending

__all__ = ["Proposition", "proposition_audit"]

SupportStatus = Literal["supported", "inferred", "contradicted", "stale", "unsupported"]
PropType = Literal["preference", "decision", "fact", "correction", "pattern", "gotcha"]
ActionType = Literal["write", "pending", "discard"]
ReviewStatus = Literal["pending", "approved", "rejected", "auto_approved", "consumed"]


@dataclass
class Proposition:
    content: str
    type: PropType
    support_status: SupportStatus
    scores: dict[str, int]        # durability/novelty/actionability/confidence/scope
    total_score: int
    action: ActionType
    source_session: str
    source_excerpt: str
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    status: ReviewStatus = "pending"
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Proposition":
        filtered = {k: v for k, v in d.items() if k in cls.__dataclass_fields__}
        # Basic validation: scores must be dict with expected keys
        scores = filtered.get("scores")
        if not isinstance(scores, dict):
            filtered["scores"] = {k: 0 for k in ("durability", "novelty", "actionability", "confidence", "scope")}
        # type must be valid
        valid_types = {"preference", "decision", "fact", "correction", "pattern", "gotcha"}
        if filtered.get("type") not in valid_types:
            filtered["type"] = "fact"
        return cls(**filtered)


# ── Single-scan context loader ────────────────────────────────────────────────

def _load_audit_context() -> tuple[str, bool]:
    """Load dedup baseline and bootstrap status in one pass.

    Returns:
        (dedup_baseline_text, is_bootstrap_mode)
    Reads pending.jsonl once and buckets by status, avoiding 3x full scans.
    """
    parts: list[str] = []

    # Claude Code auto-memory — limit to 20 most-recent files
    mem_dir: Path = _cfg_mod.MEMORY_DIR
    md_files = sorted(mem_dir.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)[:20]
    for md in md_files:
        try:
            text = md.read_text(encoding="utf-8", errors="replace")
            if text.strip():
                parts.append(f"[auto-memory: {md.name}]\n{text[:400]}")
        except OSError:
            pass

    # CLAUDE.md feedback-graduated rules
    graduated = Path.home() / ".claude" / "rules" / "common" / "feedback-graduated.md"
    if graduated.exists():
        parts.append(f"[feedback-graduated]\n{graduated.read_text(encoding='utf-8')[:600]}")

    # Single scan of pending.jsonl — bucket by status
    all_pending = load_pending()  # one read
    calibrated_count = sum(1 for p in all_pending if p.status in ("approved", "consumed"))
    bootstrap = calibrated_count < 10

    if all_pending:
        snippets = [f"- {p.content}" for p in all_pending[:30]]
        parts.append("[already-pending propositions]\n" + "\n".join(snippets))

    baseline = "\n\n---\n\n".join(parts)[:3000]
    return baseline, bootstrap


# ── Judge prompt ──────────────────────────────────────────────────────────────

def _build_judge_prompt(
    distill_items: list[str],
    dedup_baseline: str,
    session_id: str,
) -> str:
    """Build judge prompt that scores pre-extracted items (not raw conversation).

    This takes distill output (decisions/gotchas/preferences already extracted
    by _distill_one) and asks the judge to score + classify them, avoiding
    duplicate LLM extraction from raw conversation.
    """
    try:
        from memory_keeper.rubrics.judge_prompt_v1 import JUDGE_SYSTEM_PROMPT, FEW_SHOT_EXAMPLES
        few_shot = FEW_SHOT_EXAMPLES
        system_part = JUDGE_SYSTEM_PROMPT
    except ImportError:
        few_shot = ""
        system_part = _FALLBACK_SYSTEM

    items_block = "\n".join(f"<item>{item}</item>" for item in distill_items)

    return f"""{system_part}

<dedup_baseline>
{dedup_baseline}
</dedup_baseline>

{few_shot}

## 待评分条目（session: {session_id}）
以下条目已从对话中提取，你的任务是对每条做分类和五维评分，不需要重新从对话提取。

<items>
{items_block}
</items>

## 输出要求
返回 JSON 数组，对每条待评分条目输出一个对象：
```json
[
  {{
    "content": "命题文本（保持原意，可微调为独立可判断的表述）",
    "type": "preference|decision|fact|correction|pattern|gotcha",
    "support_status": "supported|inferred",
    "scores": {{
      "durability": 0,
      "novelty": 0,
      "actionability": 0,
      "confidence": 0,
      "scope": 0
    }},
    "source_excerpt": "复述该条目的核心内容（≤100字）"
  }}
]
```
只输出 JSON，不输出解释。已在去重基线中的条目，novelty 打 0。总分 ≤2 的条目不要包含在输出中。
"""


_FALLBACK_SYSTEM = """你是一个记忆审计专家。从对话中提取值得长期记忆的原子命题，并对每条命题评分。

## 命题类型
- preference: 用户偏好/风格选择
- decision: 本次session做的技术决策
- fact: 关于项目/环境的事实
- correction: 用户纠错
- pattern: 跨session反复出现的行为模式
- gotcha: 踩坑记录

## 不提取的内容
- 纯代码输出（代码在git里）
- 寒暄/确认语气词（"好的"、"收到"）
- 已在规则库中的内容

## 五维评分（每维0-2分）
- durability: 下次session还有用？0=纯当次上下文, 1=可能有用, 2=明确跨session
- novelty: 已被记录过？0=已有, 1=有新信息, 2=全新
- actionability: 能改变Claude行为？0=纯观察, 1=间接有用, 2=直接可执行
- confidence: 确定是用户真实意图？0=单次隐含, 1=明确一次/多次隐含, 2=多次明确/纠错
- scope: 适用范围？0=仅当前文件, 1=项目级, 2=跨项目
"""


# ── Main audit pipeline ───────────────────────────────────────────────────────

def proposition_audit(
    distill_items: list[str],
    session_id: str,
) -> list[Proposition]:
    """Score pre-extracted distill items via judge prompt → route to pending store.

    Takes the output of _distill_one() (decisions + gotchas + preferences) and
    runs them through the rubric judge for classification and 5-dim scoring.
    Does NOT re-extract from raw conversation — avoids doubling LLM calls.

    Args:
        distill_items: Pre-extracted items from distill (decisions/gotchas/preferences)
        session_id: Source session filename for traceability

    Returns:
        List of Proposition objects that were processed (write + pending; discards excluded)
    """
    if not distill_items:
        return []

    dedup_baseline, bootstrap = _load_audit_context()
    prompt = _build_judge_prompt(distill_items, dedup_baseline, session_id)

    raw = llm_json(prompt, max_tokens=2000)
    if not raw:
        print(f"    [proposition] input={len(distill_items)} judge_returned=0 (LLM returned nothing)")
        return []

    # llm_json now supports list return
    items: list[dict] = raw if isinstance(raw, list) else raw.get("propositions", [])
    if not items:
        print(f"    [proposition] input={len(distill_items)} judge_returned=0 (empty parse)")
        return []
    results: list[Proposition] = []
    discarded = 0

    for item in items:
        scores: dict[str, int] = item.get("scores", {})
        total = sum(scores.get(k, 0) for k in ("durability", "novelty", "actionability", "confidence", "scope"))

        # Determine action from score
        if total >= 5:
            action: ActionType = "write"
        elif total >= 3:
            action = "pending"
        else:
            action = "discard"

        # Bootstrap: force all to pending (no auto-approve)
        if bootstrap and action == "write":
            action = "pending"

        if action == "discard":
            discarded += 1
            continue  # Don't store discards

        review_status: ReviewStatus = "auto_approved" if action == "write" and not bootstrap else "pending"

        prop = Proposition(
            content=item.get("content", ""),
            type=item.get("type", "fact"),
            support_status=item.get("support_status", "inferred"),
            scores=scores,
            total_score=total,
            action=action,
            source_session=session_id,
            source_excerpt=item.get("source_excerpt", "")[:200],
            status=review_status,
        )

        if not prop.content.strip():
            discarded += 1
            continue

        results.append(prop)

    if results:
        append_pending(results)

    print(f"    [proposition] input={len(distill_items)} judge_returned={len(items)} kept={len(results)} discarded={discarded} bootstrap={bootstrap}")
    return results
