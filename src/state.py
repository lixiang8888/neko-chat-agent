"""状态模型：猫娘 Agent 的全部持久化变量。

设计要点
--------
1. ``affection`` 是慢变量，代表长期关系；``mood`` 是快变量，代表当下心情。
   两者解耦 —— 好感高不代表此刻心情好，这个落差本身就是亲密感的来源。
2. ``tier_floor`` 是「档位回落」的底线。闲置时好感只在当前档位内下探，
   不跨档位；但单次大幅扣分（见 aversion.trigger_at）会打掉一档。
3. ``neg_depth`` 记录已经跌入过几个负值区间，用于负向分值的累进放大。
   它是单向的 —— 一旦跌深，罪孽不会因为回升而消失。
4. ``branch`` 是崩坏线的最终归属：normal / bloom / wither。wither 不可逆。
5. 存档用「临时文件 + rename」原子落盘，避免中途崩溃写坏状态。
"""

from __future__ import annotations

import json
import os
import random
import tempfile
from collections import deque
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Deque, Literal

Branch = Literal["normal", "bloom", "wither"]
Phase = Literal["normal", "warning", "collapse", "recovering"]

SAVE_VERSION = 1


# --------------------------------------------------------------------------
# 子结构
# --------------------------------------------------------------------------


@dataclass
class Aversion:
    """对某一行为族的持久抵触。

    触发条件：单次事件扣分 <= aversion.trigger_at（默认 -12）。
    与「信任冻结」的区别在于 —— 冻结是短期的全局惩罚，抵触是长期且
    只针对那一类行为。她会记住「你在这件事上伤过我」。
    """

    family: str
    strength: float
    created_turn: int
    last_decay_turn: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Aversion":
        return cls(
            family=d["family"],
            strength=float(d.get("strength", 1.0)),
            created_turn=int(d.get("created_turn", 0)),
            last_decay_turn=int(d.get("last_decay_turn", 0)),
        )


@dataclass
class MessageRef:
    """一条对话消息。``id`` 形如 m00001，用于压缩范围寻址。

    ``kind`` 区分普通回合与危机回合。危机回合的消息**照常存进 history**
    （她确实会记得），但**不进压缩摘要** —— 不被总结成剧情素材。
    详见 ``memory`` 里对 crisis 的过滤。
    """

    id: str
    role: str  # user | assistant | system
    text: str
    turn: int
    tokens: int = 0
    kind: str = "normal"  # normal | crisis

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "MessageRef":
        return cls(**d)


@dataclass
class SummaryBlock:
    """压缩块。tier 1 = 原始对话的摘要，tier 2 = T1 的蒸馏，tier 3 = 超级浓缩。

    与 billion-context 的差异：这里的块还要额外承载「角色一致性」信息 ——
    她的语气变化、关系进展、未兑现的承诺，这些在角色扮演里比文件路径更关键。
    """

    block_id: str
    tier: int
    topic: str
    summary: str
    start_turn: int
    end_turn: int
    child_block_ids: list[str] = field(default_factory=list)
    active: bool = True

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "SummaryBlock":
        return cls(**d)


@dataclass
class StoryBible:
    """精炼剧情档案 —— 始终注入，永不压缩。

    这是解决「压缩后失忆」的核心：把压缩丢掉的语义信息，用结构化字段
    显式保存下来。LLM 不需要记住发生过什么，它只需要读这张表。
    """

    rituals: list[str] = field(default_factory=list)
    promises_open: list[dict[str, Any]] = field(default_factory=list)
    facts: list[str] = field(default_factory=list)
    milestones: list[str] = field(default_factory=list)
    pet_name: str | None = None
    relationship_note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "StoryBible":
        return cls(
            rituals=list(d.get("rituals", [])),
            promises_open=list(d.get("promises_open", [])),
            facts=list(d.get("facts", [])),
            milestones=list(d.get("milestones", [])),
            pet_name=d.get("pet_name"),
            relationship_note=d.get("relationship_note", ""),
        )


