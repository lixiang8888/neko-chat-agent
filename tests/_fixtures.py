"""离线测试台的公共夹具。

所有测试都不联网 —— 数值逻辑必须能在没有模型的情况下完整验证。
"""

from __future__ import annotations

import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.engine import Engine  # noqa: E402
from src.scoring import Proposal, Rules, load_rules, resolve  # noqa: E402
from src.state import GameState  # noqa: E402


def make_rules() -> Rules:
    return load_rules(ROOT / "config")


def make_state(**kw) -> GameState:
    st = GameState(save_id="test")
    for k, v in kw.items():
        setattr(st, k, v)
    return st


def make_engine(state: GameState, seed: int = 42) -> Engine:
    return Engine(state, make_rules(), rng=random.Random(seed))


def score(action_id: str, state: GameState, **kw) -> float:
    rules = make_rules()
    direction = kw.pop("direction", None)
    if direction is None:
        spec = rules.by_id(action_id)
        direction = "positive" if (spec and spec.value > 0) else "negative"
    proposal = Proposal(action_id=action_id, direction=direction, **kw)
    return resolve(proposal, state, rules).final_value


# --------------------------------------------------------------------------
# 迷你测试框架（避免引入 pytest 依赖）
# --------------------------------------------------------------------------


class Runner:
    def __init__(self) -> None:
        self.passed = 0
        self.failed: list[tuple[str, str]] = []

    def check(self, name: str, cond: bool, detail: str = "") -> None:
        if cond:
            self.passed += 1
            print(f"  [PASS] {name}")
        else:
            self.failed.append((name, detail))
            print(f"  [FAIL] {name}  {detail}")

    def eq(self, name: str, actual, expected, tol: float = 1e-6) -> None:
        if isinstance(expected, float):
            ok = abs(float(actual) - expected) <= tol
        else:
            ok = actual == expected
        self.check(name, ok, f"实际={actual!r} 期望={expected!r}")

    def section(self, title: str) -> None:
        print(f"\n── {title} " + "─" * max(0, 40 - len(title)))

    def summary(self) -> int:
        print("\n" + "=" * 50)
        total = self.passed + len(self.failed)
        print(f"  通过 {self.passed}/{total}")
        if self.failed:
            print("  失败项：")
            for name, detail in self.failed:
                print(f"    - {name}  {detail}")
        print("=" * 50)
        return 1 if self.failed else 0
