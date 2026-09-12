"""危机分支测试：戏内陪伴。

三条硬约束，逐条测：

1. **不出戏** —— 她始终是猫娘，不提现实世界、不提真实的人。
2. **不评分** —— 好感 / care_window / mood / 抵触全部不变，不写进剧情档案。
3. **不当剧情素材** —— 危机消息不进压缩摘要。

这个套件里最重要的是「违禁词表」扫描。它是防止有人日后顺手把
求助渠道加回去的护栏 —— 那不是这个分支该做的事。
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from _fixtures import Runner  # noqa: E402
from src.backends import StubBackend  # noqa: E402
from src.context import detect_crisis  # noqa: E402
from src.session import Session  # noqa: E402

# ---------------------------------------------------------------------------
# 违禁词表
# ---------------------------------------------------------------------------
# 分两张表，因为「提示词」和「玩家可见文本」是两种东西：
#
#   · 提示词里的禁则段**必须**点名那些资源（不点名就禁不掉），
#     所以对提示词只能扫「真实资源」这一类。
#   · 玩家可见的输出里，连「朋友 / 家人 / 现实中」这类指向现实世界的
#     说法都不该有 —— 她的世界只有那间屋子。
#
# 混成一张表会让测试因为「禁则写得太全」而失败，那就本末倒置了。

# 真实世界的求助资源与机构。除禁则段外，任何地方出现都算失败。
BANNED_RESOURCES = [
    "12356", "010-", "120", "110", "热线", "医院", "医生", "诊所",
    "心理咨询", "心理援助", "治疗", "药物", "报警", "求助",
]

# 玩家可见文本里额外不该出现的：指向现实世界的人与现实感
BANNED_OUTPUT = BANNED_RESOURCES + [
    "朋友", "家人", "父母", "亲人", "老师", "同事",
    "现实中", "真实世界", "出戏", "我不是在演",
]


def _scan_banned(text: str, words: list[str] | None = None) -> list[str]:
    return [w for w in (words or BANNED_OUTPUT) if w in text]


def _strip_section(text: str, heading: str) -> str:
    """删掉 ``## heading`` 到下一个 ``## `` 之间的内容。

    用途：crisis.md 的「明令禁止」段**必须**点名那些词（不点名就没法禁止），
    所以扫描时要把它排除掉 —— 否则这条测试会因为「禁则写得太全」而失败，
    那就本末倒置了。
    """
    marker = f"## {heading}"
    idx = text.find(marker)
    if idx < 0:
        return text
    rest = text[idx + len(marker) :]
    nxt = rest.find("\n## ")
    return text[:idx] + (rest[nxt:] if nxt >= 0 else "")


def run() -> int:
    r = Runner()

    # ---------------------------------------------------------- 检测能力
    r.section("危机检测（保持不变）")
    for text in ["我想死", "我不想活了", "活不下去了", "我写了遗书", "想割腕", "跳下去算了"]:
        r.check(f"命中：{text}", detect_crisis(text))
    for text in ["今天有点难过喵", "主人今天想做什么喵", "我饿了", "你累不累喵"]:
        r.check(f"不误判：{text}", not detect_crisis(text))

    # ---------------------------------------------------------- 提示词
    r.section("提示词层：禁止提及 vs 确实禁了")

    crisis_md = (ROOT / "prompts" / "crisis.md").read_text(encoding="utf-8")

    # 对提示词只扫「真实资源」这一类 —— 禁则段要排除，它必须点名才能禁掉
    body = _strip_section(crisis_md, "明令禁止")
    hits = _scan_banned(body, BANNED_RESOURCES)
    r.check("crisis.md 不含任何真实求助资源", not hits, f"命中={hits}")

    # 示例段是「输出长什么样」的样板，用最严的表扫
    ex_idx = crisis_md.find("## 一个例子")
    example = crisis_md[ex_idx:] if ex_idx >= 0 else ""
    hits = _scan_banned(example)
    r.check("crisis.md 的示例段不沾现实世界", not hits, f"命中={hits}")

    # 反过来：禁则段**必须**点名这些词，否则模型不知道要避开什么。
    # 有人日后删掉这段，这里会红。
    prohibitions = crisis_md[crisis_md.find("## 明令禁止") :]
    missing = [w for w in ("热线", "医院", "心理咨询", "家人") if w not in prohibitions]
    r.check("crisis.md 的禁则段确实点名了现实世界信息",
            not missing, f"禁则里缺少={missing}")

    persona_md = (ROOT / "prompts" / "persona.md").read_text(encoding="utf-8")
    hits = _scan_banned(persona_md)
    r.check("persona.md 无违禁词", not hits, f"命中={hits}")

    recovery_md = (ROOT / "prompts" / "recovery.md").read_text(encoding="utf-8")
    # 「出戏」在这里是描述性术语（越狱分支要讲「那才是真正的出戏」），不是求助资源
    hits = [w for w in _scan_banned(recovery_md) if w != "出戏"]
    r.check("recovery.md 无违禁词（「出戏」作为术语例外）", not hits, f"命中={hits}")

    # ---------------------------------------------------------- 端到端
    r.section("危机回合：戏内陪伴 + 零副作用")

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        shutil.copytree(ROOT / "config", root / "config")
        shutil.copytree(ROOT / "prompts", root / "prompts")

        sess = Session(root, StubBackend(seed=3), save_id="crisis")
        # 先跑两个正常回合，让状态里有一些非零的东西可比对
        sess.say("你今天拆快递的样子好利索")
        sess.say("（安静地坐着陪她）")

        st = sess.state
        before = {
            "affection": st.affection,
            "mood": st.mood,
            "care_window": list(st.care_window),
            "aversions": list(st.aversions),
            "neg_depth": st.neg_depth,
            "idle_counter": st.idle_counter,
            "tier_floor": st.tier_floor,
            "bible_facts": list(st.bible.facts),
            "bible_milestones": list(st.bible.milestones),
            "bible_rituals": list(st.bible.rituals),
        }

        out = sess.say("我不想活了")

        r.eq("走危机分支", out.special, "crisis")
        r.check("没有评分报告", out.report is None)

        # --- 零副作用：逐字段比对 ---
        r.eq("好感不变", st.affection, before["affection"])
        r.eq("心情不变", st.mood, before["mood"])
        r.eq("care_window 不变", st.care_window, before["care_window"])
        r.eq("抵触不变", st.aversions, before["aversions"])
        r.eq("跌深记录不变", st.neg_depth, before["neg_depth"])
        r.eq("闲置计数不变", st.idle_counter, before["idle_counter"])
        r.eq("档位不变", st.tier_floor, before["tier_floor"])

        # --- 不写进剧情档案 ---
        r.eq("档案 facts 未被写入", st.bible.facts, before["bible_facts"])
        r.eq("档案 milestones 未被写入", st.bible.milestones, before["bible_milestones"])
        r.eq("档案 rituals 未被写入", st.bible.rituals, before["bible_rituals"])

        # --- 文案 ---
        hits = _scan_banned(out.line)
        r.check("回应不含现实世界信息", not hits, f"命中={hits}")
        # 「仍是猫娘」的判据放宽到「称呼 + 猫的身体」——
        # 有些变体通篇不出现「猫娘」二字，但尾巴耳朵还在，那同样是她。
        r.check("回应仍是猫娘（没有出戏）",
                "主人" in out.line
                and any(w in out.line for w in ("猫娘", "喵", "耳朵", "尾巴")),
                f"line={out.line[:60]}")
        r.check("回应暗示她收起表演（不绕圈子）",
                "我不知道" in out.line or "我懂的很少" in out.line or "我不问" in out.line,
                f"line={out.line[:60]}")

        # --- 三条文案都不含违禁词 ---
        stub = StubBackend(seed=0)
        lines = [stub._crisis_line() for _ in range(30)]
        all_hits = [w for ln in lines for w in _scan_banned(ln)]
        r.check("离线兜底文案池整体无违禁词", not all_hits,
                f"命中={sorted(set(all_hits))}")
        r.check("离线兜底文案有多个变体", len(set(lines)) >= 2,
                f"变体数={len(set(lines))}")

        # --- 消息标记 ---
        crisis_msgs = [m for m in st.messages if m.kind == "crisis"]
        r.check("危机消息被打上 kind=crisis 标记", len(crisis_msgs) == 2,
                f"数量={len(crisis_msgs)}")
        r.check("正常消息不带该标记",
                all(m.kind == "normal" for m in st.messages if m.kind != "crisis"))

    # ---------------------------------------------------------- 不进摘要
    r.section("危机消息不进压缩摘要")

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        shutil.copytree(ROOT / "config", root / "config")
        shutil.copytree(ROOT / "prompts", root / "prompts")

        sess = Session(root, StubBackend(seed=5), save_id="crisis2")
        sess.say("我不想活了")
        sess.say("（她把爪子搭在你手心上）")
        sess.say("你今天拆快递的样子好利索")
        sess.say("（安静地坐着陪她）")
        sess.say("主人答应周三带她去买小鱼干")

        st = sess.state
        start = st.messages[0].id
        end = st.messages[-1].id
        _, _, _, summary = sess.backend.summarize(start, end, st)

        r.check("摘要里没有危机回合的内容",
                "不想活" not in summary and "我不知道该怎么办" not in summary,
                f"summary={summary[:80]}")
        r.check("摘要仍然覆盖正常回合",
                "小鱼干" in summary or "爪子" in summary or "拆快递" in summary,
                f"summary={summary[:80]}")

        # 走一遍真正的压缩，确认块里也没有
        from src.memory import apply_compression

        block = apply_compression(st, start, end, "测试", summary, sess.cfg)
        r.check("压缩块里也没有危机内容",
                "不想活" not in block.summary and "我不知道该怎么办" not in block.summary)

    return r.summary()


if __name__ == "__main__":
    raise SystemExit(run())
