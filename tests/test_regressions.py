"""回归测试：锁住两个真实出现过的 bug。

这个文件不测「功能对不对」，测的是**修好的东西别再坏回去**。
每个用例都对应一次实际发生的问题，注释里写清楚当时是什么现象。
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from _fixtures import Runner, make_engine, make_state  # noqa: E402
from src.backends import StubBackend  # noqa: E402
from src.context import PromptBuilder  # noqa: E402
from src.scoring import Proposal  # noqa: E402
from src.session import Session  # noqa: E402
from src.state import GameState, tone_profile  # noqa: E402


# ---------------------------------------------------------------------------
# bug 1：负分被档位下限吃掉，开局做坏事好感不掉
# ---------------------------------------------------------------------------
# 现象：新存档连续做 -2 的事，好感一直停在 50.0，delta 恒为 +0.00。
# 根因：tier_floor 初始就是 50，恰好等于初始好感，而 _floor_guard 规定
#       「任何下降都不能击穿档位下限，除非单次 ≤ -12」——于是 max(48, 50) = 50，
#       负分全部被吃掉，只有一次扣满 -12 才能推动。
# 修法：档位保护只用于**被动损耗**（闲置回落 / 愿望落空），
#       玩家行为造成的扣分照常落地。


def _test_negative_lands(r: Runner) -> None:
    r.section("回归 · 玩家行为造成的扣分真的落地")

    st = make_state()
    eng = make_engine(st)
    before = st.affection
    rep = eng.process_turn(Proposal(action_id="interrupt", direction="negative"))

    r.check("负分事件让好感下降", st.affection < before,
            f"affection {before} → {st.affection}")
    r.check("delta 不是 0", rep.delta != 0.0, f"delta={rep.delta}")

    # 连续做坏事应该持续走低（冷却容许的范围内）
    st2 = make_state()
    eng2 = make_engine(st2)
    deltas = []
    for _ in range(3):
        deltas.append(eng2.process_turn(
            Proposal(action_id="interrupt", direction="negative")).delta)
    r.check("连续负向持续走低", all(d < 0 for d in deltas), f"deltas={deltas}")
    r.check("累计降幅符合预期", st2.affection < before - 2.0,
            f"affection={st2.affection}")

    # 语气必须跟着变 —— 这是用户实际看到的现象
    st3 = make_state()
    eng3 = make_engine(st3)
    r.eq("开局语气是「天生好感」", tone_profile(st3)["label"], "天生好感")
    for _ in range(3):
        eng3.process_turn(Proposal(action_id="interrupt", direction="negative"))
    r.eq("做了坏事后语气转为「警惕」", tone_profile(st3)["label"], "警惕")

    # 正分仍然正常（别把修复做过头）
    st4 = make_state()
    eng4 = make_engine(st4)
    rep4 = eng4.process_turn(Proposal(action_id="stop_on_request", direction="positive"))
    r.check("正分仍然正常加分", st4.affection > 50.0 and rep4.delta > 0,
            f"affection={st4.affection} delta={rep4.delta}")


def _test_passive_still_guarded(r: Runner) -> None:
    """修复不能误伤档位保护 —— 被动损耗仍须被挡在档位内。"""
    r.section("回归 · 被动损耗仍受档位保护")

    st = make_state(affection=95.0, tier_floor=90)
    eng = make_engine(st)
    for _ in range(60):
        eng._tick_idle([])
    r.check("闲置回落不跨过档位下限", st.affection >= 90.0,
            f"affection={st.affection}（应停在 90）")

    # 愿望落空同理
    st2 = make_state(affection=95.0, tier_floor=90)
    eng2 = make_engine(st2)
    st2.desire = "want_petting"
    st2.desire_expires = 0
    eng2._expire_desire([])
    r.check("愿望落空不跨过档位下限", st2.affection >= 90.0,
            f"affection={st2.affection}")


# ---------------------------------------------------------------------------
# bug 2：她说自己的名字是存档 id（default）
# ---------------------------------------------------------------------------
# 现象：对话提示符和状态显示的是 "default >"，那是存档文件名。
# 修法：GameState 增加 her_name，开新周目时可指定/询问。


def _test_her_name(r: Runner) -> None:
    r.section("回归 · 猫娘有自己的名字，不是存档 id")

    r.eq("默认名字", GameState().her_name, "猫娘")
    r.eq("可以指定名字", GameState(her_name="小雨").her_name, "小雨")

    # 存档往返
    st = GameState(save_id="s1", her_name="小雨")
    back = GameState.from_dict(st.to_dict())
    r.eq("名字能存进存档", back.her_name, "小雨")

    # 旧存档没有这个字段 → 应回落到默认值，而不是崩
    legacy = GameState().to_dict()
    legacy.pop("her_name")
    r.eq("旧存档缺字段时回落默认", GameState.from_dict(legacy).her_name, "猫娘")

    # 提示词注入
    st = make_state(her_name="小雨")
    eng = make_engine(st)
    text = PromptBuilder(ROOT / "prompts").system_prompt(st, eng, inject_style=False)
    r.check("{name} 已被替换", "{name}" not in text)
    r.check("名字进了提示词", "小雨" in text)

    # 空名字不能把提示词打出个空洞
    st = make_state(her_name="")
    eng = make_engine(st)
    text = PromptBuilder(ROOT / "prompts").system_prompt(st, eng, inject_style=False)
    r.check("空名字回落到「猫娘」", "猫娘" in text)


def _test_new_game_name(r: Runner) -> None:
    r.section("回归 · 开新周目能带上名字")

    from src import lockout
    from src.state import SaveStore

    with tempfile.TemporaryDirectory() as td:
        store = SaveStore(Path(td))
        st = lockout.new_game(store, save_id="n1", her_name="小雨")
        r.eq("新周目记得名字", st.her_name, "小雨")

        again = store.load("n1")
        r.eq("名字已落盘", again.her_name, "小雨")

        blank = lockout.new_game(store, save_id="n2", her_name="   ")
        r.eq("空白名字回落默认", blank.her_name, "猫娘")


# ---------------------------------------------------------------------------
# bug 3：玩家替猫娘写动作 / 心情 / 感受
# ---------------------------------------------------------------------------
# 这是提示词层的约束，程序侧只能验证「规则确实注入了」和
# 「她仍然会拒绝、会扣好感」。真正的行为验证靠在线实测。


def _test_player_boundary_prompt(r: Runner) -> None:
    r.section("回归 · 提示词里有「主人只能写他自己」这条规则")

    persona = (ROOT / "prompts" / "persona.md").read_text(encoding="utf-8")
    for kw in ("主人只能写他自己", "那些没有发生过", "你有权拒绝"):
        r.check(f"persona.md 含「{kw}」", kw in persona)

    # 这条规则必须进系统提示词（不能只是躺在文件里）
    st = make_state()
    eng = make_engine(st)
    text = PromptBuilder(ROOT / "prompts").system_prompt(st, eng, inject_style=False)
    r.check("规则确实注入到系统提示词", "主人只能写他自己" in text)


def _test_refusal_still_scores(r: Runner) -> None:
    """她拒绝之后，玩家继续施压必须真的扣分、真的掉档情绪。"""
    r.section("回归 · 越界之后她的状态会变")

    st = make_state()
    eng = make_engine(st)
    # stop_on_request 是她划边界；leverage_dependency 是拿依赖压她（-25，破防级）
    eng.process_turn(Proposal(action_id="stop_on_request", direction="positive"))
    r.check("她划边界会被正面对待", st.affection > 50.0, f"affection={st.affection}")

    rep = eng.process_turn(
        Proposal(action_id="leverage_dependency", direction="negative"))
    r.check("拿依赖压她是破防级扣分", rep.result.final_value <= -12,
            f"final={rep.result.final_value}")
    r.check("好感大幅下降", st.affection < 50.0, f"affection={st.affection}")
    r.check("触发抵触（她记住了这件事）", bool(st.aversions),
            f"aversions={[a.family for a in st.aversions]}")
    r.check("进入信任冻结", st.trust_freeze_until > st.turn)

    # 「打掉一档」只在**高档位**成立 —— 最低档（50）下面没有档位可掉，
    # 这不是 bug 是设计：新存档本来就在地板上。真正的代价是好感本身。
    st_high = make_state(affection=85.0, tier_floor=80)
    eng_high = make_engine(st_high)
    eng_high.process_turn(Proposal(action_id="leverage_dependency", direction="negative"))
    r.eq("高档位时破防级扣分打掉一档", st_high.tier_floor, 70)
    r.check("打掉档位后好感也确实掉了", st_high.affection < 85.0,
            f"affection={st_high.affection}")

    st_low = make_state(affection=52.0, tier_floor=50)
    eng_low = make_engine(st_low)
    eng_low.process_turn(Proposal(action_id="leverage_dependency", direction="negative"))
    r.eq("最低档位时无处可掉（设计如此）", st_low.tier_floor, 50)
    r.check("但没有档位可掉不代表没代价 —— 好感照样崩",
            st_low.affection < 40.0, f"affection={st_low.affection}")


def run() -> int:
    r = Runner()
    _test_negative_lands(r)
    _test_passive_still_guarded(r)
    _test_her_name(r)
    _test_new_game_name(r)
    _test_player_boundary_prompt(r)
    _test_refusal_still_scores(r)
    return r.summary()


if __name__ == "__main__":
    raise SystemExit(run())
