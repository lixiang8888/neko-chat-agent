"""评分引擎测试：十条护栏 + 三轴公式 + 状态系数。

这是保障「LLM 自主判分但不破坏平衡」的核心防线。
"""

from __future__ import annotations

from _fixtures import Runner, make_rules, make_state, score

from src.scoring import Proposal, _lower_step, is_echo, resolve


def run() -> int:
    r = Runner()
    rules = make_rules()

    # ---------------------------------------------------------------- 白名单
    r.section("白名单与门禁")

    st = make_state(affection=50.0)
    r.eq("T1 行为给满分", score("stop_on_request", st), 3.0)

    st = make_state(affection=50.0)
    r.eq("门禁未达倒扣（表达想念需 55）", score("declare_affection", st), -2.0)

    st = make_state(affection=60.0)
    r.eq("门禁达标正常给分", score("declare_affection", st), 2.0)

    st = make_state(affection=30.0)
    r.eq("低好感触碰尾巴类倒扣", score("grab_tail", st), -6.0)

    # ---------------------------------------------------------------- 冷却
    r.section("冷却递减")

    st = make_state(affection=50.0)
    st.turn = 100
    st.cooldowns["specific_praise"] = [99]
    r.eq("窗口内第 2 次 ×0.5", score("specific_praise", st), 1.0)

    st.cooldowns["specific_praise"] = [99, 98]
    r.eq("窗口内第 3 次 ×0.25", score("specific_praise", st), 0.5)

    st.cooldowns["specific_praise"] = [99, 98, 97]
    r.eq("窗口内第 4 次归零", score("specific_praise", st), 0.0)

    st.cooldowns["specific_praise"] = [70]  # 30 回合前，超出窗口
    r.eq("冷却已过恢复满分", score("specific_praise", st), 2.0)

    # ---------------------------------------------------------------- 复述
    r.section("复述惩罚")

    r.check("精确复述她的上一句 → 判为复述",
            is_echo("我哪儿都不去喵", "主人，我今天想了很久喵。我哪儿都不去喵。", ""))
    r.check("复制自己上一句 → 判为复述",
            is_echo("你今天回来啦", "", "你今天回来啦"))
    r.check("正常回应不误判",
            not is_echo("要不要我陪你一会儿", "我有点难过喵", "晚安"))

    st = make_state(affection=50.0)
    res = resolve(
        Proposal(action_id="specific_praise", direction="positive"),
        st, rules, player_text="你今天拆快递的样子好利索",
        her_last_line="你今天拆快递的样子好利索", last_player_text="",
    )
    r.eq("复述惩罚把分数打成 0", res.final_value, 0.0)

    # ---------------------------------------------------------------- 截断
    r.section("截断与吸收")

    st = make_state(affection=50.0)
    res = resolve(Proposal(action_id="stop_on_request", direction="positive",
                           axes={"cost": 2, "specificity": 2, "timing": 2}), st, rules)
    r.eq("单事件硬上限 +3", res.final_value, 3.0)

    # ---------------------------------------------------------------- 三轴
    r.section("三轴兜底公式")

    st = make_state(affection=50.0)
    res = resolve(Proposal(direction="positive", axes={"cost": 2, "specificity": 2, "timing": 2}),
                  st, rules)
    r.eq("三轴满 6 分 → +3", res.final_value, 3.0)

    res = resolve(Proposal(direction="positive", axes={"cost": 0, "specificity": 0, "timing": 1}),
                  st, rules)
    r.eq("三轴 1 分 → +0.5", res.final_value, 0.5)

    res = resolve(Proposal(direction="positive", axes={"cost": 0, "specificity": 0, "timing": 0}),
                  st, rules)
    r.eq("三轴 0 分 → 0", res.final_value, 0.0)

    res = resolve(Proposal(direction="negative", axes={"depth": 2, "targeting": 2, "timing": 2}),
                  st, rules)
    r.eq("负向三轴满 → -25", res.final_value, -25.0)

    # ---------------------------------------------------------------- 取低档
    r.section("不确定取低一档（方向只向下）")

    r.eq("+3 降为 +2", _lower_step(3.0), 2.0)
    r.eq("+2 降为 +1", _lower_step(2.0), 1.0)
    r.eq("-15 降为 -12（少伤害）", _lower_step(-15.0), -12.0)
    r.eq("-25 降为 -18", _lower_step(-25.0), -18.0)

    st = make_state(affection=50.0)
    res = resolve(Proposal(action_id="stop_on_request", direction="positive", uncertain=True),
                  st, rules)
    r.eq("标了不确定就降档", res.final_value, 2.0)

    # ---------------------------------------------------------------- 高位惩罚
    r.section("高位惩罚系数")

    st = make_state(affection=72.0)
    r.eq("≥70 时 -14 ×1.5", score("cold_war", st), -21.0)

    st = make_state(affection=88.0)
    r.eq("≥85 时 -14 ×2", score("cold_war", st), -28.0)

    st = make_state(affection=88.0)
    r.eq("高位小节扣分不放大（只放 ≥-6）", score("perfunctory", st), -1.0)

    # ---------------------------------------------------------------- 状态系数
    r.section("状态系数")

    st = make_state(affection=-30.0, deep_negative=False)
    r.eq("警告期正向 ×0.7", score("specific_praise", st), 1.4)

    st = make_state(affection=-60.0, deep_negative=True, probation=0)
    r.eq("深度负值初始 ×0.3×预热0 → 0", score("specific_praise", st), 0.0)

    st = make_state(affection=-60.0, deep_negative=True, probation=5)
    r.eq("预热满后 ×0.3", score("specific_praise", st), 0.6)

    st = make_state(affection=-30.0, deep_negative=False)
    r.eq("-50~-20 之间难度不变（用户第 5 点）", score("specific_praise", st), 1.4)

    # ---------------------------------------------------------------- 累进
    r.section("负值累进放大")

    st = make_state(affection=-30.0, neg_depth=1)
    r.eq("depth=1 时 -6 ×1.5", score("grab_tail", st), -9.0)

    st = make_state(affection=-60.0, neg_depth=2)
    r.eq("depth=2 时 -6 ×2.0", score("grab_tail", st), -12.0)

    # ---------------------------------------------------------------- 冻结
    r.section("信任冻结")

    st = make_state(affection=50.0)
    st.turn = 10
    st.trust_freeze_until = 20
    r.eq("冻结期正向不计分", score("stop_on_request", st), 0.0)

    # ---------------------------------------------------------------- 崩坏
    r.section("崩坏期分流")

    st = make_state(affection=-30.0)
    st.collapse_active = True
    r.eq("崩坏期常规正向失效", score("stop_on_request", st), 0.0)

    st = make_state(affection=-30.0)
    st.collapse_active = True
    r.eq("崩坏期专用行为照常给分", score("release_pressure", st), 3.0)

    st = make_state(affection=-30.0)
    st.collapse_active = True
    r.eq("崩坏期索取回应倒扣", score("demand_response", st), -3.0)

    # ---------------------------------------------------------------- 归一化
    r.section("语义归一化")

    from src.scoring import normalize

    hit = normalize("你就这么过去吧，我不勉强你", rules)
    r.check("锚点命中「不勉强」", hit is not None and hit[0] == "stop_on_request",
            f"实际={hit}")
    hit2 = normalize("嗯", rules)
    r.check("敷衍被识别", hit2 is not None and hit2[0] == "perfunctory", f"实际={hit2}")
    hit3 = normalize("今天天气不错然后我去超市买了点东西顺便修了下自行车", rules)
    r.check("中性长文本不误命中", hit3 is None, f"实际={hit3}")

    return r.summary()


if __name__ == "__main__":
    raise SystemExit(run())
