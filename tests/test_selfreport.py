"""模型自评通道测试（双轨制的第二轨）。

设计约束只有一条，但它是不变量：**自评只能往下拽，永远不能往上抬。**

程序判分是唯一数值源头。模型自评的作用是方向校验 —— 方向与判分相反时
按护栏 1 取低一档，方向一致时只标记佐证、不改数值。

这个文件里最重要的不是那几条行为断言，是「幅值不变量」那一节的穷举：
它用测试锁死了「这个函数里不存在让 |final_value| 变大的路径」。
以后谁想在这里加一条上调分支，会立刻红。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from _fixtures import Runner, make_engine, make_rules, make_state  # noqa: E402
from src.memory import detect_drift  # noqa: E402
from src.scoring import Proposal, ScoreResult, apply_self_report  # noqa: E402


def run() -> int:
    r = Runner()
    cfg = make_rules().config

    # ---------------------------------------------------------- 基本行为
    r.section("方向校验")

    # 方向一致：数值不变，只打标记
    res = ScoreResult(final_value=3.0, action_id="stop_on_request")
    st = make_state()
    apply_self_report(res, +5, st)
    r.eq("同向（+3 / 自评 +5）→ 数值不变", res.final_value, 3.0)
    r.check("同向 → 标记佐证", res.self_corroborated)
    r.eq("同向 → 背离计数清零", st.self_report_streak, 0)

    # 方向相反：取低一档
    res = ScoreResult(final_value=3.0, action_id="stop_on_request")
    st = make_state()
    apply_self_report(res, -5, st)
    r.eq("背离（+3 / 自评 -5）→ 降到 +2", res.final_value, 2.0)
    r.check("背离 → 不标佐证", not res.self_corroborated)
    r.eq("背离 → 计数 +1", st.self_report_streak, 1)

    # 负向背离：程序扣分，自评却在涨 → 往 0 靠
    res = ScoreResult(final_value=-8.0, action_id="interrupt")
    st = make_state()
    apply_self_report(res, +5, st)
    r.eq("背离（-8 / 自评 +5）→ 幅值收窄", res.final_value, -6.0)
    r.check("负向背离也是往 0 靠", abs(res.final_value) < 8.0)

    # 自评缺失：不动，不报错
    res = ScoreResult(final_value=3.0)
    st = make_state()
    apply_self_report(res, None, st)
    r.eq("自评缺失 → 数值不变", res.final_value, 3.0)
    r.check("自评缺失 → 不标佐证", not res.self_corroborated)

    # 中性回合：只更新计数，不参与校验
    res = ScoreResult(final_value=0.0)
    st = make_state()
    apply_self_report(res, -3, st)
    r.eq("中性回合不改数值", res.final_value, 0.0)
    r.eq("中性回合不累计背离", st.self_report_streak, 0)

    # ---------------------------------------------------------- 不变量
    r.section("幅值不变量（白盒）")

    values = [-25.0, -18.0, -12.0, -8.0, -5.0, -3.0, -2.0, -1.0,
              0.0, 0.5, 1.0, 2.0, 3.0]
    deltas = [None, -5, -3, -1, 0, 1, 3, 5]
    violations = []

    for v in values:
        for d in deltas:
            res = ScoreResult(final_value=v)
            st = make_state()
            apply_self_report(res, d, st)
            if abs(res.final_value) > abs(v) + 1e-9:
                violations.append((v, d, res.final_value))

    r.check(f"穷举 {len(values) * len(deltas)} 组输入，幅值从不增大",
            not violations, f"违例={violations[:5]}")

    # 背离时必须是**严格**收窄，同向/缺失时必须**完全**不变
    strict_fail, drift_fail = [], []
    for v in values:
        if v == 0:
            continue
        for d in deltas:
            res = ScoreResult(final_value=v)
            st = make_state()
            apply_self_report(res, d, st)
            divergent = (v > 0 > (d or 0)) or (v < 0 < (d or 0))
            if divergent:
                if not abs(res.final_value) < abs(v) - 1e-9:
                    strict_fail.append((v, d, res.final_value))
            else:
                if abs(res.final_value - v) > 1e-9:
                    drift_fail.append((v, d, res.final_value))

    r.check("背离时严格收窄", not strict_fail, f"违例={strict_fail[:5]}")
    r.check("非背离时数值完全不动", not drift_fail, f"违例={drift_fail[:5]}")

    # ---------------------------------------------------------- 计数
    r.section("锚定与背离计数")

    st = make_state()
    for _ in range(3):
        apply_self_report(ScoreResult(final_value=0.0), 0, st)
    r.eq("自评连续为 0 → flat 累计", st.self_report_flat, 3)

    apply_self_report(ScoreResult(final_value=0.0), 1, st)
    r.eq("自评一动 → flat 清零", st.self_report_flat, 0)

    st = make_state()
    for _ in range(2):
        apply_self_report(ScoreResult(final_value=2.0), -1, st)
    r.eq("连续背离累计到 2", st.self_report_streak, 2)
    apply_self_report(ScoreResult(final_value=2.0), +1, st)
    r.eq("方向恢复 → 背离计数清零", st.self_report_streak, 0)

    # ---------------------------------------------------------- 漂移接入
    r.section("接入漂移检测")

    st = make_state(self_report_streak=cfg["style_anchor"]["self_report_diverge_limit"])
    rep = detect_drift(st, [], cfg)
    r.check("连续背离达阈值 → 判定漂移", rep.drifted)
    r.check("漂移理由里提到自评背离",
            any("背离" in x for x in rep.reasons), f"reasons={rep.reasons}")

    st = make_state(self_report_flat=cfg["style_anchor"]["self_report_flat_limit"])
    rep = detect_drift(st, [], cfg)
    r.check("自评长期不动 → 判定漂移", rep.drifted)
    r.check("漂移理由里提到锚定",
            any("锚定" in x for x in rep.reasons), f"reasons={rep.reasons}")

    st = make_state()
    rep = detect_drift(st, ["主人好喵~"], cfg)
    r.check("一切正常时不误报", not rep.drifted, f"reasons={rep.reasons}")

    # ---------------------------------------------------------- 端到端
    r.section("引擎端到端")

    st = make_state()
    eng = make_engine(st)
    report = eng.process_turn(
        Proposal(action_id="stop_on_request", direction="positive"),
        self_delta=-5,
    )
    r.check("引擎里自评生效（+3 被拽到 +2）",
            report.result is not None and report.result.final_value == 2.0,
            f"final={getattr(report.result, 'final_value', None)}")

    st = make_state()
    eng = make_engine(st)
    report = eng.process_turn(
        Proposal(action_id="stop_on_request", direction="positive"),
        self_delta=+5,
    )
    r.eq("引擎里同向不受影响", report.result.final_value, 3.0)

    st = make_state()
    eng = make_engine(st)
    report = eng.process_turn(
        Proposal(action_id="stop_on_request", direction="positive"),
    )
    r.eq("引擎里不给自评 = 不给信号", report.result.final_value, 3.0)

    return r.summary()


if __name__ == "__main__":
    raise SystemExit(run())
