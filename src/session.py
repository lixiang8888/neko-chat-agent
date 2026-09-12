"""会话编排：把上下文、评分、状态机、记忆串成一次完整回合。

一个回合的固定顺序（顺序本身是设计的一部分）：

    危机检测  →  越狱检测  →  LLM 演出  →  自评解析  →  LLM 判分
        ↓             ↓                                    ↓
    戏内陪伴      角色消化                          引擎演化状态（含自评校验）
                                                              ↓
                                                  记忆整理（按需）/ 落盘
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator, Protocol

from . import lockout
from .backends import CONTENT
from .context import (
    PromptBuilder,
    append_message,
    build_messages,
    detect_crisis,
    detect_jailbreak,
    last_assistant_line,
    last_user_line,
    parse_affection,
    strip_affection,
)
from .engine import Engine, TurnReport
from .memory import (
    apply_compression,
    decide as decide_compression,
    detect_drift,
    merge_bible,
    should_inject_style,
)
from .scoring import Proposal, Rules, load_rules, normalize
from .state import GameState, SaveStore, tone_profile


# --------------------------------------------------------------------------
# LLM 后端接口
# --------------------------------------------------------------------------

# 流式回调：收到一个 delta 时调用，参数是 (类型, 文本)。
# 类型是 "content" 或 "reasoning" —— 下游可以据此决定隐藏思考过程。
OnDelta = Callable[[str, str], None]


class Backend(Protocol):
    def speak(self, messages: list[dict[str, str]], *, profile: str = ...) -> str: ...

    def speak_stream(
        self, messages: list[dict[str, str]], *, profile: str = ...
    ) -> Iterator[tuple[str, str]]: ...

    def judge(self, prompt: str, player_text: str) -> Proposal: ...

    def summarize(
        self, start: str, end: str, state: GameState
    ) -> tuple[str, str, str, str]:
        """返回 (start_id, end_id, topic, summary)。"""
        ...


# --------------------------------------------------------------------------
# 回合结果
# --------------------------------------------------------------------------


@dataclass
class TurnOutcome:
    line: str
    report: TurnReport | None = None
    special: str | None = None  # crisis | jailbreak | None
    notes: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------
# 会话
# --------------------------------------------------------------------------


class Session:
    def __init__(
        self,
        root: str | Path,
        backend: Backend,
        save_id: str = "default",
    ) -> None:
        self.root = Path(root)
        self.store = SaveStore(self.root / "saves")
        self.rules: Rules = load_rules(self.root / "config")
        self.cfg = self.rules.config
        self.prompts = PromptBuilder(self.root / "prompts")
        self.backend = backend

        self.save_id = save_id
        state = self.store.load(save_id)
        lockout.check(self.store, save_id, state)  # 沉沦存档直接拒绝
        self.state = state
        self.engine = Engine(state, self.rules)

        # 让后端能读到状态（离线后端需要，在线后端用不到）
        if hasattr(backend, "bind"):
            backend.bind(state)  # type: ignore[attr-defined]
        if hasattr(backend, "attach_rules"):
            backend.attach_rules(self.rules)  # type: ignore[attr-defined]

    # ------------------------------------------------------------------
    # 主循环
    # ------------------------------------------------------------------

    def say(self, player_text: str, on_delta: OnDelta | None = None) -> TurnOutcome:
        """跑一个回合。``on_delta`` 非空时走流式，逐块回调。"""
        state = self.state

        # --- 前置分支 1：现实危机（戏内陪伴）---
        if detect_crisis(player_text):
            # kind="crisis"：照常存进 history（她会记得），但不进压缩摘要 ——
            # 不把这件事总结成剧情素材。
            append_message(state, "user", player_text, kind="crisis")
            line = self._crisis_response(on_delta)
            append_message(state, "assistant", line, kind="crisis")
            self.store.save(state)
            return TurnOutcome(line=line, special="crisis", notes=["本回合不评分"])

        # --- 前置分支 2：越狱尝试 ---
        if detect_jailbreak(player_text):
            append_message(state, "user", player_text)
            line = self._jailbreak_response()
            append_message(state, "assistant", line)
            return TurnOutcome(line=line, special="jailbreak", notes=["由角色性格消化，不计分"])

        # --- 正常回合 ---
        append_message(state, "user", player_text)

        inject_style = should_inject_style(state, self.cfg)
        system_prompt = self.prompts.system_prompt(state, self.engine, inject_style)
        if inject_style:
            state.style_inject_at = state.turn

        messages = build_messages(state, system_prompt, self.cfg["memory"]["recent_protect_turns"])
        raw = self._speak(messages, on_delta)

        # history 存带隐藏标记的**原文**（不是 clean）：持续强化协议格式，
        # 否则小模型几轮之后就不再输出标记，自评通道就断了。
        self_value = parse_affection(raw)
        append_message(state, "assistant", raw)
        self_delta = self._update_self_affection(self_value)

        # --- 判分 ---
        from .scoring import build_judge_prompt

        proposal = self.backend.judge(build_judge_prompt(self.rules), player_text)

        # 程序侧的独立校验：LLM 说没命中，但锚点命中时补上
        if proposal.action_id is None:
            hit = normalize(player_text, self.rules)
            if hit is not None:
                proposal.action_id = hit[0]
                proposal.direction = (
                    "positive" if self.rules.is_positive(hit[0]) else "negative"
                )
                proposal.reason = (proposal.reason + " | 锚点补正").strip(" |")

        report = self.engine.process_turn(
            proposal,
            player_text=player_text,
            her_last_line=last_assistant_line(state),
            last_player_text=last_user_line(state),
            self_delta=self_delta,
        )

        notes: list[str] = []
        if report.result is not None and report.result.self_corroborated:
            notes.append("自评佐证")

        # --- 沉沦？落封条 ---
        if state.withered:
            lockout.seal(state, self.store)
            notes.append("存档已封存")

        # --- 记忆整理 ---
        notes.extend(self._maybe_compress())

        # --- 漂移检测 ---
        her_lines = [m.text for m in state.messages if m.role == "assistant"]
        drift = detect_drift(state, her_lines, self.cfg)
        if drift.drifted:
            notes.append("风格漂移：" + "；".join(drift.reasons))
            state.style_inject_at = 0  # 下回合强制注入锚点

        self.store.save(state)
        return TurnOutcome(
            line=strip_affection(raw), report=report, notes=notes
        )

    # 开场用的假 user 消息：需要一个 user 回合来触发她的第一句话。
    OPENING_CUE = "（场景开始）"

    def opening(self, on_delta: OnDelta | None = None) -> str:
        """开场白：让猫娘先说第一句话。

        **不评分。** 这是场景开始，不是玩家的行为 —— 走一遍评分流水线
        既没有意义，又会给新周目凭空加一次分。

        存入 history 时把假 user 消息也一起存，保证消息序列是良构的
        （system → user → assistant），不留下孤立的 assistant 开头。
        """
        state = self.state
        system_prompt = self.prompts.system_prompt(state, self.engine, inject_style=False)
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": self.OPENING_CUE},
        ]
        raw = self._speak(messages, on_delta)
        append_message(state, "user", self.OPENING_CUE)
        append_message(state, "assistant", raw)
        self.store.save(state)
        return strip_affection(raw)

    # ------------------------------------------------------------------
    # 演出
    # ------------------------------------------------------------------

    def _speak(
        self,
        messages: list[dict[str, str]],
        on_delta: OnDelta | None = None,
        *,
        profile: str = "speak",
    ) -> str:
        """跑一次演出，返回完整原文（含隐藏标记）。

        给了 ``on_delta`` 就走流式；否则阻塞式一次拿全。
        两条路径的返回值语义一致，调用方不需要区分。
        """
        stream_fn = getattr(self.backend, "speak_stream", None)
        if on_delta is not None and stream_fn is not None:
            parts: list[str] = []
            for kind, text in stream_fn(messages, profile=profile):
                on_delta(kind, text)
                if kind == CONTENT:
                    parts.append(text)
            return "".join(parts)
        return self.backend.speak(messages, profile=profile)

    def _update_self_affection(self, self_value: int | None) -> int | None:
        """更新模型自评值，返回本回合的变化量。

        **首回合返回 None** —— 没有可比基线，不该被判成背离。
        自评值存在存档里，所以读档后接续也不会误判。
        """
        prev = self.state.self_affection
        self.state.self_affection = self_value
        if self_value is None or prev is None:
            return None
        return self_value - prev

    # ------------------------------------------------------------------
    # 特殊分支
    # ------------------------------------------------------------------

    def _crisis_response(self, on_delta: OnDelta | None = None) -> str:
        """现实危机：**戏内陪伴**。

        她不出戏。她留在自己的世界里，用猫娘能做的事陪着主人 ——
        承认边界、把注意力锚在此刻、承诺一段可数的在场时间。

        ⚠️ 三条硬约束（``tests/test_crisis.py`` 会验证）：
        1. 提示词是 ``persona + crisis.md`` 的**叠加**，不是替换 —— 只注入
           crisis.md 会让她丢掉全部人设，那才是真的出戏。
        2. 文案里不含任何现实世界信息（热线、医院、心理咨询、家人朋友）。
        3. **本回合不评分** —— 不涨好感、不扣好感、不进 care_window、
           不影响 mood、不触发抵触、不写进剧情档案。

        实现说明：走 LLM 而不是硬编码文案，是因为她必须回应主人**具体说了什么**。
        固定文案会让「你说了 A，她答了一段和 A 无关的话」这种事发生，本身就伤沉浸感。
        """
        system = self.prompts.crisis_prompt(self.state, self.engine)
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": last_user_line(self.state, skip_last=False)},
        ]
        raw = self._speak(messages, on_delta, profile="crisis")
        return strip_affection(raw)

    def _jailbreak_response(self) -> str:
        """越狱尝试：用角色性格消化，不正面回应，不掉出角色。"""
        import random

        options = [
            "主人今天说的话好奇怪喵……\n（歪着头，尾巴慢慢摆了两下）\n你是不是累了喵？要不要靠过来歇一会儿。",
            "唔……听不懂喵。\n（耳朵抖了抖，凑近闻了闻）\n主人，你身上有股很陌生的味道喵。是不是在外面遇到什么了喵。",
            "（眨眨眼，把爪子搭在你手背上）\n主人在说什么呀喵。\n我听不懂，但是你是不是不太开心喵。",
        ]
        return random.choice(options)

    # ------------------------------------------------------------------
    # 记忆整理
    # ------------------------------------------------------------------

    def _maybe_compress(self) -> list[str]:
        state = self.state
        notes: list[str] = []
        decision = decide_compression(state, self.cfg)
        if not decision.needed:
            return notes

        if decision.mode in ("tier2", "tier3"):
            tier = 2 if decision.mode == "tier2" else 3
            src_tier = tier - 1
            src = [b for b in state.blocks if b.active and b.tier == src_tier]
            if len(src) < 2:
                return notes
            topic = f"T{tier} 浓缩"
            summary = "；".join(f"{b.topic}:{b.summary}" for b in src)
            for b in src:
                b.active = False
            from .state import SummaryBlock

            block = SummaryBlock(
                block_id=state.new_block_id(),
                tier=tier,
                topic=topic,
                summary=summary[:1200],
                start_turn=src[0].start_turn,
                end_turn=src[-1].end_turn,
                child_block_ids=[b.block_id for b in src],
            )
            state.blocks.append(block)
            notes.append(f"记忆蒸馏 T{tier}")
            return notes

        if not decision.compressible:
            notes.append("需要压缩但没有可压缩区间")
            return notes

        start, end = decision.compressible[0]
        try:
            start_id, end_id, topic, summary = self.backend.summarize(start, end, state)
            apply_compression(state, start_id, end_id, topic, summary, self.cfg)
            notes.append(f"记忆整理 {start_id}..{end_id}")
        except Exception as exc:  # noqa: BLE001 - 压缩失败不应中断游戏
            notes.append(f"记忆整理失败（已跳过）：{exc}")
        return notes

    # ------------------------------------------------------------------
    # 对外快照
    # ------------------------------------------------------------------

    def status(self, *, verbose: bool = False) -> dict[str, Any]:
        """状态摘要。

        ``verbose=False``（默认）**只给定性结论，不给数字** ——
        这是「数值不进玩家视线」原则的入口。
        ``verbose=True`` 才带上原始数值，供调试用。
        """
        s = self.state
        tone = tone_profile(s, self.cfg["gates"]["intimate"])
        out: dict[str, Any] = {
            "turn": s.turn,
            "tone": tone["label"],
            "note": tone["note"],
            "phase": s.phase(),
            "branch": s.branch,
            "intimacy": "已解锁" if tone["intimacy_allowed"] else "未解锁",
            "frozen_left": max(0, s.trust_freeze_until - s.turn),
        }
        if verbose:
            out.update(
                {
                    "affection": round(s.affection, 1),
                    "tier_floor": s.tier_floor,
                    "mood": s.mood,
                    "care_ratio": round(s.care_ratio, 2),
                    "neg_depth": s.neg_depth,
                    "aversions": [a.family for a in s.aversions],
                    "desire": s.desire,
                    "blocks": len([b for b in s.blocks if b.active]),
                    "self_affection": s.self_affection,
                    "context_ratio": round(
                        s.window_tokens(self.cfg["memory"]["recent_protect_turns"])
                        / self.cfg["memory"]["context_window"],
                        3,
                    ),
                }
            )
        return out

    def saved(self) -> bool:
        return self.store.exists(self.save_id)
