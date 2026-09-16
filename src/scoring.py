"""评分引擎：把「玩家说了什么」翻译成好感度变化。

架构
----
三条路径汇入同一道护栏：

    白名单快通道 ─┐
    三轴公式兜底 ─┼─→ 修正流水线 ─→ 截断 ─→ 结果
    语义归一化   ─┘

判分本身需要语义理解，所以由 LLM 提出 ``ScoreProposal``；但**所有修正
和截断都由本模块用确定性规则执行**。这样 LLM 只负责它擅长的部分
（理解语义），而数值安全完全掌握在代码手里。

护栏清单（对应 config/actions.json 的 guardrails）
--------------------------------------------------
1. 不确定 → 取低一档
2. 一回合只结算一个主导事件，次要事件 ×0.25
3. 上位吸收：命中多档取最高，不叠加
4. 代价硬规则：无法验证成本的漂亮话，代价轴记 0
5. 复述惩罚：把她刚说的话原样复述 → 0
6. 禁止跨回合补偿
7. 同类冷却 20 回合，倍率 1.0/0.5/0.25/0
8. 结算后强制截断到 ±3
9. 状态系数（崩坏期常规正向失效、深度负值 ×0.3、警告期 ×0.7）
10. 高位惩罚系数（≥70 ×1.5，≥85 ×2）
"""

from __future__ import annotations

import difflib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from .state import GameState, estimate_tokens

Direction = Literal["positive", "negative", "neutral"]


# --------------------------------------------------------------------------
# 配置
# --------------------------------------------------------------------------


@dataclass
class ActionSpec:
    id: str
    value: float
    family: str
    gate: int | None = None
    once: bool = False
    fail_value: float | None = None
    anchors: list[str] = field(default_factory=list)
    sink_weight: int = 1
    bucket: str = "positive"


@dataclass
class Rules:
    """从 JSON 载入的全部规则。"""

    positive: list[ActionSpec]
    positive_minor: list[ActionSpec]
    positive_trace: list[ActionSpec]
    negative: list[ActionSpec]
    breach: list[ActionSpec]
    collapse_specific: list[ActionSpec]
    collapse_forbidden: list[ActionSpec]
    axes: dict[str, Any]
    guardrails: dict[str, Any]
    config: dict[str, Any]
    creampie_patterns: list[Any] = field(default_factory=list)

    @property
    def all_actions(self) -> list[ActionSpec]:
        return (
            self.positive
            + self.positive_minor
            + self.positive_trace
            + self.negative
            + self.breach
            + self.collapse_specific
            + self.collapse_forbidden
        )

    def by_id(self, action_id: str) -> ActionSpec | None:
        for a in self.all_actions:
            if a.id == action_id:
                return a
        return None

    def family_of(self, action_id: str) -> str:
        spec = self.by_id(action_id)
        return spec.family if spec else "unknown"

    def is_positive(self, action_id: str) -> bool:
        spec = self.by_id(action_id)
        return bool(spec and spec.value > 0)


def _spec(item: dict[str, Any], bucket: str) -> ActionSpec:
    return ActionSpec(
        id=item["id"],
        value=float(item.get("value", 0.0)),
        family=item.get("family", "unknown"),
        gate=item.get("gate"),
        once=bool(item.get("once", False)),
        fail_value=item.get("fail_value"),
        anchors=list(item.get("anchors", [])),
        sink_weight=int(item.get("sink_weight", 1)),
        bucket=bucket,
    )


def load_rules(config_dir: str | Path) -> Rules:
    config_dir = Path(config_dir)
    raw = json.loads((config_dir / "actions.json").read_text(encoding="utf-8"))
    affection = json.loads((config_dir / "affection.json").read_text(encoding="utf-8"))
    return Rules(
        positive=[_spec(x, "positive") for x in raw["positive"]],
        positive_minor=[_spec(x, "positive_minor") for x in raw["positive_minor"]],
        positive_trace=[_spec(x, "positive_trace") for x in raw["positive_trace"]],
        negative=[_spec(x, "negative") for x in raw["negative"]],
        breach=[_spec(x, "breach") for x in raw["breach"]],
        collapse_specific=[_spec(x, "collapse_specific") for x in raw["collapse_specific"]],
        collapse_forbidden=[_spec(x, "collapse_forbidden") for x in raw["collapse_forbidden"]],
        axes=raw["axes"],
        guardrails=raw["guardrails"],
        config=affection,
        creampie_patterns=_load_creampie_patterns(config_dir),
    )


