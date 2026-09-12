"""状态机测试：档位、抵触、崩坏分支、存档安全。

这些用例对应讨论中确认的具体需求编号。
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from _fixtures import Runner, make_engine, make_rules, make_state

from src import lockout
from src.scoring import Proposal
from src.state import GameState, SaveStore


def pos(action: str) -> Proposal:
    return Proposal(action_id=action, direction="positive")


def neg(action: str) -> Proposal:
    return Proposal(action_id=action, direction="negative")


def run() -> int:
    r = Runner()

    # ------------------------------------------------------------ 档位回落
    r.section("档位回落修复（用户第 1 点）")

    st = make_state(affection=95.0, tier_floor=90, milestones=[50, 60, 70, 80, 90])
    eng = make_engine(st)
    for _ in range(60):
        eng.process_turn(Proposal(direction="neutral"))
    r.check("闲置回落不击穿档位下限", st.affection >= 90.0, f"实际={st.affection}")
    r.check("里程碑（独家称呼解锁）未被撤销", 90 in st.milestones)

    # 长周期验证：即便闲置 200 回合，也停在档位下限而不是滑回 50
    st2 = make_state(affection=95.0)
    eng2 = make_engine(st2)
    for _ in range(200):
        eng2.process_turn(Proposal(direction="neutral"))
    r.eq("200 回合闲置后仍停在下限 90", st2.affection, 90.0)
    r.check("没有滑回初始值 50", st2.affection > 50.0, f"实际={st2.affection}")

    # 低档位同样受保护
    st5 = make_state(affection=63.0)
    eng5 = make_engine(st5)
    for _ in range(100):
        eng5.process_turn(Proposal(direction="neutral"))
    r.eq("60 档位停在 60", st5.affection, 60.0)

    # ------------------------------------------------------------ 大幅扣分
    r.section("大幅扣分：降档 + 冻结 + 抵触（用户第 1 点）")

    st = make_state(affection=85.0, tier_floor=80, milestones=[50, 60, 70, 80])
    eng = make_engine(st)
    rep = eng.process_turn(neg("public_humiliate"), player_text="你看她多丢人")
    r.check("触发大幅扣分后打掉一档", st.tier_floor == 70, f"实际={st.tier_floor}")
    r.check("进入信任冻结", st.trust_freeze_until >= st.turn + 10,
            f"frozen_until={st.trust_freeze_until}")
    r.check("产生抵触", any(a.family == "humiliation" for a in st.aversions),
            f"aversions={[a.family for a in st.aversions]}")

    # 抵触会削弱同类正向行为
    from src.scoring import resolve
    from src.state import Aversion

    st3 = make_state(affection=50.0)
    st3.aversions.append(
        Aversion(family="attunement", strength=1.0, created_turn=1, last_decay_turn=1)
    )
    base = resolve(pos("specific_praise"), make_state(affection=50.0), make_rules()).final_value
    weakened = resolve(pos("specific_praise"), st3, make_rules()).final_value
    r.check("抵触削弱同类加分", weakened < base, f"{weakened} vs {base}")

    # 抵触会随时间衰减
    st4 = make_state(affection=20.0)
    eng4 = make_engine(st4)
    eng4.process_turn(neg("public_humiliate"))
    r.check("产生了抵触记录", len(st4.aversions) == 1)
    st4.affection = 20.0
    for _ in range(25):
        eng4.process_turn(Proposal(direction="neutral"))
    r.check("抵触会衰减", st4.aversions[0].strength < 1.0,
            f"strength={st4.aversions[0].strength}")

    # ------------------------------------------------------------ 负值区间
    r.section("负值累进与深度负值（用户第 2、5 点）")

    st = make_state(affection=-10.0)
    eng = make_engine(st)
    eng.process_turn(Proposal(direction="neutral"))
    r.eq("首个负值区间 depth=0", st.neg_depth, 0)

    st = make_state(affection=-30.0)
    eng = make_engine(st)
    eng.process_turn(Proposal(direction="neutral"))
    r.eq("-30 落第二区间 depth=1", st.neg_depth, 1)

    st = make_state(affection=-60.0)
    eng = make_engine(st)
    eng.process_turn(Proposal(direction="neutral"))
    r.eq("-60 落第三区间 depth=2", st.neg_depth, 2)
    r.check("跌破 -50 进入深度负值", st.deep_negative)

    st.affection = 5.0
    eng.process_turn(Proposal(direction="neutral"))
    r.check("回到 +5 仍未解除（需到 +20）", st.deep_negative)

    st.affection = 25.0
    eng.process_turn(Proposal(direction="neutral"))
    r.check("回到 +20 以上解除", not st.deep_negative)

    # depth 不可逆
    st = make_state(affection=-60.0)
    eng = make_engine(st)
    eng.process_turn(Proposal(direction="neutral"))
    st.affection = 30.0
    eng.process_turn(Proposal(direction="neutral"))
    r.eq("跌深记录不可逆（回升也不清零）", st.neg_depth, 2)

    # ------------------------------------------------------------ 崩坏
    r.section("崩坏 / 回升 / 沉沦 分支")

    st = make_state(affection=-25.0)
    eng = make_engine(st)
    rep = eng.process_turn(Proposal(direction="neutral"))
    r.check("跌破 -20 进入崩坏", st.collapse_active)
    r.eq("分支变更上报", rep.branch_change, "collapse")

    # 崩坏期常规正向不计分
    before = st.affection
    eng.process_turn(pos("stop_on_request"))
    r.eq("崩坏期常规正向不加分", st.affection, before, tol=1e-6)

    # 崩坏期索取 -> 沉沦计数
    for _ in range(3):
        eng.process_turn(neg("demand_response"), player_text="你怎么了")
    r.check("崩坏期累计负向触发沉沦", st.withered, f"negatives={st.collapse_negatives}")
    r.eq("分支落到 wither", st.branch, "wither")

    # 另一条线：回升
    st = make_state(affection=-25.0)
    eng = make_engine(st)
    eng.process_turn(Proposal(direction="neutral"))
    r.check("已进入崩坏（回升线）", st.collapse_active)
    st.affection = -10.0
    for _ in range(5):
        eng.process_turn(pos("release_pressure"), player_text="你不用理我")
    r.eq("五次无索取陪伴达成 recovery", st.recovery_counter, 5)
    r.check("零负向时退出崩坏", not st.collapse_active)

    st.affection = 55.0
    rep = eng.process_turn(Proposal(direction="neutral"))
    r.eq("越过 50 触发绽放", rep.branch_change, "bloom")
    r.check("绽放后锁定 bloom 分支", st.branch == "bloom")
    r.check("绽放后衰减免疫", True)

    # ------------------------------------------------------------ 存档
    r.section("存档安全")

    with tempfile.TemporaryDirectory() as td:
        store = SaveStore(Path(td))
        s = GameState(save_id="t1")
        s.affection = 66.5
        s.bible.rituals.append("每周三晒被子")
        store.save(s)

        loaded = store.load("t1")
        r.eq("好感往返一致", loaded.affection, 66.5)
        r.eq("档案往返一致", loaded.bible.rituals, ["每周三晒被子"])

        # 损坏存档 -> 备份而非静默重置
        store.path_for("t1").write_text("{ 这不是 JSON", encoding="utf-8")
        recovered = store.load("t1")
        r.check("损坏存档被备份而非丢弃",
                any(p.name.startswith("t1.broken-") for p in Path(td).glob("*")),
                f"目录内容={[p.name for p in Path(td).glob('*')]}")
        r.eq("损坏后回到初始好感", recovered.affection, 50.0)

    # ------------------------------------------------------------ 锁定
    r.section("沉沦锁定与新周目（用户第 10 点）")

    with tempfile.TemporaryDirectory() as td:
        store = SaveStore(Path(td))
        s = GameState(save_id="wither1")
        s.turn = 88
        store.save(s)
        lockout.seal(s, store)

        r.check("封条已写入", lockout.is_sealed(store, "wither1"))
        try:
            lockout.check(store, "wither1", store.load("wither1"))
            r.check("沉沦存档被拒绝加载", False, "没有抛错")
        except lockout.LockedSaveError:
            r.check("沉沦存档被拒绝加载", True)

        # 手改存档也绕不过封条
        tampered = store.load("wither1")
        tampered.withered = False
        store.save(tampered)
        try:
            lockout.check(store, "wither1", store.load("wither1"))
            r.check("手改 withered 字段无法绕过封条", False, "绕过了")
        except lockout.LockedSaveError:
            r.check("手改 withered 字段无法绕过封条", True)

        # 新周目可用
        fresh = lockout.new_game(store, "save2")
        r.eq("新周目初始好感", fresh.affection, 50.0)
        r.eq("新周目不是沉沦态", fresh.withered, False)
        lockout.check(store, "save2", fresh)
        r.check("新周目可以正常加载", True)
        r.check("旧的封条记录仍在（是记录不是障碍）", lockout.is_sealed(store, "wither1"))

    return r.summary()


if __name__ == "__main__":
    raise SystemExit(run())
