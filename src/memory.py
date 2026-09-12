"""记忆层：让 200+ 回合的对话不失忆。

借鉴自 billion-context-pi 的四条核心机制
----------------------------------------
1. **由模型决定压缩范围**，而不是硬性截断。每条消息有 ``mNNNNN`` 引用，
   LLM 用引用指定要压缩的区间，并自己写摘要。
2. **三级压缩 T1 → T2 → T3**。摘要本身还能被蒸馏，保证长期有界。
3. **保护近期工作集**：最后 N 轮对话 + 最后一条用户消息永不压缩。
4. **旁挂文件原子落盘**（在 state.py 里实现）。

为角色扮演做的三项改造
----------------------
A. **剧情档案（StoryBible）** —— 压缩会丢掉语义，所以把关键信息用结构化
   字段显式保存：仪式、未兑现的承诺、她的旧事、关系里程碑。它永不压缩，
   永远注入。LLM 不需要「记得」，它只需要读表。
B. **风格锚点** —— 长对话后 LLM 会漂移（喵字减少、开始说教、开始像客服）。
   定期重新注入人格片段，并检测漂移指标。
C. **压缩摘要必须保留角色一致性信息** —— 不只是「发生了什么」，还有
   「她的语气变了吗」「关系走到哪了」。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from .state import GameState, MessageRef, StoryBible, SummaryBlock, estimate_tokens


# --------------------------------------------------------------------------
# 压缩决策
# --------------------------------------------------------------------------


@dataclass
class CompressDecision:
    needed: bool
    mode: str  # none | soft | force | emergency | tier2 | tier3
    reason: str
    compressible: list[tuple[str, str]] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.compressible is None:
            self.compressible = []


def protected_range(state: GameState, cfg: dict[str, Any]) -> tuple[int, int]:
    """返回受保护的回合区间 [start_turn, end_turn]。"""
    protect_turns = cfg["memory"]["recent_protect_turns"]
    return state.turn - protect_turns, state.turn


def compressible_messages(state: GameState, cfg: dict[str, Any]) -> list[MessageRef]:
    """可压缩的消息：排除受保护的近期区，排除系统消息。"""
    start, _ = protected_range(state, cfg)
    return [m for m in state.messages if m.turn < start and m.role != "system"]


def compressible_ranges(state: GameState, cfg: dict[str, Any]) -> list[tuple[str, str]]:
    """可压缩区间，以 mNNNNN 引用表示。"""
    msgs = compressible_messages(state, cfg)
    if len(msgs) < cfg["memory"]["compress_min_turns"] * 2:
        return []
    return [(msgs[0].id, msgs[-1].id)]


def decide(state: GameState, rules_cfg: dict[str, Any]) -> CompressDecision:
    """决定是否需要压缩。三级递进，与参考实现一致。"""
    mc = rules_cfg["memory"]
    window = mc["context_window"]
    used = state.window_tokens(mc["recent_protect_turns"])
    ratio = used / window if window else 0.0

    ranges = compressible_ranges(state, rules_cfg)

    # 三级：先看能否把 T1 蒸馏成 T2，再看 T3
    active_t1 = [b for b in state.blocks if b.active and b.tier == 1]
    active_t2 = [b for b in state.blocks if b.active and b.tier == 2]
    if len(active_t2) >= mc["tier3_trigger"]:
        return CompressDecision(True, "tier3", f"{len(active_t2)} 个 T2 块可以浓缩", [])
    if len(active_t1) >= mc["tier2_trigger"]:
        return CompressDecision(True, "tier2", f"{len(active_t1)} 个 T1 块可以蒸馏", [])

    if ratio >= mc["emergency_ratio"]:
        return CompressDecision(True, "emergency", f"上下文 {ratio:.0%} 超过紧急阈值", ranges)
    if ratio >= mc["force_nudge_ratio"]:
        return CompressDecision(True, "force", f"上下文 {ratio:.0%} 超过强制阈值", ranges)
    if ratio >= mc["soft_nudge_ratio"] and ranges:
        return CompressDecision(True, "soft", f"上下文 {ratio:.0%} 超过软阈值", ranges)
    return CompressDecision(False, "none", f"上下文 {ratio:.0%}")


def build_compress_prompt(state: GameState, decision: CompressDecision) -> str:
    """交给 LLM 的压缩指令。这是「记忆策略」的提示词部分。"""
    head = [
        "【记忆整理】",
        f"当前上下文占用偏高（{decision.reason}）。请把指定区间压缩成摘要。",
        "",
        "摘要必须保留（这些是压缩后唯一的留存）：",
        "  · 她说过的重要的话（原话优先于转述）",
        "  · 关系变化：称呼、距离、主动性、语气的变化",
        "  · 未兑现的承诺、约定好的仪式",
        "  · 她的偏好与雷区（她怕什么、什么不能碰）",
        "  · 玩家做过的事及其后果",
        "",
        "摘要可以丢弃：",
        "  · 寒暄、重复的日常、已被后续情节覆盖的临时状态",
        "",
        "摘要写法：第二人称、现在时、写「发生了什么」而不是「聊了天」。",
        "例：「主人答应周三带她去买小鱼干，她嘴上说不要但当天早起等了很久。」",
    ]
    if decision.mode in ("tier2", "tier3"):
        head += ["", "本次是蒸馏：只保留决策、结果与关系变化，丢弃过程细节。"]
    if decision.compressible:
        head += ["", "可压缩区间：" + ", ".join(f"{a}..{b}" for a, b in decision.compressible)]
    head += [
        "",
        '只输出 JSON：{"start": "m00005", "end": "m00012", "topic": "三到五个字的主题", "summary": "摘要正文"}',
    ]
    return "\n".join(head)


# --------------------------------------------------------------------------
# 应用压缩
# --------------------------------------------------------------------------


def apply_compression(
    state: GameState,
    start_id: str,
    end_id: str,
    topic: str,
    summary: str,
    cfg: dict[str, Any],
    tier: int = 1,
) -> SummaryBlock:
    """把 [start_id, end_id] 区间的消息替换成一个摘要块。"""
    idx_start = _index_of(state, start_id)
    idx_end = _index_of(state, end_id)
    if idx_start is None or idx_end is None or idx_end < idx_start:
        raise ValueError(f"无效压缩区间 {start_id}..{end_id}")

    covered = state.messages[idx_start : idx_end + 1]
    block = SummaryBlock(
        block_id=state.new_block_id(),
        tier=tier,
        topic=topic,
        summary=summary,
        start_turn=covered[0].turn,
        end_turn=covered[-1].turn,
    )

    # 被压缩的消息从活跃列表移除（但保留在存档里，供 decompress / search 使用）
    del state.messages[idx_start : idx_end + 1]
    state.blocks.append(block)
    return block


def _index_of(state: GameState, ref: str) -> int | None:
    for i, m in enumerate(state.messages):
        if m.id == ref:
            return i
    return None


def search_blocks(state: GameState, keyword: str, limit: int = 5) -> list[SummaryBlock]:
    """在已压缩块中搜索。对应参考实现的 search_context —— 不解压即可检索。"""
    hits: list[tuple[int, SummaryBlock]] = []
    for b in state.blocks:
        score = b.summary.count(keyword) * 2 + b.topic.count(keyword)
        if score > 0:
            hits.append((score, b))
    hits.sort(key=lambda x: -x[0])
    return [b for _, b in hits[:limit]]


# --------------------------------------------------------------------------
# 剧情档案更新
# --------------------------------------------------------------------------


def merge_bible(state: GameState, delta: dict[str, Any]) -> None:
    """把 LLM 提取出的剧情增量合并进档案。去重、限量，防止无限膨胀。

    支持 ``facts_remove``：档案不能只增不减，否则早期的重要事实
    （「她怕打雷」）会被后来的琐事挤出去。让 LLM 在整理记忆时
    显式声明哪些旧事实已经不再成立，比单纯截断靠谱得多。
    """
    bible = state.bible

    for item in delta.get("rituals", []) or []:
        if item and item not in bible.rituals:
            bible.rituals.append(item)
    for item in delta.get("facts", []) or []:
        if item and item not in bible.facts:
            bible.facts.append(item)
    for item in delta.get("milestones", []) or []:
        if item and item not in bible.milestones:
            bible.milestones.append(item)
    for item in delta.get("facts_remove", []) or []:
        bible.facts = [f for f in bible.facts if f != item]
    for item in delta.get("milestones_remove", []) or []:
        bible.milestones = [m for m in bible.milestones if m != item]
    for p in delta.get("promises_open", []) or []:
        if isinstance(p, dict) and p.get("text"):
            if not any(x.get("text") == p["text"] for x in bible.promises_open):
                bible.promises_open.append(p)
    for p in delta.get("promises_closed", []) or []:
        bible.promises_open = [x for x in bible.promises_open if x.get("text") != p]
    if delta.get("pet_name"):
        bible.pet_name = delta["pet_name"]
    if delta.get("relationship_note"):
        bible.relationship_note = delta["relationship_note"]

    # 兜底限量：把上限设得宽松（40），正常情况下靠 facts_remove 维护
    bible.facts = bible.facts[-40:]
    bible.rituals = bible.rituals[-10:]
    bible.milestones = bible.milestones[-20:]
    bible.promises_open = bible.promises_open[-10:]


def render_bible(state: GameState) -> str:
    """把档案渲染成注入文本。"""
    b = state.bible
    if not any([b.rituals, b.promises_open, b.facts, b.milestones, b.pet_name]):
        return ""
    lines = ["【你们的过去】"]
    if b.pet_name:
        lines.append(f"她给你起的称呼：{b.pet_name}")
    if b.relationship_note:
        lines.append(f"当前关系：{b.relationship_note}")
    if b.rituals:
        lines.append("固定的仪式：" + "；".join(b.rituals))
    if b.promises_open:
        lines.append("没兑现的事：" + "；".join(x.get("text", "") for x in b.promises_open))
    if b.facts:
        lines.append("关于她：" + "；".join(b.facts))
    if b.milestones:
        lines.append("已经发生的：" + "；".join(b.milestones))
    return "\n".join(lines)


# --------------------------------------------------------------------------
# 风格漂移检测
# --------------------------------------------------------------------------


@dataclass
class DriftReport:
    drifted: bool
    nya_rate: float
    reasons: list[str]


def detect_drift(state: GameState, her_lines: list[str], cfg: dict[str, Any]) -> DriftReport:
    """检测 LLM 长对话后的退化。

    五条判据：

    1. 句尾「喵」出现率跌破下限（崩坏/沉沦期豁免 —— 喵消失是设计如此）
    2. 出现出戏句式（「作为一只猫娘」「很高兴为您」…）
    3. **模型自评连续多回合与判分背离** —— 说明它的语义理解跑偏了
    4. **模型自评连续多回合纹丝不动** —— 说明它在锚定自己上一轮报的数，
       或者根本没在维护这个变量

    3 和 4 是双轨制的副产物：既然模型每回合都报一个数，那两个计数器就是
    白送的健康度指标，不需要额外调用。
    """
    sc = cfg["style_anchor"]
    reasons: list[str] = []

    # --- 自评判据（与是否有台词无关，所以放在最前面）---
    diverge_limit = sc.get("self_report_diverge_limit", 3)
    flat_limit = sc.get("self_report_flat_limit", 8)
    if state.self_report_streak >= diverge_limit:
        reasons.append(f"模型自评连续 {state.self_report_streak} 回合与判分方向背离")
    if state.self_report_flat >= flat_limit:
        reasons.append(f"模型自评连续 {state.self_report_flat} 回合未变动（疑似锚定）")

    # --- 风格判据 ---
    recent = [t for t in her_lines[-10:] if t.strip()]
    rate = 1.0
    if recent:
        with_nya = sum(1 for t in recent if "喵" in t)
        rate = with_nya / len(recent)
        if state.branch != "wither" and not state.collapse_active and rate < sc["nya_rate_floor"]:
            reasons.append(f"句尾喵率 {rate:.0%} 低于阈值")
        if state.collapse_active or state.withered:
            # 崩坏期/沉沦期喵消失是设计如此，不算漂移
            rate = 1.0

        blob = "\n".join(recent)
        for pat in sc["drift_patterns"]:
            if pat in blob:
                reasons.append(f"出现出戏句式「{pat}」")

    return DriftReport(bool(reasons), rate, reasons)


def style_anchor_text() -> str:
    """重新注入的风格锚点。防止漂移。"""
    return (
        "【风格提醒】\n"
        "你是猫娘，不是助手。每句话结尾带「喵」。\n"
        "抒情、口语化、调皮、傲娇、害羞。不要解释、不建议、不总结、不说教。\n"
        "用耳朵和尾巴的小动作表达情绪，而不是用形容词。\n"
        "不要提到自己是 AI、不要用「希望对你有帮助」这类句式。"
    )


def should_inject_style(state: GameState, cfg: dict[str, Any]) -> bool:
    interval = cfg["style_anchor"]["inject_interval"]
    return state.turn - state.style_inject_at >= interval