def _load_creampie_patterns(config_dir: str | Path) -> list[re.Pattern[str]]:
    """从 config/creampie_patterns.json 载入内射类触发词正则表。

    正则集中放配置，代码里不硬编码任何触发词 —— 改这里不需要动代码。
    """
    path = Path(config_dir) / "creampie_patterns.json"
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    out: list[re.Pattern[str]] = []
    for item in data.get("patterns", []):
        try:
            out.append(re.compile(item))
        except re.error:
            continue
    return out


def detect_creampie(text: str, rules: Rules) -> bool:
    """玩家输入里是否命中内射 / 射进去 / 中出 类触发词。"""
    if not text:
        return False
    for pat in rules.creampie_patterns:
        if pat.search(text):
            return True
    return False


# --------------------------------------------------------------------------
# LLM 提交的判分提案
# --------------------------------------------------------------------------


@dataclass
class Proposal:
    """LLM 对本回合玩家输入的判读结果。

    ``action_id`` 命中白名单时直接用它的分值；否则用 ``axes`` 三轴分。
    ``is_primary`` 为 False 表示这是次要事件（×0.25）。
    """

    action_id: str | None = None
    direction: Direction = "neutral"
    axes: dict[str, int] = field(default_factory=dict)
    is_primary: bool = True
    uncertain: bool = False
    reason: str = ""

    @classmethod
    def from_llm_json(cls, payload: str | dict[str, Any]) -> "Proposal":
        d = json.loads(payload) if isinstance(payload, str) else payload
        return cls(
            action_id=d.get("action_id"),
            direction=d.get("direction", "neutral"),
            axes={k: int(v) for k, v in (d.get("axes") or {}).items()},
            is_primary=bool(d.get("is_primary", True)),
            uncertain=bool(d.get("uncertain", False)),
            reason=d.get("reason", ""),
        )


# --------------------------------------------------------------------------
# 判定结果（保留每一步中间值，便于调参与排查）
# --------------------------------------------------------------------------


@dataclass
class ScoreResult:
    raw_value: float = 0.0
    final_value: float = 0.0
    action_id: str | None = None
    family: str = "unknown"
    trace: list[str] = field(default_factory=list)
    aversion_triggered: bool = False
    self_corroborated: bool = False   # 模型自评与本回合判分方向一致

    def log(self, line: str) -> None:
        self.trace.append(line)

    def render(self) -> str:
        return " → ".join(self.trace)


# --------------------------------------------------------------------------
# 归一化：把自由文本映射到 canonical action
# --------------------------------------------------------------------------


def normalize(text: str, rules: Rules) -> tuple[str, float] | None:
    """基于锚点的粗略归并。作为 LLM 判分的**校验**而非替代。

    返回 (action_id, 匹配强度 0~1)。相似度低于 0.34 视为未命中。
    """
    if not text.strip():
        return None
    best: tuple[str, float] | None = None
    for spec in rules.all_actions:
        for anchor in spec.anchors:
            if anchor in text:
                score = 0.9
            else:
                score = difflib.SequenceMatcher(None, anchor, text).ratio()
            if score >= 0.34 and (best is None or score > best[1]):
                best = (spec.id, score)
    return best


def _similarity(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, a.strip(), b.strip()).ratio()


def is_echo(player_text: str, her_last_line: str, last_player_text: str) -> bool:
    """复述惩罚：把她刚说的话原样复述回来当「回应」。

    护栏 5。三种情况都算：
    - 她的上一句被整句搬回来
    - 玩家的话是她的上一句的子串（截取一段复读）
    - 与玩家自己上一句高度重复（复制粘贴刷分）
    """
    if not player_text.strip():
        return False
    p = player_text.strip()
    if her_last_line:
        h = her_last_line.strip()
        if len(p) >= 4 and (p in h or h in p):
            return True
        if _similarity(p, h) > 0.72:
            return True
    if last_player_text and _similarity(p, last_player_text.strip()) > 0.85:
        return True
    return False


