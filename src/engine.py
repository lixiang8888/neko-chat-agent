"""状态机：把评分结果转化为状态演化。

本模块实现用户在讨论中确认的全部机制，编号对应需求：

1. 档位回落修复 + 大幅扣分打掉档位 + 信任冻结 + 抵触（aversion）
2. 负值不封底；跌破 -50 后回升难度大增（系数衰减 + 禁制期）；
   回到 +20 才完全恢复；每跌入一个新负值区间，负向幅度累进放大
3. 随机心情，隐藏数值，只透过动作体现
4. care_ratio 滑动窗口 20 回合
5. -50 ~ -20 之间回升难度不变（常规机制）
6. 内在状态机：她当下想要什么
7. 精炼文件持久化（见 state.SaveStore / memory.StoryBible）
10. WITHER 锁死内容，保留新周目
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Any

from .scoring import Proposal, Rules, ScoreResult, apply_self_report, resolve
from .state import Aversion, GameState, tone_profile


# --------------------------------------------------------------------------
# 回合报告
# --------------------------------------------------------------------------


@dataclass
class TurnReport:
    turn: int
    delta: float
    affection: float
    tier_floor: int
    result: ScoreResult | None = None
    events: list[str] = field(default_factory=list)
    branch_change: str | None = None

    def render(self) -> str:
        parts = [f"[第 {self.turn} 回合] 好感 {self.affection:.1f}"]
        if self.delta:
            parts.append(f"变化 {self.delta:+.2f}")
        parts.append(f"档位下限 {self.tier_floor}")
        if self.events:
            parts.append("｜".join(self.events))
        return "  ".join(parts)


# --------------------------------------------------------------------------
# 引擎
# --------------------------------------------------------------------------


class Engine:
    def __init__(self, state: GameState, rules: Rules, rng: random.Random | None = None) -> None:
        self.state = state
        self.rules = rules
        self.cfg = rules.config
        self.rng = rng or random.Random()

    # ------------------------------------------------------------------
    # 主循环
    # ------------------------------------------------------------------

    def process_turn(
        self,
        proposal: Proposal,
        *,
        player_text: str = "",
        her_last_line: str = "",
        last_player_text: str = "",
        self_delta: int | None = None,
    ) -> TurnReport:
        """跑一个回合。``self_delta`` 是模型自评的变化量（双轨制的第二轨）。

        自评**不驱动数值**，只做方向校验 —— 方向与判分相反时按护栏 1 取低一档。
        详见 ``scoring.apply_self_report``。
        """
        state = self.state
        state.turn += 1
        events: list[str] = []

        # --- 兼容提案为空的情况 ---
        if proposal is None:
            proposal = Proposal()

        # --- 心情快变量先动 ---
        self._tick_mood(events)

        # --- 让位给新回合的计时器 ---
        self._expire_desire(events)

        # --- 判分 ---
        result = resolve(
            proposal,
            state,
            self.rules,
            player_text=player_text,
            her_last_line=her_last_line,
            last_player_text=last_player_text,
        )

        # --- 模型自评并入（只能下调，不能上调）---
        apply_self_report(result, self_delta, state)

        # --- 应用分值 ---
        delta = self._apply_delta(result, events)

        # --- 派生状态（档位 / 跌深记录 / 深度负值态）---
        self._sync_derived(events)

        # --- 抵触：单次大幅扣分产生持久排斥 ---
        self._register_aversion(result, events)

        # --- 冷却记账 ---
        self._record_action(result)

        # --- 滑动窗口 ---
        self._push_care_window(result)

        # --- 崩坏分支判定 ---
        branch_change = self._evaluate_branches(result, events)

        # --- 空闲回落与档位维护 ---
        self._tick_idle(events)

        # --- 抵触衰减 ---
        self._decay_aversions(events)

        # --- 心情受事件影响 ---
        self._mood_from_event(result)

        # --- 主动状态：她当下想要什么 ---
        self._roll_desire(events)

        report = TurnReport(
            turn=state.turn,
            delta=delta,
            affection=state.affection,
            tier_floor=state.tier_floor,
            result=result,
            events=events,
            branch_change=branch_change,
        )
        return report

    # ------------------------------------------------------------------
    # 应用分值
    # ------------------------------------------------------------------

    def _tier_scale(self) -> float:
        """当前档位的分/好感换算率。

        曲线（50→60 需 10 分，60→70 需 15，70→80 需 22，80→90 需 32，
        90→100 需 45）在这里落地成「越往上越慢」的实现：
        每个好感点需要 curve[floor]/10 个行动分。
        """
        floor = max(50, min(90, self.state.tier_floor))
        requirement = self.cfg["tier"]["curve"].get(str(floor), 10)
        return max(0.1, requirement / 10.0)

    def _floor_guard(self, proposed: float, *, allow_break: bool) -> float:
        """档位保护：**被动损耗**不能击穿当前档位下限。

        只用于闲置回落和愿望落空两处 —— 那是没有玩家动作时发生的自然衰减，
        不该把已达成的档位磨掉（好感 95 闲置 200 回合停在 90，不会掉回 89
        把「独家称呼」的解锁撤销掉）。

        ⚠️ **不要用它挡玩家行为造成的扣分。** 那是个真实踩过的 bug：
        新存档的 ``tier_floor`` 初始恰好是 50，等于初始好感，于是任何负分
        都被 ``max(proposed, 50)`` 吃掉 —— 玩家做什么好感都不掉，只有单次
        ≤ -12 才能推动一点。玩家做的事必须真的算数。
        """
        if allow_break:
            return proposed
        return max(proposed, float(self.state.tier_floor))

    def _apply_delta(self, result: ScoreResult, events: list[str]) -> float:
        state = self.state
        raw = result.final_value
        if raw == 0:
            return 0.0

        # 崩坏期与深度负值期的正向被独立处理，不走档位换算
        if raw > 0:
            scaled = raw / self._tier_scale()
        else:
            scaled = raw  # 负向全价，且还会被累进放大

        ceiling = self.cfg["affection"]["ceiling_bloom"] if state.bloomed else self.cfg["affection"]["ceiling"]
        before = state.affection

        # 玩家行为**不受档位下限保护** —— 做了就真的算数。
        # 档位保护只挡被动损耗（闲置回落 / 愿望落空），见 _floor_guard 的说明。
        # 档位本身仍只被 ≥ |aversion.trigger_at| 的大错打掉（见 _register_aversion）。
        state.affection = round(min(ceiling, before + scaled), 3)

        return round(state.affection - before, 3)

    def _sync_derived(self, events: list[str]) -> None:
        """每回合都要重算的派生状态。

        这些不能只在「有分值变化」的回合执行 —— 否则玩家静坐不动时，
        跌深记录不会累积、深度负值态不会解除，状态就与现实脱节了。
        """
        self._sync_floor(events)
        self._sync_neg_depth(events)
        self._sync_deep_negative(events)

    def _sync_floor(self, events: list[str]) -> None:
        """好感上升时抬高档位下限；档位下限是「里程碑」，闲置回落不跨过它。"""
        state = self.state
        for floor in self.cfg["tier"]["floors"]:
            if state.affection >= floor and floor > state.tier_floor:
                state.tier_floor = floor
                if floor not in state.milestones:
                    state.milestones.append(floor)
                    events.append(f"档位达成 {floor}")

    def _sync_neg_depth(self, events: list[str]) -> None:
        """跌入新的负值区间时，深度 +1（不可逆）。"""
        state = self.state
        edges = self.cfg["negative_zones"]["edges"]
        depth = 0
        for i, edge in enumerate(edges):
            if state.affection <= edge:
                depth = i
        if depth > state.neg_depth:
            state.neg_depth = depth
            events.append(f"跌落更深 depth={depth}")

    def _sync_deep_negative(self, events: list[str]) -> None:
        """深度负值态：跌破 -50 进入，回到 +20 才解除（用户第 2 点）。"""
        dc = self.cfg["deep_negative"]
        state = self.state
        if state.affection < dc["deep_below"] and not state.deep_negative:
            state.deep_negative = True
            state.probation = 0
            events.append("陷入深度负值")
        elif state.deep_negative and state.affection >= dc["recover_until"]:
            state.deep_negative = False
            state.probation = 0
            events.append("从深度负值中走出")

    # ------------------------------------------------------------------
    # 抵触（用户第 1 点）
    # ------------------------------------------------------------------

    def _register_aversion(self, result: ScoreResult, events: list[str]) -> None:
        """单次事件扣分 <= trigger_at 时，对该行为族产生持久抵触。"""
        av_cfg = self.cfg["aversion"]
        if result.final_value > av_cfg["trigger_at"]:
            return
        family = result.family
        if family in ("unknown", "axes"):
            family = "unknown"

        existing = self.state.aversion_for(family)
        if existing:
            existing.strength = min(1.0, existing.strength + 0.5)
            existing.created_turn = self.state.turn
            events.append(f"抵触加深 {family}")
        else:
            self.state.aversions.append(
                Aversion(
                    family=family,
                    strength=av_cfg["strength_initial"],
                    created_turn=self.state.turn,
                    last_decay_turn=self.state.turn,
                )
            )
            events.append(f"产生抵触 {family}")

        # 同时进入信任冻结
        freeze = av_cfg.get("freeze_turns") or self.cfg["trust_freeze"]["turns"]
        self.state.trust_freeze_until = max(self.state.trust_freeze_until, self.state.turn + freeze)
        events.append(f"信任冻结 {freeze} 回合")

        # 破防级：打掉一档（用户第 1 点明确要求）
        if result.final_value <= av_cfg["trigger_at"]:
            old = self.state.tier_floor
            self.state.tier_floor = max(self.cfg["tier"]["floors"][0], self.state.tier_floor - 10)
            if self.state.tier_floor != old:
                events.append(f"档位被打掉 {old}→{self.state.tier_floor}")

    def _decay_aversions(self, events: list[str]) -> None:
        av_cfg = self.cfg["aversion"]
        keep: list[Aversion] = []
        for av in self.state.aversions:
            if self.state.turn - av.last_decay_turn >= av_cfg["decay_interval"]:
                av.strength *= av_cfg["decay_factor"]
                av.last_decay_turn = self.state.turn
            if av.strength >= av_cfg["remove_below"]:
                keep.append(av)
            else:
                events.append(f"抵触消解 {av.family}")
        self.state.aversions = keep

    # ------------------------------------------------------------------
    # 崩坏与分支
    # ------------------------------------------------------------------

    def _evaluate_branches(self, result: ScoreResult, events: list[str]) -> str | None:
        state = self.state
        collapse_threshold = self.cfg["collapse"]["threshold"]

        # 已沉沦的存档不再变化
        if state.withered:
            return None

        # --- 绽放：从崩坏中走出并越过 50（必须先经历过崩坏）---
        # 注意：这个判定必须在「未崩坏就早返回」之前，否则永远不会触发。
        if (
            state.ever_collapsed
            and not state.collapse_active
            and state.affection >= self.cfg["collapse"]["bloom_cross"]
            and not state.bloomed
        ):
            state.bloomed = True
            state.branch = "bloom"
            events.append("绽放")
            return "bloom"

        # --- 进入崩坏 ---
        if not state.collapse_active and state.affection <= collapse_threshold:
            state.collapse_active = True
            state.ever_collapsed = True
            state.recovery_counter = 0
            state.collapse_negatives = 0
            events.append("进入崩坏")
            return "collapse"

        if not state.collapse_active:
            return None

        # --- 崩坏期内计数 ---
        if result.final_value < 0:
            state.collapse_negatives += 1
            events.append(f"崩坏期负向 +1（{state.collapse_negatives}）")
        elif result.final_value > 0 and result.action_id:
            spec = self.rules.by_id(result.action_id)
            if spec and spec.bucket == "collapse_specific":
                state.recovery_counter += 1
                events.append(f"无索取陪伴 {state.recovery_counter}")

        # --- 沉沦：崩坏期累计负向达标 ---
        if state.collapse_negatives >= self.cfg["collapse"]["wither_negative_limit"]:
            state.withered = True
            state.branch = "wither"
            events.append("沉沦锁定")
            return "wither"

        # --- 回升：累计无索取陪伴达标且零负向 ---
        need = self.cfg["collapse"]["recover_counter_needed"]
        if (
            state.recovery_counter >= need
            and state.collapse_negatives == 0
            and state.affection > collapse_threshold
        ):
            state.collapse_active = False
            events.append("开始回升")
            return "recovering"

        return None

    # ------------------------------------------------------------------
    # 空闲回落（用户第 1 点修复）
    # ------------------------------------------------------------------

    def _tick_idle(self, events: list[str]) -> None:
        """闲置回落：只在当前档位内下探，不跨过档位下限。

        原始设计会让好感从 95 掉到 89，把「独家称呼」解锁撤销掉——
        那是 bug。已达成档位是里程碑，不可撤销。
        """
        rev = self.cfg["tier"]["idle_reversion"]
        if not rev["enabled"]:
            return
        if self.state.bloomed and rev["bloom_immune"]:
            return
        if self.state.collapse_active or self.state.withered:
            return

        self.state.idle_counter += 1
        if self.state.idle_counter % rev["interval"] != 0:
            return

        floor = float(self.state.tier_floor)
        if self.state.affection > floor:
            self.state.affection = max(floor, self.state.affection - rev["step"])
            events.append(f"闲置回落至 {self.state.affection:.1f}")

    # ------------------------------------------------------------------
    # 心情（用户第 3 点）
    # ------------------------------------------------------------------

    def _tick_mood(self, events: list[str]) -> None:
        m = self.cfg["mood"]
        if self.rng.random() < m["drift_chance"]:
            step = self.rng.choice([-1, 1])
            lo, hi = m["range"]
            self.state.mood = max(lo, min(hi, self.state.mood + step))

    def _mood_from_event(self, result: ScoreResult) -> None:
        m = self.cfg["mood"]
        lo, hi = m["range"]
        if result.final_value >= 2:
            self.state.mood = min(hi, self.state.mood + m["positive_raise"])
        elif result.final_value <= -3:
            self.state.mood = max(lo, self.state.mood - m["negative_drop"])

    def mood_hint(self) -> str:
        """把隐藏心情翻译成表演提示。数字永远不给玩家看。"""
        m = self.state.mood
        if m >= 2:
            return "她今天兴致很高，语速快，小动作多，容易被逗笑。"
        if m == 1:
            return "她今天心情不错，比平时更愿意搭话。"
        if m == 0:
            return "她今天情绪平常，反应与好感度基线一致。"
        if m == -1:
            return "她今天有点闷，回答偏短，但对你依然松。"
        return "她今天很低落，懒得动，耳朵塌着。对别人更冷，对你仍留一条缝。"

    # ------------------------------------------------------------------
    # 内在状态机（用户第 6 点）
    # ------------------------------------------------------------------

    def _roll_desire(self, events: list[str]) -> None:
        d = self.cfg["desire"]
        if self.state.desire is not None:
            return
        if self.state.turn < 3:
            return
        self.state.desire = self.rng.choice(d["wants"])
        span = self.rng.randint(d["roll_min"], d["roll_max"])
        self.state.desire_expires = self.state.turn + span
        self.state.desire_since = self.state.turn
        events.append(f"（她此刻想要：{self.state.desire}）")

    def _expire_desire(self, events: list[str]) -> None:
        """未被满足的愿望过期，算作被忽视。

        注意：这里的扣分同样受档位保护 —— 愿望落空是琐碎的损耗，
        不该把已经达成的档位磨掉。
        """
        if self.state.desire is None:
            return
        if self.state.turn < self.state.desire_expires:
            return
        d = self.cfg["desire"]
        target = self.state.affection + d["ignored_penalty"]
        self.state.affection = round(self._floor_guard(target, allow_break=False), 3)
        events.append(f"她想要的落空了（{self.state.desire}）")
        self.state.desire = None

    def satisfy_desire(self, action_id: str) -> bool:
        """玩家识别到她当下的需求并回应。"""
        mapping = {
            "want_petting": "name_her_need",
            "want_solitude": "stop_on_request",
            "want_showoff": "specific_praise",
            "want_face_saving": "give_face",
            "want_food": "keep_promise",
            "want_play": "name_her_need",
        }
        if self.state.desire and mapping.get(self.state.desire) == action_id:
            self.state.desire = None
            return True
        return False

    def desire_hint_for_prompt(self) -> str:
        """给 LLM 的表演提示 —— 不能直说，要让她用行为暗示。"""
        if not self.state.desire:
            return ""
        hints = {
            "want_petting": "她现在很想被摸头，但绝不会开口要。会绕着你转、把头往你手边凑。",
            "want_solitude": "她现在想独处，会往角落挪。别烦她，让她自己待着。",
            "want_showoff": "她刚做成一件事，很想被夸，但只会假装不经意地提起。",
            "want_face_saving": "她刚才嘴硬了，现在下不来台。等一个台阶。",
            "want_food": "她饿了，会在你附近晃来晃去，但不会直说。",
            "want_play": "她无聊了，想找点事做，会故意捣点小乱。",
        }
        return hints.get(self.state.desire, "")

    # ------------------------------------------------------------------
    # 记账
    # ------------------------------------------------------------------

    def _record_action(self, result: ScoreResult) -> None:
        if result.action_id:
            history = self.state.cooldowns.setdefault(result.action_id, [])
            history.append(self.state.turn)
            # 只留最近的记录，避免长会话把存档撑大
            window = self.cfg["cooldown"]["same_event_turns"]
            self.state.cooldowns[result.action_id] = [
                t for t in history if self.state.turn - t < window * 2
            ]
            if result.final_value > 0:
                spec = self.rules.by_id(result.action_id)
                if spec and spec.once and spec.id not in self.state.used_once:
                    self.state.used_once.append(spec.id)
        self.state.last_events.append(result.action_id or "none")
        self.state.last_events = self.state.last_events[-5:]

        # 有效回合重置闲置计数；中性回合累积（用于档位内回落）
        if result.final_value != 0:
            self.state.idle_counter = 0

    def _push_care_window(self, result: ScoreResult) -> None:
        window = self.cfg["care_ratio"]["window"]
        if result.final_value > 0:
            self.state.care_window.append(1)
        elif result.final_value < 0:
            self.state.care_window.append(0)
        # 中性回合不占窗口，避免挂机拉低 care_ratio
        self.state.care_window = self.state.care_window[-window:]

        # 崩坏期的无索取陪伴算正向照料
        if self.state.collapse_active and result.action_id:
            spec = self.rules.by_id(result.action_id)
            if spec and spec.bucket == "collapse_specific":
                if not self.state.care_window or self.state.care_window[-1] == 0:
                    self.state.care_window.append(1)
                    self.state.care_window = self.state.care_window[-window:]

    # ------------------------------------------------------------------
    # 对外快照
    # ------------------------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        """交给提示词层的状态摘要。注意：数值不进提示词，只进表演结论。"""
        s = self.state
        tone = tone_profile(s, self.cfg["gates"]["intimate"])
        return {
            "turn": s.turn,
            "tone": tone,
            "intimacy_allowed": tone["intimacy_allowed"],
            "mood_hint": self.mood_hint(),
            "desire_hint": self.desire_hint_for_prompt(),
            "aversions": [{"family": a.family, "strength": round(a.strength, 2)} for a in s.aversions],
            "frozen": s.turn < s.trust_freeze_until,
            "frozen_left": max(0, s.trust_freeze_until - s.turn),
            "branch": s.branch,
        }