# --------------------------------------------------------------------------
# 主状态
# --------------------------------------------------------------------------


@dataclass
class GameState:
    """一整个存档的全部状态。"""

    # --- 元信息 ---
    version: int = SAVE_VERSION
    turn: int = 0
    created_at: str = ""
    save_id: str = "default"

    # --- 身份 ---
    # 她的名字。开新周目时由玩家输入，默认「猫娘」。
    # 用途：CLI 提示符、提示词里的人称、存档可读性。
    her_name: str = "猫娘"

    # --- 核心数值 ---
    affection: float = 50.0
    mood: int = 0

    # --- 档位 ---
    tier_floor: int = 50
    tier_progress: float = 0.0
    milestones: list[int] = field(default_factory=list)

    # --- 负值累进 ---
    neg_depth: int = 0
    deep_negative: bool = False
    probation: int = 0

    # --- 惩罚与抵触 ---
    trust_freeze_until: int = 0
    aversions: list[Aversion] = field(default_factory=list)

    # --- 崩坏分支 ---
    collapse_active: bool = False
    ever_collapsed: bool = False
    recovery_counter: int = 0
    collapse_negatives: int = 0
    branch: Branch = "normal"
    bloomed: bool = False
    withered: bool = False

    # --- 内在状态 ---
    desire: str | None = None
    desire_expires: int = 0
    desire_since: int = 0

    # --- 模型自评（方向校验用，不驱动数值）---
    # 程序判分是唯一数值源头；这里存的是**模型自己报的数**，只用来做方向一致性校验。
    # 两条轨道独立演化，背离时按护栏 1 取低一档。详见 scoring.apply_self_report。
    self_affection: int | None = None
    self_report_streak: int = 0   # 连续「方向背离」回合数
    self_report_flat: int = 0     # 连续「自评纹丝不动」回合数（抓锚定）

    # --- 强制痛苦性行为（forced_pain）递进等级 ---
    # 0 = 未触发；1~5 对应 prompts/persona.md 里的 L1 哀求 → L2 放弃思考
    # → L3 哭泣痛苦回应 → L4 身体失控放荡呻吟 → L5 绝望呆滞/本能反应。
    # 内射 / 射进去 / 中出 类触发词会让 level 立刻 +1（数值加深一档，上限 L5）。
    forced_pain_active: bool = False
    forced_pain_level: int = 0
    forced_pain_expression: str = ""      # 害怕 / 闪躲 / 沉默
    forced_pain_turns: int = 0            # 已持续回合数（用于按轮次推进）
    forced_pain_creampie_count: int = 0   # 内射类触发累计次数
    forced_pain_recovery_streak: int = 0  # 连续正向回合数（用于退出）

    # --- 交互统计 ---
    cooldowns: dict[str, list[int]] = field(default_factory=dict)
    used_once: list[str] = field(default_factory=list)
    idle_counter: int = 0
    care_window: list[int] = field(default_factory=list)
    last_events: list[str] = field(default_factory=list)

    # --- 记忆 ---
    messages: list[MessageRef] = field(default_factory=list)
    blocks: list[SummaryBlock] = field(default_factory=list)
    bible: StoryBible = field(default_factory=StoryBible)
    next_msg_seq: int = 1
    next_block_seq: int = 1
    style_inject_at: int = 0

    # ------------------------------------------------------------------
    # 便捷访问
    # ------------------------------------------------------------------

    def new_message_id(self) -> str:
        mid = f"m{self.next_msg_seq:05d}"
        self.next_msg_seq += 1
        return mid

    def new_block_id(self) -> str:
        bid = f"b{self.next_block_seq}"
        self.next_block_seq += 1
        return bid

    @property
    def care_ratio(self) -> float:
        """滑动窗口内的正向互动占比。窗口为空时视为 1.0（尚未建立样本）。"""
        if not self.care_window:
            return 1.0
        return sum(self.care_window) / len(self.care_window)

    @property
    def high_tier(self) -> int:
        """当前已解锁的最高档位下限。"""
        return self.tier_floor

    def aversion_for(self, family: str) -> Aversion | None:
        for av in self.aversions:
            if av.family == family:
                return av
        return None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["bible"] = self.bible.to_dict()
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "GameState":
        d = dict(d)
        d["bible"] = StoryBible.from_dict(d.get("bible") or {})
        d["aversions"] = [Aversion.from_dict(x) for x in d.get("aversions") or []]
        d["messages"] = [MessageRef.from_dict(x) for x in d.get("messages") or []]
        d["blocks"] = [SummaryBlock.from_dict(x) for x in d.get("blocks") or []]
        known = {f for f in cls.__dataclass_fields__}
        d = {k: v for k, v in d.items() if k in known}
        return cls(**d)

    # ------------------------------------------------------------------
    # 派生状态
    # ------------------------------------------------------------------

    def phase(self, collapse_threshold: float = -20.0) -> Phase:
        if self.collapse_active:
            return "recovering" if self.recovery_counter > 0 else "collapse"
        if self.affection < collapse_threshold:
            return "collapse"
        if self.affection < 0 or self.care_ratio < 0.3:
            return "warning"
        return "normal"

    def window_tokens(self, protect_turns: int) -> int:
        """当前发出去的大致 token 量（含压缩块摘要 + 全部未压缩原文）。

        必须统计全部未压缩消息，而不是只看受保护的尾部 ——
        未压缩的旧消息同样会发出去，忽略它们会让压缩阈值永远不触发，
        然后在某个时刻突然溢出窗口。
        """
        total = 0
        for b in self.blocks:
            if b.active:
                total += estimate_tokens(b.summary)
        for m in self.messages:
            if m.role == "system":
                continue
            total += m.tokens
        return total