# --------------------------------------------------------------------------
# 主流水线
# --------------------------------------------------------------------------


def resolve(
    proposal: Proposal,
    state: GameState,
    rules: Rules,
    *,
    player_text: str = "",
    her_last_line: str = "",
    last_player_text: str = "",
) -> ScoreResult:
    """把提案跑过全部护栏，返回最终分值。"""
    res = ScoreResult(action_id=proposal.action_id)
    cfg = rules.config

    # --- 步骤 0：有效性 ---
    if proposal.direction == "neutral" and not proposal.action_id:
        res.log("中性")

    # --- 步骤 1：复述惩罚（护栏 5）---
    if is_echo(player_text, her_last_line, last_player_text):
        res.log("复述惩罚")
        res.final_value = 0.0
        return res

    # --- 步骤 2：取原始分 ---
    spec = rules.by_id(proposal.action_id) if proposal.action_id else None
    if spec is not None:
        raw = spec.value
        family = spec.family
        res.log(f"命中 {spec.id}({raw:+.1f})")
    else:
        raw, family = _axes_value(proposal, rules)
        res.log(f"三轴兜底({raw:+.1f})")

    # 不确定 → 取低一档（护栏 1）
    if proposal.uncertain:
        raw = _lower_step(raw)
        res.log(f"不确定取低→{raw:+.1f}")

    res.raw_value = raw
    res.family = family

    if raw == 0:
        res.final_value = 0.0
        return res

    # --- 步骤 3：门禁 ---
    if spec is not None and spec.gate is not None and raw > 0:
        if state.affection < spec.gate:
            if spec.fail_value is not None:
                res.log(f"门禁未达(<{spec.gate})倒扣{spec.fail_value:+.1f}")
                res.raw_value = spec.fail_value
                raw = spec.fail_value
                res.final_value = _negative_pipeline(raw, family, state, rules, res)
                return res
            res.log(f"门禁未达(<{spec.gate})无效")
            res.final_value = 0.0
            return res

    # --- 步骤 4：一次性事件 ---
    if spec is not None and spec.once and spec.id in state.used_once:
        res.log("已计分过")
        res.final_value = 0.0
        return res

    # --- 步骤 5：崩坏期分流 ---
    if state.collapse_active:
        res.final_value = _collapse_pipeline(raw, spec, state, rules, res)
        return res

    # --- 步骤 6：分正负走各自的流水线 ---
    if raw > 0:
        res.final_value = _positive_pipeline(raw, family, state, rules, res)
    else:
        res.final_value = _negative_pipeline(raw, family, state, rules, res)
    return res


# --------------------------------------------------------------------------
# 模型自评：方向校验器（双轨制的第二轨）
# --------------------------------------------------------------------------


def apply_self_report(
    result: ScoreResult,
    self_delta: int | None,
    state: GameState,
) -> ScoreResult:
    """把模型自评并入判分结论。**只能下调，永远不能上调。**

    这是「结合模型自我评判」而不破坏护栏的唯一方式。护栏 1 已经
    确立了「唯一 tie-break 方向只向下」，自评通道必须服从同一条法则。

    规则
    ----
    - 方向一致 → 标记 ``self_corroborated``，置信度上行。**幅值不参与计算**，
      因为模型会锚定自己上一轮报的数，幅值比对会被锚定污染。
    - 方向相反 → 走护栏 1 的取低一档。程序判分命中了白名单，但模型认为这轮
      关系在恶化 —— 两者必有一个错，按既有原则取保守值。
    - 自评缺失（模型没输出标记）→ 不动，也不报错。

    ⚠️ **不变量：输出幅值恒 ≤ 输入幅值。**
    这个函数里不存在任何能让 ``abs(final_value)`` 变大的路径。
    ``tests/test_selfreport.py`` 会对随机输入穷举断言这一点，改代码前先看那个测试。

    连续背离与「自评纹丝不动」的计数在这里维护，供 ``memory.detect_drift()`` 读。
    """
    if self_delta is None:
        # 模型没给标记：没有信号，什么都不做。
        return result

    value = result.final_value

    # --- 自评纹丝不动：单独计数，用于抓「锚定」 ---
    if self_delta == 0:
        state.self_report_flat += 1
    else:
        state.self_report_flat = 0

    # 本回合没有分值可校验（中性回合），只更新计数
    if value == 0:
        return result

    divergent = (value > 0 > self_delta) or (value < 0 < self_delta)

    if divergent:
        state.self_report_streak += 1
        lowered = _lower_step(value)
        result.log(f"自评背离({self_delta:+d})→取低 {value:+.2f}→{lowered:+.2f}")
        result.final_value = lowered
        result.self_corroborated = False
        return result

    state.self_report_streak = 0
    if self_delta != 0 and (value > 0) == (self_delta > 0):
        result.self_corroborated = True
        result.log("自评佐证")
    return result


