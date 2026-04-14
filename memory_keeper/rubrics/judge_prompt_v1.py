"""memory_keeper.rubrics.judge_prompt_v1 — Judge prompt and few-shot examples.

Encodes dreamtime-audit-rubric-v1 §1-3 as a system prompt + few-shot block.
Used by proposition.proposition_audit() as the core LLM evaluation prompt.
"""
from __future__ import annotations

import json
from pathlib import Path

__all__ = ["JUDGE_SYSTEM_PROMPT", "FEW_SHOT_EXAMPLES", "load_seed_samples"]


JUDGE_SYSTEM_PROMPT = """你是 dreamtime 记忆审计专家。任务：从对话片段中提取值得长期记忆的原子命题，并对每条命题做五维评分。

## §1 命题拆解规则

每条命题必须满足：
- **独立可判断**：不依赖当前对话上下文也能理解
- **单一主张**：不包含 A 且 B 的复合陈述
- **可溯源**：能指回对话的具体位置

命题类型（type 字段）：
| type | 含义 | 例子 |
|------|------|------|
| preference | 用户偏好/风格选择 | "用 Redis Streams 别用 Kafka" |
| decision | 本次 session 做的技术决策 | "P_risk multiplier 设为 0.7" |
| fact | 关于项目/环境的事实 | "SUPER 机器 SSH 必须跳转" |
| correction | 用户纠错（最强信号） | "不要用 pip install -e . 覆盖 CUDA torch" |
| pattern | 跨 session 反复出现的行为模式 | "用户总是先问架构再写代码" |
| gotcha | 踩坑记录 | "Bash 里 ! 在密码中需要转义" |

**不提取**：
- 纯代码输出（代码在 git 里）
- 临时指令（"先跑这个测试"）
- 寒暄/确认语气词（"好的"、"收到"、"明白"）
- 已在去重基线中的规则（检查「已有规则/记忆」部分）

## §2 Support Status 判定

| status | 判定标准 |
|--------|---------|
| supported | 用户明确说过，或代码 diff 可验证 |
| inferred | 从用户行为推断（接受方案、不反对） |
| contradicted | 与同 session 其他命题矛盾 |
| stale | 与已有记忆冲突（用户可能改主意了） |
| unsupported | 无法追溯到对话内容 |

**contradicted 和 unsupported 的命题直接丢弃，不输出。**

## §3 五维评分（每维 0-2 分，总分 0-10）

### durability（跨 session 持久性）
- 0: 纯当次上下文（"先跑这个测试"）
- 1: 可能有用但不确定
- 2: 明确跨 session（"所有 Bash 命令加 rtk 前缀"）

### novelty（唯一性）
- 0: 已在去重基线中
- 1: 相关但有新信息（已知偏好的新理由）
- 2: 全新信息

### actionability（可操作性）
- 0: 纯观察/背景（"用户今天心情不错"）
- 1: 间接有用（"用户在做量化项目"→影响建议方向）
- 2: 直接可执行（"禁止在 Worker 层用贵模型"）

### confidence（确定性）
- 0: 单次隐含行为，可能偶然
- 1: 明确说过一次，或多次隐含
- 2: 多次明确说过，或纠错过（最强信号）

### scope（影响范围）
- 0: 仅限当前文件/函数
- 1: 项目级
- 2: 跨项目通用

**总分判定**：
- ≥5: action = "write"
- 3-4: action = "pending"（需人审）
- ≤2: action = "discard"（不输出）"""


def load_seed_samples() -> list[dict]:
    """Load built-in seed samples from seed_samples.json."""
    path = Path(__file__).parent / "seed_samples.json"
    if not path.exists():
        return []
    return json.loads(path.read_text(encoding="utf-8"))


def _build_few_shot() -> str:
    """Build few-shot examples: 4 positive + 1 discard (negative) for balanced judge."""
    import random

    samples = load_seed_samples()
    if not samples:
        return ""

    # Split into positive and discard samples
    positive = [s for s in samples if s.get("expected", {}).get("action") != "discard"]
    negative = [s for s in samples if s.get("expected", {}).get("action") == "discard"]

    # Shuffle positive with fixed seed for reproducibility, take 4
    rng = random.Random(42)
    rng.shuffle(positive)
    selected_positive = positive[:4]

    lines = ["## Few-shot 示例\n"]

    # Positive examples
    for i, sample in enumerate(selected_positive):
        exp = sample.get("expected", {})
        prop = exp.get("proposition", "")
        if not prop:
            continue
        scores = exp.get("scores", {})
        total = exp.get("total_score", 0)
        action = exp.get("action", "pending")

        lines.append(f"### 示例 {i + 1}（{exp.get('type', '?')} / {action}）")
        lines.append(f"**输入条目**：{sample['input'][:200]}")
        lines.append(f"**命题**：{prop}")
        lines.append(f"**type**: {exp.get('type')} | **support_status**: {exp.get('support_status')}")
        lines.append(f"**scores**: durability={scores.get('durability')} novelty={scores.get('novelty')} actionability={scores.get('actionability')} confidence={scores.get('confidence')} scope={scores.get('scope')} → total={total} → **{action}**")
        lines.append(f"**注**：{sample['notes']}\n")

    # Negative example (discard) — critical for preventing over-extraction
    if negative:
        neg = negative[0]
        lines.append(f"### 示例（应丢弃 — 不输出任何命题）")
        lines.append(f"**输入条目**：{neg['input']}")
        lines.append(f"**输出**：`[]`（空数组，不提取任何命题）")
        lines.append(f"**原因**：{neg['notes']}\n")

    return "\n".join(lines)


# Build at import time (cached)
FEW_SHOT_EXAMPLES: str = _build_few_shot()
