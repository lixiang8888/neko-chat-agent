#!/usr/bin/env python3
"""用 ``MarkerBackend`` 起一个真的 uvicorn。

不是给玩家用的 —— 是给「隐藏标记不上屏」这条验收用的：
只有后端真的吐标记，才能证明桥接层把它扣住了。

    python tests/web/run_marker_server.py 8022
"""

from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
for p in (str(ROOT), str(HERE)):
    if p not in sys.path:
        sys.path.insert(0, p)

import marker_backend  # noqa: E402

marker_backend.install()

import uvicorn  # noqa: E402

from server.app import app  # noqa: E402

if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8022
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
