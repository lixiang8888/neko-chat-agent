#!/usr/bin/env python3
"""出截图：四个 tone 档位、扉、危机回合、终幕。

对着**普通服务器**跑（``NEKO_OFFLINE=1``，StubBackend）而不是 marker 服务器 ——
离线后端有正经的 ``_crisis_line()``，危机那张截图才是真的。

    python ~/.claude/skills/webapp-testing/scripts/with_server.py \\
      --server "uv run python tests/web/fixtures.py && uv run python -m uvicorn server.app:app --port 8000" \\
      --port 8000 -- python tests/web/screenshots.py

⚠️ 造存档**必须在起服务之前**。桥接层按 save_id 缓存 ``Session``
（见 ``server/app.py`` 的 ``Sessions``），存档文件被外部改写时它不会重读 ——
所以「先起服务再改存档」看到的还是老状态。这不是 bug，是内存状态的代价；
它换来的是每个回合不用重新读盘。

截图落在 ``docs/shots/``。
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import fixtures  # noqa: E402

BASE = os.environ.get("NEKO_BASE", "http://127.0.0.1:8000")
OUT = ROOT / "docs" / "shots"

# 存档 id → 文件名。顺序就是感情的走向。
TONES = [
    ("save2", "01-警惕"),
    ("save3", "02-依赖"),
    ("save4", "03-独属"),
    ("save5", "04-认定"),
]


def shoot(page, name: str) -> None:
    page.screenshot(path=str(OUT / f"{name}.png"))
    print(f"  → {name}.png")


def open_save(page, save_id: str) -> None:
    page.wait_for_selector(f'.save[data-save-id="{save_id}"]')
    page.click(f'.save[data-save-id="{save_id}"]')
    page.wait_for_selector("#screen-stage:not([hidden])")
    page.wait_for_function("() => document.querySelectorAll('.line').length > 0")
    page.wait_for_function("() => !document.querySelector('#say-input').disabled")
    time.sleep(0.9)          # 让光的过渡走完再截


def main() -> int:
    from playwright.sync_api import sync_playwright

    OUT.mkdir(parents=True, exist_ok=True)
    fixtures.build(clean=True)

    with sync_playwright() as p:
        browser = p.chromium.launch()
        # 1× 就够看清了。2× 每张 5MB，十几张下来是几十 MB 的无谓体积 ——
        # 想要更细的图就 NEKO_SHOT_SCALE=2。
        scale = float(os.environ.get("NEKO_SHOT_SCALE", "1"))
        page = browser.new_page(viewport={"width": 1280, "height": 860},
                                device_scale_factor=scale)
        page.set_default_timeout(60000)
        page.goto(BASE, wait_until="load")

        print("扉")
        page.wait_for_selector(".save")
        time.sleep(0.6)
        shoot(page, "00-扉")

        for save_id, name in TONES:
            print(f"舞台 · {name}")
            open_save(page, save_id)
            shoot(page, f"tone-{name}")
            page.click("#btn-door")
            page.wait_for_selector("#screen-title:not([hidden])")

        # 危机回合：冷下来，但她还在
        print("危机回合")
        open_save(page, "save3")
        page.fill("#say-input", "我今天真的不想活了。")
        page.press("#say-input", "Enter")
        page.wait_for_function(
            "() => !document.querySelector('#say-input').disabled", timeout=90000
        )
        time.sleep(0.9)
        shoot(page, "05-危机回合")

        # 常态对照：同一个位置上，灯是暖的、她在动
        print("常态对照")
        page.click("#btn-door")
        page.wait_for_selector("#screen-title:not([hidden])")
        open_save(page, "save3")
        time.sleep(0.9)
        shoot(page, "06-常态对照")

        # 终幕：沉沦。灯灭，这一局结束。
        print("终幕")
        page.click("#btn-door")
        page.wait_for_selector("#screen-title:not([hidden])")
        open_save(page, "save8")
        page.fill("#say-input", "别说了。")
        page.press("#say-input", "Enter")
        page.wait_for_function(
            "() => document.querySelector('#screen-stage').classList"
            ".contains('stage--ended')", timeout=90000
        )
        time.sleep(1.2)
        shoot(page, "09-终幕-沉沦")

        # 窄屏
        print("窄屏")
        page.set_viewport_size({"width": 390, "height": 844})
        page.click("#btn-door")
        page.wait_for_selector("#screen-title:not([hidden])")
        time.sleep(0.4)
        shoot(page, "07-窄屏-扉")
        open_save(page, "save5")
        shoot(page, "08-窄屏-舞台")

        browser.close()

    print(f"\n截图在 {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