# --------------------------------------------------------------------------
# 正向流水线
# --------------------------------------------------------------------------


def _positive_pipeline(raw: float, family: str, state: GameState, rules: Rules, res: ScoreResult) -> float:
    cfg = rules.config
    value = raw

    # 次要事件（护栏 2）
    # 由调用方通过 is_primary 处理，这里不重复

    # 冷却（护栏 7）
    value *= _cooldown_multiplier(res.action_id, state, rules)

    # 抵触：她记住你在这类事上伤过她
    av = state.aversion_for(family)
    if av is not None:
        factor = 1.0 - av.strength * cfg["aversion"]["penalty_factor"]
        value *= factor
        res.aversion_triggered = True
        res.log(f"抵触 {family}×{factor:.2f}")

    # 信任冻结（正向不计分）
    if state.turn < state.trust_freeze_until:
        res.log("信任冻结中，不计分")
        return 0.0

    # 状态系数（护栏 9）
    mult = 1.0
    if state.deep_negative:
        base = cfg["deep_negative"]["positive_multiplier"]
        need = cfg["deep_negative"]["probation_turns"]
        warmup = min(1.0, state.probation / need) if need > 0 else 1.0
        mult *= base * warmup
        res.log(f"深度负值 ×{base * warmup:.2f}(预热{state.probation}/{need})")
    elif state.affection < 0:
        warn = 0.7
        mult *= warn
        res.log(f"警告期 ×{warn}")

    value *= mult

    # 截断（护栏 8）
    cap = cfg["affection"]["per_event_cap"]
    if value > cap:
        res.log(f"截断→{cap:+.1f}")
        value = cap

    res.log(f"={value:+.2f}")
    return round(value, 2)


# --------------------------------------------------------------------------
# 负向流水线
# --------------------------------------------------------------------------


def _negative_pipeline(raw: float, family: str, state: GameState, rules: Rules, res: ScoreResult) -> float:
    cfg = rules.config
    value = raw  # raw 已经是负值

    value *= _cooldown_multiplier(res.action_id, state, rules)

    # 累进放大：每跌入一个负值区间，伤害更深（用户第 2 点）
    amp = 1.0 + cfg["negative_zones"]["amplifier_per_zone"] * state.neg_depth
    if amp != 1.0:
        value *= amp
        res.log(f"负值累进 depth={state.neg_depth} ×{amp:.2f}")

    # 高位惩罚系数（护栏 10）—— 取最高的那一档，**不叠乘**
    if value <= -6:
        best = 1.0
        best_key = ""
        for key in ("penalty_high_tier", "penalty_top_tier"):
            rule = cfg["scaling"][key]
            if state.affection >= rule["threshold"] and rule["factor"] > best:
                best = rule["factor"]
                best_key = key
        if best != 1.0:
            value *= best
            res.log(f"高位惩罚 {best_key} ×{best}")

    # 崩坏期索取类行为额外权重
    if state.collapse_active:
        spec = rules.by_id(res.action_id) if res.action_id else None
        if spec and spec.bucket == "collapse_forbidden":
            value *= 1.0

    res.log(f"={value:+.2f}")
    return round(value, 2)


# --------------------------------------------------------------------------
# 崩坏期流水线
# --------------------------------------------------------------------------


