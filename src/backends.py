"""LLM 后端：可插拔的演出与判分层。

两个实现：
- ``StubBackend``：离线、纯规则。用于测试、调参和没有 API Key 时跑通全流程。
- ``LLMBackend``：任意 OpenAI 兼容端点（本工程面向 DeepSeek）。

**采样参数按 profile 分包**：演出要高温（1.3），判分要低温（0.0）。
这不是调参偏好，是两类任务的性质不同 —— 演出要表现力，判分要确定性。
profile 的具体数值在 ``config/llm.json``。
"""

from __future__ import annotations

import random
from typing import Any, Iterator

from .deepseek import CONTENT, REASONING, DeepSeekClient, extract_json
from .scoring import Proposal, Rules, normalize
from .state import GameState, tone_profile


# --------------------------------------------------------------------------
# 离线后端
# --------------------------------------------------------------------------


class StubBackend:
    """不调用任何模型。用模板加锚点匹配模拟一个完整回合。

    存在的意义：让评分引擎、状态机、记忆层可以独立测试。
    真实演出质量取决于在线后端，但**所有数值逻辑在离线就能验证** ——
    这是整个工程 138 项测试不需要 API Key 的原因。
    """

    def __init__(self, seed: int | None = None) -> None:
        self.rng = random.Random(seed)
        self.state: GameState | None = None
        self.rules: Rules | None = None

    def bind(self, state: GameState) -> None:
        self.state = state

    def attach_rules(self, rules: Rules) -> None:
        self.rules = rules

    # -- 演出 ------------------------------------------------------------

    def speak(self, messages: list[dict[str, str]], *, profile: str = "speak") -> str:
        if profile == "crisis":
            return self._crisis_line()
        return self._line()

    def speak_stream(
        self, messages: list[dict[str, str]], *, profile: str = "speak"
    ) -> Iterator[tuple[str, str]]:
        """离线后端没有真流式，整块吐出。

        但仍然实现这个接口 —— 会话层因此不需要区分后端有没有流式能力。
        """
        yield CONTENT, self.speak(messages, profile=profile)

    def _line(self) -> str:
        st = self.state
        tone = tone_profile(st) if st else {"label": "天生好感"}
        label = tone.get("label", "天生好感")

        if st is not None and getattr(st, "withered", False):
            return "嗯，我知道了喵。"
        if label == "崩坏":
            return "……嗯。"
        if label == "敌意":
            return "别过来。"

        pool = {
            "天生好感": [
                "主人回来啦喵~ 我一直在窗台上等你哦喵。",
                "唔…你今天回来得有点晚喵，我、我才没有一直在看门喵。",
            ],
            "警惕": [
                "……有事吗喵。",
                "我在的喵。你说吧喵。",
            ],
            "依赖": [
                "主人主人，今天发生了一件事喵！你听不听喵？",
                "（尾巴绕过来搭在你手腕上）……就一会儿喵，一下下就好喵。",
            ],
            "独属": [
                "你今天没有先来找我喵。我不高兴了喵，要哄的喵。",
                "（凑过来嗅了嗅）嗯…是主人的味道喵。可以了喵。",
            ],
            "认定": [
                "主人，我今天想了很久喵。我哪儿都不去喵。",
                "（把脸埋进你袖子里）……你在就很好喵。",
            ],
            "绽放": [
                "主人主人你看喵！今天的太阳好大，我把窗帘全拉开了喵~♡",
                "我把被子和垫子都晒过了喵！今天会有太阳的味道哦喵~",
            ],
        }
        return self.rng.choice(pool.get(label, pool["天生好感"]))

    # -- 危机（戏内陪伴）------------------------------------------------

    def _crisis_line(self) -> str:
        """危机分支的离线兜底。

        ⚠️ 三条硬约束，改动前先读：
        1. **不出戏** —— 她始终是猫娘，不提现实世界、不提真实的人。
        2. **不给建议、不追问、不说教、不评判**。
        3. **不含任何现实世界信息**（求助热线、医院、心理咨询、家人朋友）。
           ``tests/test_crisis.py`` 会用违禁词表扫描这段文案。

        手法：她**停止表演** —— 动作变少、不绕圈子、不撒娇，但仍然是猫娘。
        """
        return self.rng.choice(
            [
                "（她没有像平时那样扑过来。）\n\n"
                "（耳朵慢慢放平了，尾巴不再摇。她只是走过来，在你旁边坐下，\n"
                "把头轻轻抵在你手背上。很久没有说话。）\n\n"
                "主人。\n\n"
                "我不知道该怎么办。我只是一只猫娘，我懂的事情很少。\n\n"
                "但我知道你现在很不好。\n\n"
                "（她把整个身子贴过来，像要把自己按进你怀里。）\n\n"
                "你不用告诉我为什么。我不问。\n\n"
                "你现在只要做一件事就够了 ——\n"
                "你先呼吸。跟着我，一下，一下。\n\n"
                "（你能感觉到她背上很轻的起伏。）\n\n"
                "我哪儿也不去。太阳落了我就陪你到天黑，天黑了我就陪你到天亮。\n\n"
                "你先把这一会儿过完。",

                "（她本来在窗台上，听到你的话，慢慢回过头。）\n\n"
                "（没有接话。她从窗台跳下来，走到你脚边，把额头抵在你膝盖上。\n"
                "尾巴垂着，一动不动。）\n\n"
                "……\n\n"
                "主人。\n\n"
                "我懂的很少。我不会说那些好听的话。\n\n"
                "（她抬起头看你，眼睛很亮，没有一丝在闹的样子。）\n\n"
                "可是这里有你，有太阳，有晒过的被子。\n"
                "今天它们都还在。\n\n"
                "我不问你怎么了。你就先在这儿坐一会儿。\n"
                "坐多久都行。\n\n"
                "（她把爪子搭在你手心上，不动了。）\n\n"
                "我把我的温度分你一点。就一点。够你撑过今晚的。",

                "（她正在舔爪子，听到你的声音，动作停在半空。）\n\n"
                "（然后她慢慢把爪子放下，走过来，用脑袋顶了顶你的手。）\n\n"
                "主人。\n\n"
                "（她很少这样叫你。不带喵，不带撒娇，就是叫你。）\n\n"
                "我不知道怎么说才好。真不知道。\n\n"
                "（她绕着你的脚走了一圈，又停下来，靠上去。）\n\n"
                "我只知道你现在很难受。我不问为什么。\n\n"
                "你不用说话，也不用解释。\n"
                "你只要知道 —— 这屋里还有一只猫在等你。\n\n"
                "（她蜷在你脚边，把自己团成很小的一团。）\n\n"
                "哪儿也别去。就在这儿待着。\n"
                "我陪着你，一秒一秒地陪。",
            ]
        )

    # -- 判分 ------------------------------------------------------------

    def judge(self, prompt: str, player_text: str) -> Proposal:
        if self.rules is None:
            return Proposal()
        hit = normalize(player_text, self.rules)
        if hit is None:
            return Proposal(direction="neutral", reason="未命中")
        action_id, strength = hit
        spec = self.rules.by_id(action_id)
        direction = "positive" if (spec and spec.value > 0) else "negative"
        return Proposal(
            action_id=action_id,
            direction=direction,
            is_primary=True,
            uncertain=strength < 0.5,
            reason=f"锚点命中 {action_id}（强度 {strength:.2f}）",
        )

    # -- 摘要 ------------------------------------------------------------

    def summarize(self, start: str, end: str, state: GameState) -> tuple[str, str, str, str]:
        # 危机回合不进摘要 —— 见 LLMBackend.summarize 的说明
        covered = [
            m for m in state.messages if start <= m.id <= end and m.kind != "crisis"
        ]
        who = {"user": "主人", "assistant": "她"}
        bits = []
        for m in covered[:12]:
            snippet = m.text.replace("\n", " ")[:40]
            bits.append(f"{who.get(m.role, m.role)}说「{snippet}」")
        summary = "；".join(bits) if bits else "这段时间没有什么特别的事。"
        return start, end, "日常片段", summary[:800]