# --------------------------------------------------------------------------
# token 估算（CJK 感知）
# --------------------------------------------------------------------------


def estimate_tokens(text: str) -> int:
    """粗略 token 估算，对中文友好。

    中文一个字大约 1 token 略多，英文约 4 字符 1 token。
    这个估算只用于决定「什么时候压缩」，不需要精确到个位。
    """
    if not text:
        return 0
    cjk = 0
    other = 0
    for ch in text:
        if "\u4e00" <= ch <= "\u9fff" or "\u3040" <= ch <= "\u30ff":
            cjk += 1
        else:
            other += 1
    return int(cjk * 1.05 + other / 3.6) + 1


# --------------------------------------------------------------------------
# 存档读写（原子落盘）
# --------------------------------------------------------------------------


class SaveStore:
    """存档仓库。写盘用「临时文件 + rename」，任何时刻崩掉都不会写坏存档。"""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def path_for(self, save_id: str) -> Path:
        return self.root / f"{save_id}.json"

    def exists(self, save_id: str) -> bool:
        return self.path_for(save_id).exists()

    def load(self, save_id: str = "default") -> GameState:
        path = self.path_for(save_id)
        if not path.exists():
            state = GameState(save_id=save_id)
            state.created_at = _now_iso()
            return state
        try:
            raw = path.read_text(encoding="utf-8")
            return GameState.from_dict(json.loads(raw))
        except (OSError, ValueError, TypeError):
            # 存档损坏时不静默重置 —— 备份后新建，避免玩家进度无声消失
            broken = path.with_suffix(f".broken-{_now_stamp()}.json")
            try:
                path.rename(broken)
            except OSError:
                pass
            state = GameState(save_id=save_id)
            state.created_at = _now_iso()
            state.bible.relationship_note = f"（原存档损坏，已备份为 {broken.name}）"
            return state

    def save(self, state: GameState) -> None:
        path = self.path_for(state.save_id)
        payload = json.dumps(state.to_dict(), ensure_ascii=False, indent=2)
        fd, tmp = tempfile.mkstemp(dir=str(self.root), prefix=".tmp-", suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(payload)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
        except OSError:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise

    def delete(self, save_id: str) -> None:
        p = self.path_for(save_id)
        if p.exists():
            p.unlink()

    def list_saves(self) -> list[str]:
        return sorted(p.stem for p in self.root.glob("*.json") if not p.name.startswith("."))


def _now_iso() -> str:
    from datetime import datetime

    return datetime.now().isoformat(timespec="seconds")


def _now_stamp() -> str:
    from datetime import datetime

    return datetime.now().strftime("%Y%m%d%H%M%S")


# --------------------------------------------------------------------------
# 派生：由数值推导出的角色表现参数（供提示词注入使用）
# --------------------------------------------------------------------------


def intimacy_allowed(state: GameState, gate: int = 80) -> bool:
    """露骨内容是否已解锁。

    **用 ``tier_floor`` 判定，不用 ``affection``。** 理由是档位特性本身：
    ``tier_floor`` 只升不降、不可撤销，天然是「解锁」语义；而 ``affection``
    会因闲置回落波动 —— 用它判定会出现「解锁了又锁回去」的荒谬情况，
    正是 ``_tick_idle()`` 专门修掉的那类 bug。

    把门槛放这里而不是写进提示词：模型看不到数值，一句「好感达到 80 才行」
    它无法执行。这里算成布尔结论再注入，才是程序强制的里程碑。
    """
    return state.tier_floor >= gate


def tone_profile(state: GameState, intimate_gate: int = 80) -> dict[str, Any]:
    """把数值翻译成「她该怎么演」。这是提示词层唯一需要读的接口。

    LLM 不应该看到原始数字的取舍逻辑，只应该看到结论。
    """
    a = state.affection
    intimate = intimacy_allowed(state, intimate_gate)
    if state.withered:
        return {"label": "沉沦", "initiative": 0.0, "nya_visible": True, "body_language": False,
                "intimacy_allowed": False,
                "note": "安静、礼貌、疏远。带喵，但机械。不主动起话题。"}
    if state.branch == "bloom":
        return {"label": "绽放", "initiative": 1.0, "nya_visible": True, "body_language": True,
                "intimacy_allowed": intimate,
                "note": "主动、明亮、会自己主持日常仪式。句子短而快。"}
    if state.collapse_active:
        return {"label": "崩坏", "initiative": 0.0, "nya_visible": False, "body_language": False,
                "intimacy_allowed": intimate,
                "note": "句尾喵消失或机械重复。耳朵尾巴的描写彻底不给，只保留极偶尔的裂缝信号。"}
    if a >= 90:
        return {"label": "认定", "initiative": 0.9, "nya_visible": True, "body_language": True,
                "intimacy_allowed": intimate,
                "note": "会主动说想留下。允许她说出独占性的话。"}
    if a >= 80:
        return {"label": "独属", "initiative": 0.7, "nya_visible": True, "body_language": True,
                "intimacy_allowed": intimate,
                "note": "主动要陪伴，吃醋明显，开始用独家称呼。"}
    if a >= 65:
        return {"label": "依赖", "initiative": 0.5, "nya_visible": True, "body_language": True,
                "intimacy_allowed": intimate,
                "note": "愿意分享私事，允许触碰尾巴附近。"}
    if a >= 50:
        return {"label": "天生好感", "initiative": 0.35, "nya_visible": True, "body_language": True,
                "intimacy_allowed": intimate,
                "note": "主动搭话、撒娇讨摸。随时可以退开。"}
    if a >= 0:
        return {"label": "警惕", "initiative": 0.15, "nya_visible": True, "body_language": False,
                "intimacy_allowed": intimate,
                "note": "保持距离，只回应必要对话。不主动。"}
    return {"label": "敌意", "initiative": 0.0, "nya_visible": True, "body_language": False,
            "intimacy_allowed": intimate,
            "note": "冷言冷语，句尾不再带喵。"}
