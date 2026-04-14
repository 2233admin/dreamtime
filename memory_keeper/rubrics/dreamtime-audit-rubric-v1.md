---
id: dreamtime-audit-rubric-v1
status: draft
version: 1.1
updated_at: 2026-03-31
upstream: OpenClaw investment-learning-audit-rubric-v1-draft (方法论借鉴)
---

# Dreamtime Audit Rubric V1

对话 JSONL → 原子命题 → 记忆写入 的审计规则。
判断"一条从对话中提取的命题，值不值得写进长期记忆"。

## 0. 系统定位与兼容边界

### 与 Claude Code auto-memory 的关系

Claude Code 内置 auto-memory 在**对话进行中**实时写 `~/.claude/projects/*/memory/`。
Dreamtime 在 **session 结束后**做第二遍扫描，定位是：

1. **拾遗** — auto-memory 漏掉的隐含偏好和行为模式
2. **校准** — 审计 auto-memory 已写入内容的质量
3. **跨 session 模式识别** — auto-memory 只看单次对话，dreamtime 看积累

**兼容规则**：
- Dreamtime **不写** Claude Code 原生文件（CLAUDE.md / rules/ / MEMORY.md）
- Dreamtime 扫描时**先读** auto-memory 本次已写入的内容作为 dedup 基线
- 需要升级到 CLAUDE.md 或 feedback-graduated.md 时，走 notify → 人审 → 人手动写入

### 双适配器架构

Dreamtime 输出通过两类适配器解耦，用户按需配置：

**记忆存储适配器**（写到哪）：

| adapter | 依赖 | 说明 |
|---------|------|------|
| `ClaudeMemoryAdapter` | 零依赖（默认） | 写 `~/.claude/memory-store/dreamtime/` |
| `Mem0Adapter` | mem0 | 开源记忆层 |
| `MemOSAdapter` | MemOS | |
| `MemUAdapter` | memU (PG + bge-m3) | 图记忆 |
| 自定义 | 实现 `MemoryAdapter` 协议 | |

**通知渠道适配器**（告诉谁）：

| adapter | 依赖 | 说明 |
|---------|------|------|
| `FileAdapter` | 零依赖（默认） | 写 markdown 到本地 |
| `FeishuAdapter` | 飞书 webhook | 机器人推送 |
| `DiscordAdapter` | DC webhook | |
| `WeChatAdapter` | 企业微信 API | |
| `QQAdapter` | QQ 机器人 | |
| `SlackAdapter` | Slack webhook | |
| 自定义 | 实现 `NotifyAdapter` 协议 | |

**协议接口**：

```python
class MemoryAdapter(Protocol):
    def write(self, proposition: Proposition) -> bool: ...
    def query(self, text: str, top_k: int = 5) -> list[Proposition]: ...
    def exists(self, proposition: Proposition) -> bool: ...  # 去重

class NotifyAdapter(Protocol):
    def send_review(self, items: list[Proposition]) -> bool: ...  # pending-confirm
    def send_digest(self, digest: DreamDigest) -> bool: ...       # 每日摘要
```

**配置**（config.yaml）：
```yaml
memory_backend: claude          # claude / mem0 / memos / memu
notify_channels:
  - type: file                  # 默认
  - type: discord
    webhook: $DISCORD_WEBHOOK
  - type: feishu
    webhook: $FEISHU_WEBHOOK
```

### Bootstrap 协议（冷启动）

```
全裸安装（无历史 session）
    │
    ▼
seed_samples（内置 10 条 canonical 样本）→ judge few-shot 冷启动
    │
    ▼
前 5 次 session → judge 全标 pending-confirm（不自动写入）
    │
    ▼
用户审了 ≥ 10 条 → 校准集建立 → judge 开始自主写入
    │
    ▼
正常运转
```

**有历史但无校准集**：judge 先跑 JSONL，挑边界案例（分数 3-5）给人审。
**有 CLAUDE.md 但无 session**：从已有规则反向生成合成样本。
**关键原则**：没校准集之前，judge 只推荐不写入。

## 1. Proposition Decomposition 规则

### 输入
一轮对话（user + assistant，可能含 tool_use / tool_result）。

### 拆解目标
拆成 **atomic propositions**，每条满足：
- 独立可判断（不依赖上下文也能理解）
- 单一主张（不是 A 且 B）
- 可溯源（能指回原对话的具体位置）

### 命题类型

| type | 含义 | 例子 |
|------|------|------|
| `preference` | 用户偏好/风格选择 | "用 Redis Streams 别用 Kafka" |
| `decision` | 本次 session 做的技术决策 | "P_risk multiplier 设为 0.7" |
| `fact` | 关于项目/环境的事实 | "SUPER 机器 SSH 必须跳转" |
| `correction` | 用户纠错 | "不要用 pip install -e . 覆盖 CUDA torch" |
| `pattern` | 跨 session 反复出现的行为模式 | "用户总是先问架构再写代码" |
| `gotcha` | 踩坑记录 | "Bash 里 ! 在密码中需要转义" |

### 不拆的内容
- 纯代码输出（代码本身在 git 里，不需要记忆）
- tool_use 的原始参数（噪音）
- 寒暄/确认语气词（"好的"、"收到"）
- 已经在 CLAUDE.md / feedback-graduated.md 里的规则