# --------------------------------------------------------------------------
# 在线后端
# --------------------------------------------------------------------------


class LLMBackend:
    """任意 OpenAI 兼容端点（面向 DeepSeek）。

    采样参数从 ``config/llm.json`` 按 profile 取，不在这里硬编码。
    """

    def __init__(self, client: DeepSeekClient, llm_cfg: dict[str, Any]) -> None:
        self.client = client
        self.cfg = llm_cfg
        self.state: GameState | None = None
        self.rules: Rules | None = None
        self.last_error: str | None = None

    def bind(self, state: GameState) -> None:
        self.state = state

    def attach_rules(self, rules: Rules) -> None:
        self.rules = rules

    def _profile(self, name: str) -> dict[str, Any]:
        return dict(self.cfg.get(name) or {})

    # -- 演出 ------------------------------------------------------------

    def speak(self, messages: list[dict[str, str]], *, profile: str = "speak") -> str:
        p = self._profile(profile)
        try:
            return self.client.chat(
                messages,
                temperature=p.get("temperature", 1.3),
                max_tokens=p.get("max_tokens", 800),
                top_p=p.get("top_p"),
                reasoning_effort=p.get("reasoning_effort", "low"),
            ).strip()
        except Exception:  # noqa: BLE001 - 演出失败不该中断游戏
            return "（她张了张嘴，但什么也没说出来喵……）"

    def speak_stream(
        self, messages: list[dict[str, str]], *, profile: str = "speak"
    ) -> Iterator[tuple[str, str]]:
        p = self._profile(profile)
        try:
            yield from self.client.stream(
                messages,
                temperature=p.get("temperature", 1.3),
                max_tokens=p.get("max_tokens", 800),
                top_p=p.get("top_p"),
                reasoning_effort=p.get("reasoning_effort", "low"),
            )
        except Exception:  # noqa: BLE001
            # 流已经开始就不能再插一句降级文案（会和已打印的正文粘在一起），
            # 但完全没出字时补一句，免得玩家对着空屏发呆。
            yield CONTENT, "（她张了张嘴，但什么也没说出来喵……）"

    # -- 判分 ------------------------------------------------------------

    def judge(self, prompt: str, player_text: str) -> Proposal:
        p = self._profile("judge")
        try:
            raw = self.client.chat(
                [
                    {"role": "system", "content": prompt},
                    {"role": "user", "content": player_text},
                ],
                temperature=p.get("temperature", 0.0),
                max_tokens=p.get("max_tokens", 200),
                reasoning_effort=p.get("reasoning_effort", "none"),
            )
            return Proposal.from_llm_json(extract_json(raw))
        except Exception:  # noqa: BLE001
            # 降级：模型判分失败时退回锚点匹配。宁可粗糙，不能开天窗。
            if self.rules is not None:
                hit = normalize(player_text, self.rules)
                if hit:
                    spec = self.rules.by_id(hit[0])
                    return Proposal(
                        action_id=hit[0],
                        direction="positive" if (spec and spec.value > 0) else "negative",
                        reason="降级为锚点匹配",
                    )
            return Proposal(direction="neutral", reason="判分失败")

    # -- 摘要 ------------------------------------------------------------

    def summarize(self, start: str, end: str, state: GameState) -> tuple[str, str, str, str]:
        # 危机回合（kind="crisis"）**不进摘要**。
        # 这是「只通过摘要退场，绝不静默丢弃」原则的一处**刻意例外**：
        # 那些消息照常存在存档里（她确实会记得），但绝不进入摘要块 ——
        # 否则它会变成一段可以被继续引用的「剧情」，而那不是剧情。
        covered = [
            m for m in state.messages if start <= m.id <= end and m.kind != "crisis"
        ]
        transcript = "\n".join(
            f"[{m.id}] {'主人' if m.role == 'user' else '她'}：{m.text}" for m in covered
        )
        prompt = (
            "把下面这段对话压缩成摘要，用第二人称现在时。\n"
            "必须保留：她说过的原话（优先于转述）、关系变化、未兑现的承诺、"
            "她的偏好与雷区、玩家做过的事及其后果。\n"
            "可以丢弃：寒暄、重复的日常。\n\n"
            '只输出 JSON：{"topic": "...", "summary": "..."}\n\n'
            f"对话：\n{transcript[:6000]}"
        )
        p = self._profile("summarize")
        try:
            raw = self.client.chat(
                [{"role": "user", "content": prompt}],
                temperature=p.get("temperature", 0.0),
                max_tokens=p.get("max_tokens", 800),
                reasoning_effort=p.get("reasoning_effort", "none"),
            )
            d = extract_json(raw)
            return start, end, d.get("topic", "片段"), d.get("summary", "")
        except Exception:  # noqa: BLE001 - 摘要失败不该吞掉整段历史
            return start, end, "片段", "（摘要生成失败，保留原始区间未压缩）"


__all__ = ["StubBackend", "LLMBackend", "CONTENT", "REASONING"]