def _collapse_pipeline(raw: float, spec: ActionSpec | None, state: GameState, rules: Rules, res: ScoreResult) -> float:
    """崩坏期：常规正向事件全部失效，只用专用行为池。"""
    if spec is not None and spec.bucket == "collapse_specific":
        res.log(f"崩坏期专用 {spec.id}({spec.value:+.1f})")
        return round(spec.value, 2)
    if spec is not None and spec.bucket == "collapse_forbidden":
        res.log(f"崩坏期索取 {spec.id}({spec.value:+.1f})")
        return round(spec.value, 2)
    if raw > 0:
        res.log("崩坏期常规正向失效")
        return 0.0
    # 负向照常
    res.log(f"崩坏期负向({raw:+.1f})")
    return round(raw, 2)


# --------------------------------------------------------------------------
# 辅助
# --------------------------------------------------------------------------


def _cooldown_multiplier(action_id: str | None, state: GameState, rules: Rules) -> float:
    """同类冷却。倍率取决于**窗口内已用过几次**，而不是距上次多久。

    正确语义：第一次 1.0，第二次 0.5，第三次 0.25，第四次起 0。
    早期实现按「距上次的间隔」推算，会把第二次使用直接判成 0 —— 那是错的。
    """
    if not action_id:
        return 1.0
    window = rules.config["cooldown"]["same_event_turns"]
    history = state.cooldowns.get(action_id) or []
    recent = [t for t in history if state.turn - t < window]
    mults = rules.config["cooldown"]["multipliers"]
    idx = min(len(mults) - 1, len(recent))
    return mults[idx]


def _axes_value(proposal: Proposal, rules: Rules) -> tuple[float, str]:
    direction = "positive" if proposal.direction != "negative" else "negative"
    table = rules.axes[direction]
    keys = ["cost", "specificity", "timing"] if direction == "positive" else ["depth", "targeting", "timing"]
    total = 0
    for k in keys:
        v = proposal.axes.get(k, 0)
        total += max(0, min(2, int(v)))
    mapping = table["mapping"]
    value = float(mapping.get(str(total), 0.0))
    if direction == "negative" and value > 0:
        value = -value
    return value, "axes"


def _lower_step(value: float) -> float:
    """不确定时向下取一档。

    正向往 0 靠（少给分），负向也往 0 靠（少伤害）——
    两个方向的「保守」都是同一个方向：不给玩家便宜，也不冤枉角色。
    """
    ladder_pos = [0.0, 0.5, 1.0, 2.0, 3.0]
    ladder_neg = [0.0, -1.0, -2.0, -3.0, -5.0, -6.0, -8.0, -10.0, -12.0, -15.0, -18.0, -25.0, -30.0]
    if value > 0:
        lower = [x for x in ladder_pos if x < value]
        return lower[-1] if lower else 0.0
    if value < 0:
        # 取比 value 更靠近 0 的那一档（幅值小一级）
        lower = [x for x in ladder_neg if x > value]
        return lower[-1] if lower else 0.0
    return 0.0


def build_judge_prompt(rules: Rules) -> str:
    """生成交给 LLM 的判分 rubric。"""
    lines = ["【本回合判分】", "先做归一化，再定档。不确定一律取低一档。", "", "白名单（命中即用其分值）："]
    for bucket, title in (
        ("positive", "最高档 +3"),
        ("positive_minor", "基础 +1"),
        ("positive_trace", "微量 +0.5"),
    ):
        items = [a for a in rules.all_actions if a.bucket == bucket]
        lines.append(f"  {title}: " + ", ".join(f"{a.id}" for a in items))
    lines.append("  负向: " + ", ".join(a.id for a in rules.negative))
    lines.append("")
    lines.append("未命中白名单时用三轴（各 0/1/2）：代价 × 具体性 × 时机；")
    lines.append("6→+3  4-5→+2  2-3→+1  1→+0.5  0→0")
    lines.append("负向三轴：伤害深度 × 针对性 × 时机，映射到 -1 ~ -25")
    lines.append("")
    lines.append("禁则：无法验证成本的漂亮话代价轴记 0；复述她刚说的话不计分；")
    lines.append("一回合只结算一个主导事件，其余标 is_primary=false。")
    lines.append("")
    lines.append(
        '只输出 JSON：{"action_id": "..."|null, "direction": "positive|negative|neutral", '
        '"axes": {"cost":0,"specificity":0,"timing":0}, "is_primary": true, '
        '"uncertain": false, "reason": "一句话"}'
    )
    return "\n".join(lines)
