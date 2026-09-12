"""平衡性测试：刷分机器人、经验曲线、长期可持续性。

这是验证「不破坏游戏平衡」的硬指标。数值设计得好不好看没用，
要看一个最优策略的机器人需要多少回合才能打满。
"""

from __future__ import annotations

from _fixtures import Runner, make_engine, make_rules, make_state

from src.scoring import Proposal
from src.engine import Engine

TARGET = 100.0


def _best_action(rules, state):
    """贪心：在冷却允许的前提下，选本回合期望收益最高的行为。"""
    best_id, best_val = None, 0.0
    for spec in rules.positive + rules.positive_minor + rules.positive_trace:
        if spec.gate is not None and state.affection < spec.gate:
            continue
        if spec.once and spec.id in state.used_once:
            continue
        from src.scoring import _cooldown_multiplier

        eff = spec.value * _cooldown_multiplier(spec.id, state, rules)
        # 门禁惩罚不参与
        if spec.fail_value is not None and state.affection < (spec.gate or 0):
            continue
        if eff > best_val:
            best_id, best_val = spec.id, eff
    return best_id


def run_bot(max_turns: int = 400, remember_gate: bool = True) -> tuple[int | None, Engine]:
    """最优策略机器人。每回合使用当前可得的最优行为，直到打满。"""
    rules = make_rules()
    st = make_state(affection=50.0)
    eng = Engine(st, rules)

    for t in range(1, max_turns + 1):
        action = _best_action(rules, st)
        if action is None:
            eng.process_turn(Proposal(direction="neutral"))
        else:
            eng.process_turn(Proposal(action_id=action, direction="positive"),
                             player_text=f"bot turn {t} action {action}")
        if st.affection >= TARGET:
            return t, eng
    return None, eng


def run_naive(max_turns: int = 600) -> tuple[int | None, Engine]:
    """笨玩家：只反复用同一个行为，且不定期搭话。"""
    rules = make_rules()
    st = make_state(affection=50.0)
    eng = Engine(st, rules)

    for t in range(1, max_turns + 1):
        if t % 7 == 0:
            eng.process_turn(Proposal(direction="neutral"))  # 冷场
        else:
            eng.process_turn(Proposal(action_id="specific_praise", direction="positive"),
                             player_text=f"naive {t}")
        if st.affection >= TARGET:
            return t, eng
    return None, eng


def run() -> int:
    r = Runner()

    # ------------------------------------------------------------ 曲线
    r.section("经验曲线（124 分制）")

    rules = make_rules()
    curve = rules.config["tier"]["curve"]
    total = sum(curve.values())
    r.eq("曲线总需求 = 124", total, 124)

    st = make_state(affection=50.0)
    eng = make_engine(st)
    r.eq("50 档位换算率 1.0（+3 行动分 = +3 好感）", eng._tier_scale(), 1.0)

    st.tier_floor = 60
    r.eq("60 档位换算率 1.5", eng._tier_scale(), 1.5)

    st.tier_floor = 90
    r.eq("90 档位换算率 4.5（最高档最慢）", eng._tier_scale(), 4.5)

    # ------------------------------------------------------------ 刷分机器人
    r.section("对抗性测试：最优策略机器人")

    turns, eng = run_bot()
    st = eng.state
    r.check("机器人最终打满 100（证明可达成）", turns is not None and st.affection >= TARGET,
            f"turns={turns} affection={st.affection}")

    if turns is not None:
        print(f"       → 最优策略需要 {turns} 回合打满")
        r.check("最少不低于 35 回合（防膨胀下限）", turns >= 35, f"turns={turns}")
        r.check("不超过 200 回合（可达性上限）", turns <= 200, f"turns={turns}")

    # ------------------------------------------------------------ 笨玩家
    r.section("对照：低效玩家")

    naive_turns, naive_eng = run_naive()
    if naive_turns is not None:
        print(f"       → 单一重复行为的玩家需要 {naive_turns} 回合")
        r.check("低效路径显著更慢（策略有区分度）",
                turns is not None and naive_turns > turns,
                f"naive={naive_turns} best={turns}")
    else:
        r.check("低效玩家在 600 回合内无法打满（说明存在有效门槛）", True)

    # ------------------------------------------------------------ 刷分护栏
    r.section("刷分护栏：单回合上限不可绕过")

    st = make_state(affection=50.0)
    eng = make_engine(st)
    # 一回合塞满高价值行为
    from src.scoring import resolve
    proposals = ["stop_on_request", "sit_with_grief", "public_advocacy", "name_her_need"]
    total_delta = 0.0
    for p in proposals:
        res = resolve(Proposal(action_id=p, direction="positive"), st, make_rules())
        total_delta += res.final_value
    r.check("单事件各自封顶在 +3", all(
        resolve(Proposal(action_id=p, direction="positive"), st, make_rules()).final_value <= 3.0
        for p in proposals))
    # 引擎侧的回合上限由「单事件上限 + 单主事件」共同保证
    before = st.affection
    eng.process_turn(Proposal(action_id="stop_on_request", direction="positive"))
    r.check("单回合好感增幅不超过 3 分", st.affection - before <= 3.0 + 1e-6,
            f"增幅={st.affection - before}")

    # ------------------------------------------------------------ 长期
    r.section("长期可持续性")

    st = make_state(affection=100.0, tier_floor=90, milestones=[50, 60, 70, 80, 90])
    eng = make_engine(st)
    for _ in range(200):
        eng.process_turn(Proposal(action_id="specific_praise", direction="positive"),
                         player_text=f"long {_}")
    r.check("满好感后不会溢出上限", st.affection <= 100.0, f"实际={st.affection}")
    r.check("满好感后仍停在高档位", st.affection >= 90.0, f"实际={st.affection}")

    return r.summary()


if __name__ == "__main__":
    raise SystemExit(run())