## 2. Support Status 判定

每条命题必须标注来源支撑等级：

| status | 判定标准 | 写入策略 |
|--------|---------|---------|
| `supported` | 用户明确说过，或代码 diff 可验证 | 直接写入 |
| `inferred` | 从用户行为推断（接受方案、不反对） | 写入但标 `confidence: inferred` |
| `contradicted` | 与同 session 其他命题矛盾 | 丢弃，记录冲突 |
| `stale` | 与已有记忆冲突（用户可能改主意了） | 进 pending-confirm 队列 |
| `unsupported` | 无法追溯到对话内容 | 丢弃 |

## 3. Worth-Remembering 判定 (核心 rubric)

通过 support check 的命题，再过以下五个维度。
每个维度 0-2 分，总分 ≥ 5 才写入，3-4 进 pending-confirm。

### 3.1 跨 Session 持久性 (Durability)

> 这条信息下次 session 还有用吗？

| 分数 | 标准 |
|------|------|
| 0 | 纯当次上下文（"先跑这个测试"） |
| 1 | 可能有用但不确定（"这个 API 返回格式是 X"） |
| 2 | 明确跨 session（"所有 Bash 命令加 rtk 前缀"） |

### 3.2 唯一性 (Novelty)

> 这条信息已经被记录过了吗？

| 分数 | 标准 |
|------|------|
| 0 | 已在 feedback-graduated.md / CLAUDE.md / 现有记忆存储中 |
| 1 | 相关但有新信息（已知偏好的新理由） |
| 2 | 全新信息 |

### 3.3 可操作性 (Actionability)

> 这条信息能直接改变 Claude 下次的行为吗？

| 分数 | 标准 |
|------|------|
| 0 | 纯观察/背景（"用户今天心情不错"） |
| 1 | 间接有用（"用户在做量化项目"→影响建议方向） |
| 2 | 直接可执行（"禁止在 Worker 层用贵模型"） |

### 3.4 确定性 (Confidence)

> 我们多确定这是用户的真实意图？

| 分数 | 标准 |
|------|------|
| 0 | 单次隐含行为，可能是偶然 |
| 1 | 明确说过一次，或多次隐含 |
| 2 | 多次明确说过，或纠错过（最强信号） |

### 3.5 影响范围 (Scope)

> 这条适用于一个项目还是所有项目？

| 分数 | 标准 |
|------|------|
| 0 | 仅限当前文件/函数 |
| 1 | 项目级 |
| 2 | 跨项目通用 |

### 评分汇总

| 总分 | 动作 |
|------|------|
| 8-10 | 写入记忆存储 + 考虑升级到 feedback-graduated.md |
| 5-7 | 写入记忆存储 |
| 3-4 | 进 pending-confirm 队列，等人审 |
| 0-2 | 丢弃 |

## 4. 去重规则

写入前必须检查：

1. **精确匹配** — 记忆存储中已有语义相同的条目 → 跳过
2. **更新匹配** — 已有条目但新命题包含更新信息 → 更新旧条目
3. **矛盾匹配** — 已有条目但新命题相反 → 进 pending-confirm，标注冲突
4. **互补匹配** — 已有条目 + 新命题可合并为更完整的知识 → 合并

## 5. Judge Prompt 校准

### 校准集结构

每条校准样本：
```json
{
  "input": "对话片段原文",
  "proposition": "拆解出的命题",
  "expected_support": "supported|inferred|...",
  "expected_scores": {"durability": 2, "novelty": 1, ...},
  "expected_action": "write|pending|discard",
  "notes": "为什么这样判"
}
```

### 校准流程

1. 人工标注 ≥ 20 条样本（从历史 session 选有代表性的）
2. 跑 judge prompt，对比 judge 输出 vs 人工标注
3. 不一致率 > 20% → 调 prompt，重跑
4. 每次 rubric 改版，重跑全量校准集回归

### 当前校准集状态

- [ ] 首批 20 条待标注（从最近 7 天 session 选取）
- [ ] judge prompt v1 待编写
- [ ] baseline 准确率待测量

## 6. 与现有代码的接口

| 组件 | 文件 | rubric 接入点 |
|------|------|-------------|
| 对话提取 | `training.py` | 当前只做 quality_score 粗筛，需加 proposition_decompose() |
| 偏好分类 | `preference.py` | 已有 accept/reject/neutral，对应 rubric §3.4 confidence |
| 梦境 hook | `dreamtime/hook.py` | distill 输出接 rubric 评分，再决定写 inbox 还是记忆存储 |
| 去重 | `dedup.py` | 已有去重逻辑，需对齐 §4 的四种匹配模式 |
| 适配器 | `adapters/` | 已有目录，需实现 MemoryAdapter + NotifyAdapter 协议 |

## 7. 版本演进计划

- **V1 (当前)**: judge prompt + 五维评分 + ClaudeMemoryAdapter + FileAdapter，pending-confirm 人审
- **V1.5**: bootstrap 协议 + 内置 seed 样本 + 首批通知适配器（Discord/飞书）
- **V2**: 校准集 ≥ 50 条后，自动化 judge eval pipeline + Mem0/MemOS 适配器
- **V3**: pairwise 比较取代单点评分 + 跨 session 模式识别
