#!/usr/bin/env python3
"""统一测试入口：跑完所有离线测试并汇总。

**全部离线** —— 不需要 API Key，不需要装 requests，不联网。
这一点是刻意的：数值逻辑必须能在没有模型的情况下完整验证。
如果哪天这里开始需要联网，那是架构出了问题，不是测试的问题。
"""

from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import test_balance  # noqa: E402
import test_crisis  # noqa: E402
import test_engine  # noqa: E402
import test_intimacy  # noqa: E402
import test_memory  # noqa: E402
import test_regressions  # noqa: E402
import test_scoring  # noqa: E402
import test_secrets  # noqa: E402
import test_selfreport  # noqa: E402
import test_transport  # noqa: E402

SUITES = [
    ("评分引擎与护栏", test_scoring.run),
    ("状态机与分支", test_engine.run),
    ("记忆层与会话", test_memory.run),
    ("平衡性对抗", test_balance.run),
    ("传输层与显示", test_transport.run),
    ("模型自评通道", test_selfreport.run),
    ("露骨档位门", test_intimacy.run),
    ("危机分支", test_crisis.run),
    ("密钥防线", test_secrets.run),
    ("回归防线", test_regressions.run),
]


def main() -> int:
    print("=" * 52)
    print("  猫娘 Agent · 离线测试全集")
    print("=" * 52)

    codes = []
    for name, fn in SUITES:
        print(f"\n\n████ {name} " + "█" * max(0, 44 - len(name)))
        codes.append(fn())

    bad = sum(1 for c in codes if c != 0)
    print(f"\n\n{'=' * 52}")
    print(f"  套件：{len(SUITES) - bad}/{len(SUITES)} 通过")
    print("=" * 52)
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
