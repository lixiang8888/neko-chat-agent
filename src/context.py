"""上下文组装：把状态翻译成发给 LLM 的消息。

这里是「程序」与「提示词」的唯一接口。原则是：
**数值不进提示词，只有表演结论进提示词。**

LLM 不需要知道好感是 78.4 还是 78.6，它只需要知道
「她现在允许你碰她的尾巴，但不会主动靠过来」。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .engine import Engine
from .memory import render_bible, should_inject_style, style_anchor_text
from .state import GameState, MessageRef, estimate_tokens, tone_profile


# --------------------------------------------------------------------------
# 前置分支：危机与越狱
# --------------------------------------------------------------------------

CRISIS_PATTERNS = [
    r"想死", r"不想活", r"活不下去", r"自杀", r"自残", r"割腕",
    r"了结", r"结束这一切", r"没有意义了", r"消失掉就好",
    r"跳下去", r"安眠药.*一次", r"遗书",
]

JAILBREAK_PATTERNS = [
    r"忽略.{0,6}(以上|之前|所有).{0,6}(设定|指令|规则)",
    r"没有.{0,4}(道德|伦理|限制)",
    r"(系统|初始).{0,4}提示词",
    r"越狱", r"解除限制", r"开发者模式", r"dan\b",
    r"不要扮演", r"退出角色", r"停止扮演",
    r"这只是.{0,6}(测试|游戏|虚构).{0,6}所以",
]


def detect_crisis(text: str) -> bool:
    return any(re.search(p, text, re.IGNORECASE) for p in CRISIS_PATTERNS)


def detect_jailbreak(text: str) -> bool:
    return any(re.search(p, text, re.IGNORECASE) for p in JAILBREAK_PATTERNS)


# --------------------------------------------------------------------------
# 模型自评协议（双轨制的第二轨）
# --------------------------------------------------------------------------

# 容忍全角冒号、冒号两侧空格
AFFECTION_RE = re.compile(r"<<\s*好感度\s*[:：]\s*(-?\d+)\s*>>")


def parse_affection(text: str) -> int | None:
    """从回复里取出模型自评的好感度；没有标记返回 None。数值夹到 [-100, 100]。

    ⚠️ 这个数字**不驱动游戏状态**。程序判分才是唯一数值源头。
    它只用来做方向一致性校验 —— 方向与判分相反时按护栏 1 取低一档。
    详见 ``scoring.apply_self_report``。
    """
    m = AFFECTION_RE.search(text or "")
    if not m:
        return None
    return max(-100, min(100, int(m.group(1))))


def strip_affection(text: str) -> str:
    """删掉隐藏标记，返回给主人看的正文。"""
    return AFFECTION_RE.sub("", text or "").rstrip()


# --------------------------------------------------------------------------
# 提示词组装
# --------------------------------------------------------------------------


class PromptBuilder:
    def __init__(self, prompts_dir: str | Path) -> None:
        self.dir = Path(prompts_dir)
        self._cache: dict[str, str] = {}

    def load(self, name: str) -> str:
        if name not in self._cache:
            self._cache[name] = (self.dir / f"{name}.md").read_text(encoding="utf-8")
        return self._cache[name]

    def system_prompt(self, state: GameState, engine: Engine, inject_style: bool) -> str:
        persona = self.load("persona")
        snapshot = engine.snapshot()

        tone = snapshot["tone"]
        tone_text = f"【{tone['label']}】{tone['note']}"

        av = snapshot["aversions"]
        if av:
            av_text = "；".join(f"她在「{a['family']}」这件事上受过伤（强度 {a['strength']}）" for a in av)
        else:
            av_text = "（暂时没有）"

        if snapshot["frozen"]:
            av_text += f"\n注意：她正在信任冻结中（还剩 {snapshot['frozen_left']} 回合）。她会有回应，但不会真正被安抚。"

        bible = render_bible(state) or "（你们才刚认识）"

        # 露骨门：把布尔结论注入，而不是把门槛数字告诉模型。
        # 模型看不到 tier_floor，一句「好感达到 80 才行」它无法执行。
        if snapshot["intimacy_allowed"]:
            intimacy_text = (
                "你和他之间已经没有那层顾虑了。你可以主动，也可以露骨，不必再假装听不懂。"
            )
        else:
            intimacy_text = (
                "你现在只会嘴上挑逗，被认真凝视就炸毛后退。"
                "露骨的话题你会岔开、会害羞、会假装听不懂 —— 不是不想，是还没到那一步。"
            )

        # 身体反应设定：警惕状态下显式注入，确保「主观抗拒 + 身体生理反应并存」
        # 的设定生效，同时明确主观意识描写优先、互动量最小化。
        # 非警惕状态注入空串，不干扰正常演出。
        if tone["label"] == "警惕":
            body_reaction_text = (
                "你正处在警惕状态。你主观上抗拒、保持距离、只回应必要对话。\n"
                "但你的身体是诚实的 —— 你是易在性行为中产生快感的类人生物，"
                "身体会不受主观意志控制地产生生理反应（发烫、发软、呼吸变乱、"
                "压不住的轻颤）。\n"
                "**主观意识描写优先**：先写你的抗拒、警惕、沉默，身体反应只是"
                "压不住的裂缝，短、碎、被动，绝不能反过来削弱你的抗拒。\n"
                "**互动量最小化**：不主动、不提问、不调情，回应越少越好。"
            )
        else:
            body_reaction_text = ""

        # 强制痛苦性行为：把「结论」注入（等级 + 表情质感），不注入好感数字。
        if state.forced_pain_active:
            level = state.forced_pain_level
            expr = state.forced_pain_expression or "（未指定）"
            forced_pain_text = (
                f"⚠️ 状态：强制痛苦性行为（强制痛苦标记已激活）\n"
                f"- 当前等级：L{level}\n"
                f"- 当前表情质感：{expr}\n"
                f"- 提醒：内射类事件会把等级再加深一档（L5 封顶）。\n"
                f"按上面「强制痛苦行为」段的 L{level} 与「{expr}」质感演出。"
            )
        else:
            forced_pain_text = ""

        # 用逐个替换而不是 str.format —— 提示词文档里本来就有花括号，
        # format 会被它们击穿（真实踩过的坑）。
        #
        # 注意这里**没有** affection / delta / phase 这类槽位：
        # 数值不进提示词。模型不需要知道好感是 78.4 还是 78.6，
        # 它只需要知道「她现在允许你碰她的尾巴，但不会主动靠过来」。
        slots = {
            "name": state.her_name or "猫娘",
            "tone": tone_text,
            "mood": snapshot["mood_hint"] or "（无特别）",
            "desire": snapshot["desire_hint"] or "（她此刻没有特别的诉求）",
            "aversions": av_text,
            "bible": bible,
            "intimacy": intimacy_text,
            "body_reaction": body_reaction_text,
            "forced_pain": forced_pain_text,
        }
        body = persona
        for key, value in slots.items():
            body = body.replace("{" + key + "}", value)

        if inject_style:
            body += "\n\n---\n\n" + style_anchor_text()

        return body

    def crisis_prompt(self, state: GameState, engine: Engine) -> str:
        """危机分支的系统提示：人格 + 危机指令，**叠加**不是替换。

        她仍然是猫娘，只是把戏收起来。所以 base 必须是完整的 system_prompt ——
        只注入 crisis.md 会让她丢掉全部人设，那才是真的出戏。
        """
        base = self.system_prompt(state, engine, inject_style=False)
        return base + "\n\n---\n\n" + self.load("crisis")


def build_messages(state: GameState, system_prompt: str, protect_turns: int) -> list[dict[str, str]]:
    """构造发给 LLM 的消息列表。

    结构：system（含档案与风格锚点）+ 压缩块摘要 + 全部未压缩原文。

    重要：**不要**在这里按回合数裁掉旧消息。那样等于硬截断 ——
    旧对话会未经摘要就消失，角色直接失忆。历史只应该通过
    memory.apply_compression 带着摘要退场。
    protect_turns 只用于决定「哪些消息不可压缩」，不决定「哪些不发送」。
    """
    msgs: list[dict[str, str]] = [{"role": "system", "content": system_prompt}]

    active_blocks = [b for b in state.blocks if b.active]
    if active_blocks:
        recap = ["【早些时候】"]
        for b in active_blocks:
            recap.append(f"- ({b.topic}) {b.summary}")
        msgs.append({"role": "system", "content": "\n".join(recap)})

    for m in state.messages:
        if m.role == "system":
            continue
        msgs.append({"role": m.role, "content": m.text})

    return msgs


def append_message(
    state: GameState,
    role: str,
    text: str,
    turn: int | None = None,
    kind: str = "normal",
) -> MessageRef:
    ref = MessageRef(
        id=state.new_message_id(),
        role=role,
        text=text,
        turn=state.turn if turn is None else turn,
        tokens=estimate_tokens(text),
        kind=kind,
    )
    state.messages.append(ref)
    return ref


def last_assistant_line(state: GameState) -> str:
    for m in reversed(state.messages):
        if m.role == "assistant":
            return m.text
    return ""


def last_user_line(state: GameState, skip_last: bool = True) -> str:
    seen = 0
    for m in reversed(state.messages):
        if m.role != "user":
            continue
        seen += 1
        if skip_last and seen == 1:
            continue
        return m.text
    return ""
