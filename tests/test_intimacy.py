"""露骨内容档位门测试。

设计要点：**按 ``tier_floor`` 判定，不按 ``affection``。**

理由是档位特性本身 —— ``tier_floor`` 只升不降、不可撤销，天然是「解锁」语义；
而 ``affection`` 会因闲置回落波动。用 affection 判定会出现「解锁了又锁回去」，
正是 ``engine._tick_idle()`` 里专门修掉的那类 bug（好感 95 闲置 200 回合
掉到 90，把「独家称呼」解锁撤销掉）。

第二件要测的事：门槛**不能以数字形式进提示词**。模型看不到 tier_floor，
一句「好感达到 80 才行」它无法执行。程序算成布尔结论再注入，才是强制里程碑。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from _fixtures import Runner, make_engine, make_rules, make_state  # noqa: E402
from src.context import PromptBuilder  # noqa: E402
from src.state import intimacy_allowed, tone_profile  # noqa: E402


def run() -> int:
    r = Runner()
    cfg = make_rules().config
    gate = cfg["gates"]["intimate"]
    pb = PromptBuilder(ROOT / "prompts")

    r.eq("门槛来自配置", gate, 80)

    # ---------------------------------------------------------- 判定
    r.section("按档位判定")
    r.check(f"tier_floor=70 (<{gate}) → 未解锁",
            not intimacy_allowed(make_state(tier_floor=70, affection=99), gate))
    r.check(f"tier_floor={gate-1} → 未解锁",
            not intimacy_allowed(make_state(tier_floor=gate - 1, affection=99), gate))
    r.check(f"tier_floor={gate} → 已解锁",
            intimacy_allowed(make_state(tier_floor=gate, affection=0), gate))
    r.check("tier_floor=90 → 已解锁",
            intimacy_allowed(make_state(tier_floor=90, affection=10), gate))

    # ---------------------------------------------------------- 不可撤销
    r.section("档位不可撤销（这条是重点）")

    st = make_state(tier_floor=gate, affection=95)
    r.check("刚够档位时已解锁", intimacy_allowed(st, gate))

    # 闲置回落：好感掉回门槛以下，但档位下限不动
    st.affection = 75.0
    r.check("好感回落到 75 后**仍然**解锁（tier_floor 没降）",
            intimacy_allowed(st, gate),
            f"tier_floor={st.tier_floor} affection={st.affection}")

    st.affection = 60.0
    r.check("好感继续回落到 60 也仍然解锁", intimacy_allowed(st, gate))

    # 对照：如果按 affection 判定，上面两条都会失败 —— 这正是不用它的原因
    r.check("对照：affection=75 < 门槛，说明用 affection 判会锁回去",
            75.0 < gate)

    # ---------------------------------------------------------- 崩坏
    r.section("崩坏/沉沦期")

    st = make_state(tier_floor=gate, affection=-30, collapse_active=True)
    r.check("崩坏期不影响档位解锁状态", intimacy_allowed(st, gate))

    st = make_state(tier_floor=gate, withered=True)
    tone = tone_profile(st, gate)
    r.eq("沉沦期的 tone 不含亲密解锁", tone["intimacy_allowed"], False)
    r.eq("沉沦期标签", tone["label"], "沉沦")

    # ---------------------------------------------------------- tone_profile
    r.section("tone_profile 输出")

    for floor in (50, 60, 70, 80, 90):
        st = make_state(tier_floor=floor, affection=float(floor))
        tone = tone_profile(st, gate)
        r.check(f"tier_floor={floor} 的 tone 带 intimacy_allowed 字段",
                "intimacy_allowed" in tone)
        r.eq(f"tier_floor={floor} 的解锁状态与 intimacy_allowed 一致",
                tone["intimacy_allowed"], floor >= gate)

    st = make_state(tier_floor=50, affection=50.0)
    tone = tone_profile(st, gate)
    r.check("原有字段没被破坏",
            {"label", "initiative", "nya_visible", "body_language", "note"} <= set(tone))

    # ---------------------------------------------------------- 注入
    r.section("注入提示词的是结论，不是数字")

    st = make_state(tier_floor=70, affection=75.0)
    eng = make_engine(st)
    text = pb.system_prompt(st, eng, inject_style=False)

    r.check("{intimacy} 占位符已被替换", "{intimacy}" not in text)
    r.check("未解锁时给的是「会岔开话题」版",
            "岔开" in text and "炸毛" in text, f"片段={text[-300:]}")
    r.check("未解锁时文案里没有出现门槛数字",
            "80" not in text.split("## 输出格式")[0].split("有分寸")[-1][:200],
            "「有分寸」那条不该出现 80")

    st = make_state(tier_floor=90, affection=95.0)
    eng = make_engine(st)
    text = pb.system_prompt(st, eng, inject_style=False)

    r.check("已解锁时给的是「不必假装」版",
            "不必再假装" in text or "没有那层顾虑" in text, f"片段={text[-300:]}")
    r.check("已解锁时不再出现「岔开话题」的描述", "岔开" not in text)

    # ---------------------------------------------------------- 全槽位
    r.section("提示词槽位完整性")

    st = make_state(tier_floor=50, affection=50.0)
    eng = make_engine(st)
    text = pb.system_prompt(st, eng, inject_style=False)
    leftovers = [s for s in
                 ("{tone}", "{mood}", "{desire}", "{aversions}", "{bible}", "{intimacy}")
                 if s in text]
    r.check("全部占位符都被替换（没有漏配的槽位）", not leftovers,
            f"残留={leftovers}")

    # 数值不该出现在提示词里
    st = make_state(tier_floor=90, affection=78.4)
    eng = make_engine(st)
    text = pb.system_prompt(st, eng, inject_style=False)
    r.check("好感数值没有进提示词", "78.4" not in text)
    r.check("档位数值没有进提示词", "tier_floor" not in text)
    r.check("{affection} 这类旧槽位已从提示词里移除", "{affection}" not in text)

    return r.summary()


if __name__ == "__main__":
    raise SystemExit(run())
