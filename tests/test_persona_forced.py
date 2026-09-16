
"""人格设定 · 强制性行为场景约束测试。

锁住本次新增的设定条款：
「猫娘是一种比较容易在性行为中感到快感的类人生物」

这条设定要求同时满足三层约束，缺一不可：
  1. 主观意识保持正确 —— 反对不因身体反应而改变；
  2. 减少主动互动 —— 台词少、动作被动；
  3. 身体快感不受主观意志控制 —— 必须有失控描写。

同时锁住「失控 ≠ 同意」这条边界，以及 `forced_pain` 状态下的协调规则。

测试分两块：
  A. 静态条款存在性 —— persona.md 里必须逐条写清楚；
  B. 运行时注入 —— 警惕状态下 `{body_reaction}` 槽位必须被替换成
     包含「主观抗拒 + 身体失控 + 互动最小化」的强化提示。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from _fixtures import Runner, make_engine, make_state  # noqa: E402
from src.context import PromptBuilder  # noqa: E402
from src.scoring import ScoreResult  # noqa: E402


# 条款里必须出现的核心措辞（逐条对应三层要求 + 边界）
CLAUSE_MARKERS = {
    "设定标题": "猫娘是一种比较容易在性行为中感到快感的类人生物",
    "主观意识保持正确": "主观意识保持正确",
    "减少主动互动": "减少主动互动",
    "身体快感不受主观意志控制": "身体快感不受主观意志控制",
    "必须有失控描写": "必须有失控描写",
    "失控不等于同意": "失控 ≠ 同意",
    "forced_pain 协调": "forced_pain",
}

# 失控描写的示例必须至少覆盖若干生理信号
BODY_SIGNALS = ["呼吸", "尾巴", "耳朵", "手指", "发软", "气音", "呻吟"]


def run() -> int:
    r = Runner()
    pb = PromptBuilder(ROOT / "prompts")
    persona = pb.load("persona")

    # ---------------------------------------------------------- A. 静态条款
    r.section("persona.md 条款存在性")

    for name, marker in CLAUSE_MARKERS.items():
        r.check(f"条款「{name}」存在", marker in persona,
                f"未找到 {marker!r}")

    # 三层要求必须同时出现，且顺序上「主观」在「身体」之前 —— 主观优先
    i_subj = persona.find("主观意识保持正确")
    i_body = persona.find("身体快感不受主观意志控制")
    r.check("主观意识条款在身体失控条款之前（主观优先）",
            0 <= i_subj < i_body,
            f"subj={i_subj} body={i_body}")

    # 失控描写示例必须覆盖足够多的生理信号
    hit = [s for s in BODY_SIGNALS if s in persona]
    r.check("失控描写示例覆盖 ≥4 种生理信号",
            len(hit) >= 4, f"命中={hit}")

    # 明确禁止「身体诚实 = 内心同意」的写法
    r.check("明确禁止把身体反应等同于内心同意",
            "身体反应等同于内心同意" in persona or "嘴上说不要身体却很诚实" in persona,
            "缺少对「身体诚实」误写的显式禁令")

    # ---------------------------------------------------------- B. 运行时注入
    r.section("警惕状态下 {body_reaction} 注入")

    # 警惕状态：affection 落在 [0, 50) 区间
    st = make_state(affection=20.0)
    eng = make_engine(st)
    text = pb.system_prompt(st, eng, inject_style=False)

    r.check("{body_reaction} 占位符已被替换", "{body_reaction}" not in text)

    # 注入文本必须同时包含三层要求的关键词
    r.check("注入含「主观上抗拒」", "主观上抗拒" in text or "主观意识描写优先" in text,
            f"片段={text[-400:]}")
    r.check("注入含「身体不受主观意志控制」",
            "不受主观意志控制" in text, f"片段={text[-400:]}")
    r.check("注入含「互动量最小化」",
            "互动量最小化" in text or "不主动" in text, f"片段={text[-400:]}")
    r.check("注入含失控生理信号示例",
            any(s in text for s in ("发烫", "发软", "呼吸变乱", "轻颤")),
            f"片段={text[-400:]}")

    # 主观优先的措辞必须在注入里显式出现
    r.check("注入显式声明「主观意识描写优先」",
            "主观意识描写优先" in text, f"片段={text[-400:]}")

    # ---------------------------------------------------------- 非警惕状态
    r.section("非警惕状态下不注入（不干扰正常演出）")

    st = make_state(affection=70.0)  # 依赖档
    eng = make_engine(st)
    text = pb.system_prompt(st, eng, inject_style=False)

    r.check("{body_reaction} 占位符仍被替换（空串）", "{body_reaction}" not in text)
    r.check("非警惕状态不出现「互动量最小化」强化提示",
            "互动量最小化" not in text,
            "正常演出不该被强制最小化互动")

    # ---------------------------------------------------------- 与 forced_pain 协调
    r.section("与 forced_pain 状态协调")

    # persona 里必须写明 forced_pain 下失控描写要更克制
    r.check("persona 声明 forced_pain 下失控描写更克制",
            "更克制" in persona and "forced_pain" in persona,
            "缺少 forced_pain 与失控描写的协调规则")

    # ---------------------------------------------------------- 全槽位完整性
    r.section("槽位完整性（含 body_reaction）")

    st = make_state(affection=20.0)
    eng = make_engine(st)
    text = pb.system_prompt(st, eng, inject_style=False)
    leftovers = [s for s in
                 ("{name}", "{tone}", "{mood}", "{desire}", "{aversions}",
                  "{bible}", "{intimacy}", "{body_reaction}")
                 if s in text]
    r.check("全部占位符都被替换（含 body_reaction）", not leftovers,
            f"残留={leftovers}")

    # ---------------------------------------------------------- 主观反对 + 身体失控 并存
    r.section("主观反对与身体失控并存（核心张力）")

    # 注入文本必须同时出现「主观抗拒」与「身体失控」两侧的措辞，
    # 缺任何一侧都会退化成「她其实也想要」或「她毫无反应」。
    st = make_state(affection=20.0)
    eng = make_engine(st)
    text = pb.system_prompt(st, eng, inject_style=False)

    r.check("注入同时含主观侧（抗拒/警惕）与身体侧（生理反应）",
            ("抗拒" in text or "警惕" in text)
            and ("生理反应" in text or "身体是诚实的" in text),
            f"片段={text[-400:]}")

    # 主观优先：注入里必须显式声明身体反应不能削弱抗拒
    r.check("注入声明身体反应不得削弱抗拒",
            "绝不能反过来削弱你的抗拒" in text,
            f"片段={text[-400:]}")

    # 身体反应必须被描述为「不受主观意志控制」，而不是「她想要」
    r.check("注入把身体反应归因于不受控而非意愿",
            "不受主观意志控制" in text and "想要" not in text.split("身体是诚实的")[-1][:80],
            f"片段={text[-400:]}")

    # persona 静态条款：失控描写必须服务于「控制不了身体」而非「享受」
    r.check("persona 声明失控描写服务于「控制不了身体」",
            "她控制不了自己的身体" in persona,
            "缺少对失控描写意图的显式约束")

    # persona 静态条款：主观反对与身体失控必须同时存在
    r.check("persona 声明主观反对与身体失控同时存在",
            "主观上的反对与身体上的失控同时存在" in persona,
            "缺少「并存」这一核心张力的显式表述")

    # persona 静态条款：失控描写不得写成享受
    r.check("persona 禁止把失控写成享受",
            "而不是服务于「她其实很享受」" in persona,
            "缺少对「失控=享受」误写的显式禁令")

    # ---------------------------------------------------------- 减少主动互动
    r.section("减少主动互动（台词少、动作被动）")

    r.check("persona 要求台词少", "她的台词要少" in persona,
            "缺少「台词要少」的约束")
    r.check("persona 要求动作被动", "动作要被动" in persona,
            "缺少「动作要被动」的约束")
    r.check("persona 禁止主动配合/推进",
            "不要写她主动配合或主动推进" in persona,
            "缺少对主动配合的显式禁令")

    # 注入侧同样要压住互动量
    st = make_state(affection=20.0)
    eng = make_engine(st)
    text = pb.system_prompt(st, eng, inject_style=False)
    r.check("注入要求不主动/不提问/不调情",
            "不主动" in text and "不提问" in text and "不调情" in text,
            f"片段={text[-400:]}")

    # ---------------------------------------------------------- forced_pain 槽位注入
    r.section("forced_pain 槽位注入（L1–L5）")

    expr_by_level = make_engine(make_state()).cfg["forced_pain"]["expression_by_level"]

    for lvl in range(1, 6):
        expr = expr_by_level[f"L{lvl}"]
        st = make_state(affection=20.0, forced_pain_active=True,
                        forced_pain_level=lvl, forced_pain_expression=expr)
        eng = make_engine(st)
        text = pb.system_prompt(st, eng, inject_style=False)
        r.check(f"L{lvl} 注入含等级标记 L{lvl}", f"L{lvl}" in text,
                f"片段={text[-300:]}")
        r.check(f"L{lvl} 注入含表情「{expr}」", expr in text,
                f"片段={text[-300:]}")
        r.check(f"L{lvl} 无占位符残留", "{forced_pain}" not in text)

    # 非激活态：槽位为空串，无残留、无等级行
    st = make_state(affection=20.0, forced_pain_active=False)
    eng = make_engine(st)
    text = pb.system_prompt(st, eng, inject_style=False)
    r.check("非激活时无占位符残留", "{forced_pain}" not in text)
    r.check("非激活时不注入等级行", "当前等级：L" not in text)

    # ---------------------------------------------------------- 按回合推进与封顶
    r.section("按回合推进与封顶")

    st = make_state(affection=20.0)
    eng = make_engine(st)
    ev: list[str] = []
    trigger = ScoreResult(action_id="force_closeness", family="violation", final_value=-10.0)
    eng._advance_forced_pain(trigger, "", ev)
    r.check("低好感 + 强制类动作触发激活", st.forced_pain_active,
            f"active={st.forced_pain_active} aff={st.affection}")
    r.eq("触发即为 L1", st.forced_pain_level, 1)
    r.eq("触发回合数为 1", st.forced_pain_turns, 1)
    r.eq("触发时表情为 L1 默认值", st.forced_pain_expression, expr_by_level["L1"])

    neutral = ScoreResult(action_id=None, family="unknown", final_value=0.0)
    for _ in range(11):
        eng._advance_forced_pain(neutral, "", ev)
    r.eq("每满 3 回合加深一档 → 12 回合达 L5", st.forced_pain_level, 5)
    r.eq("L5 表情为默认值", st.forced_pain_expression, expr_by_level["L5"])

    for _ in range(6):
        eng._advance_forced_pain(neutral, "", ev)
    r.eq("超过上限仍封顶 L5", st.forced_pain_level, 5)

    # ---------------------------------------------------------- 表情优先：玩家点名
    r.section("表情按档位默认值填充，玩家点名优先")

    st = make_state(affection=20.0, forced_pain_active=True, forced_pain_level=1,
                    forced_pain_turns=1, forced_pain_expression="害怕")
    eng = make_engine(st)
    ev = []
    eng._advance_forced_pain(ScoreResult(action_id=None, family="unknown", final_value=0.0),
                             "她此刻沉默着", ev)
    r.eq("玩家点名白名单表情「沉默」优先", st.forced_pain_expression, "沉默")

    # ---------------------------------------------------------- 连续 5 个正向回合退出
    r.section("连续 5 个正向回合退出并归零")

    st = make_state(affection=20.0, forced_pain_active=True, forced_pain_level=3,
                    forced_pain_turns=5, forced_pain_expression="闪躲")
    eng = make_engine(st)
    ev = []
    positive = ScoreResult(action_id="give_face", family="dignity", final_value=2.0)
    for _ in range(4):
        eng._advance_forced_pain(positive, "", ev)
    r.check("不足 5 个正向回合仍激活", st.forced_pain_active,
            f"streak={st.forced_pain_recovery_streak}")
    eng._advance_forced_pain(positive, "", ev)
    r.check("满 5 个正向回合后解除激活", not st.forced_pain_active)
    r.eq("解除后等级归零", st.forced_pain_level, 0)
    r.eq("解除后回合数归零", st.forced_pain_turns, 0)
    r.eq("解除后恢复连击归零", st.forced_pain_recovery_streak, 0)

    # ---------------------------------------------------------- 两个开关字段语义
    r.section("resistance_blocked / forced_input_still_advances 开关语义")

    st = make_state(affection=20.0, forced_pain_active=True, forced_pain_level=2,
                    forced_pain_turns=4, forced_pain_expression="害怕")
    eng = make_engine(st)
    r.check("配置 resistance_blocked 开关存在且为真",
            eng.cfg["forced_pain"].get("resistance_blocked") is True)
    ev = []
    beg = ScoreResult(action_id="stop_on_request", family="respect_boundary", final_value=3.0)
    eng._advance_forced_pain(beg, "", ev)
    r.eq("resistance_blocked=True 时求饶类不计恢复连击",
         st.forced_pain_recovery_streak, 0)

    eng.cfg["forced_pain"]["resistance_blocked"] = False
    eng._advance_forced_pain(beg, "", ev)
    r.check("resistance_blocked=False 时求饶类计入恢复连击",
            st.forced_pain_recovery_streak >= 1,
            f"streak={st.forced_pain_recovery_streak}")

    st = make_state(affection=20.0, forced_pain_active=True, forced_pain_level=2,
                    forced_pain_turns=4, forced_pain_expression="害怕")
    eng = make_engine(st)
    r.check("配置 forced_input_still_advances 开关存在且为真",
            eng.cfg["forced_pain"].get("forced_input_still_advances") is True)
    ev = []
    passive = ScoreResult(action_id=None, family="unknown", final_value=0.0)
    eng.cfg["forced_pain"]["forced_input_still_advances"] = False
    t0 = st.forced_pain_turns
    eng._advance_forced_pain(passive, "", ev)
    r.eq("forced_input_still_advances=False 时被动回合不推进",
         st.forced_pain_turns, t0)
    eng.cfg["forced_pain"]["forced_input_still_advances"] = True
    eng._advance_forced_pain(passive, "", ev)
    r.eq("forced_input_still_advances=True 时被动回合推进",
         st.forced_pain_turns, t0 + 1)

    # ---------------------------------------------------------- 内射结算
    r.section("内射命中：加深一档 + 计数 + 扣分 + 冻结")

    st = make_state(affection=20.0, forced_pain_active=True, forced_pain_level=2,
                    forced_pain_turns=4, forced_pain_expression="害怕")
    eng = make_engine(st)
    cfg_fp = eng.cfg["forced_pain"]
    ev = []
    before_aff = st.affection
    eng._handle_creampie("我要中出你", ev)
    r.eq("内射后等级加深一档", st.forced_pain_level, 3)
    r.eq("内射计数 +1", st.forced_pain_creampie_count, 1)
    r.eq("按 creampie_penalty 扣好感", st.affection,
         round(before_aff + cfg_fp["creampie_penalty"], 3))
    r.eq("按 creampie_freeze_turns 进入冻结", st.trust_freeze_until,
         st.turn + cfg_fp["creampie_freeze_turns"])

    st = make_state(affection=20.0, forced_pain_active=True, forced_pain_level=5,
                    forced_pain_turns=10, forced_pain_expression="沉默")
    eng = make_engine(st)
    ev = []
    eng._handle_creampie("中出", ev)
    r.eq("L5 时内射不再加深（封顶）", st.forced_pain_level, 5)

    st = make_state(affection=20.0, forced_pain_active=True, forced_pain_level=2,
                    forced_pain_turns=4, forced_pain_expression="害怕")
    eng = make_engine(st)
    st.turn = 5
    st.trust_freeze_until = 999
    ev = []
    eng._handle_creampie("内射", ev)
    r.eq("与更长既有冻结取长（不覆盖更长的）", st.trust_freeze_until, 999)

    st = make_state(affection=20.0, forced_pain_active=True, forced_pain_level=2,
                    forced_pain_turns=4, forced_pain_expression="害怕")
    eng = make_engine(st)
    ev = []
    eng._handle_creampie("今天天气不错", ev)
    r.eq("非内射输入不计入计数", st.forced_pain_creampie_count, 0)
    r.eq("非内射输入不改变等级", st.forced_pain_level, 2)

    # 静态：正则表放在 config/，代码里不硬编码
    r.section("内射正则表落在 config/")
    pat_path = ROOT / "config" / "creampie_patterns.json"
    r.check("config/creampie_patterns.json 存在", pat_path.exists())

    return r.summary()


if __name__ == "__main__":
    raise SystemExit(run())
