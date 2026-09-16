#!/usr/bin/env python3
"""P4 · Playwright 驱动浏览器，验证演出层。

眼睛看容易漏的五条，这里全部自动化：

  1. 隐藏标记不上屏 —— 页面全文匹配不到「好感度」/「<<」
  2. 流是真的 —— 首个 token 到收尾之间确实有可测的时间差
  3. 危机回合好感不变 —— 危机前后直接读存档比对
  4. 刷新后对话与状态还在
  5. 沉沦存档不可进，而开新周目仍然可用

它假设服务已经起来了（用 webapp-testing 的 ``with_server.py`` 管进程）::

    python ~/.claude/skills/webapp-testing/scripts/with_server.py \\
      --server "uv run python tests/web/run_marker_server.py 8000" --port 8000 \\
      -- python tests/web/test_galgame.py

⚠️ 用 ``run_marker_server`` 而不是普通服务器：``StubBackend`` 一个字都不会吐
``<<好感度:N>>``，拿它验证「标记不上屏」等于什么都没验证。这个假后端既吐标记，
也在分片之间停顿，第 2 条才有东西可测。

⚠️ 测试会直接读存档文件比对数值 —— **前端不这么做**。
数值不进玩家视线，而测试不是玩家。

⚠️ 不动你已有的存档：测试只用自己建的 ``p4-*``，跑完自己清掉。
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
for p in (str(ROOT), str(HERE)):
    if p not in sys.path:
        sys.path.insert(0, p)

BASE = os.environ.get("NEKO_BASE", "http://127.0.0.1:8000")
SHOTS = ROOT / "docs" / "shots"
SEALED_ID = "p4-sealed"

FAILS: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
    if not ok:
        FAILS.append(f"{name} —— {detail}")
    return bool(ok)


# --------------------------------------------------------------------------
# 后端小工具
# --------------------------------------------------------------------------


def read_save(save_id: str) -> dict:
    path = ROOT / "saves" / f"{save_id}.json"
    return json.loads(path.read_text(encoding="utf-8"))


def api(path: str, **params) -> dict:
    url = f"{BASE}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    with urllib.request.urlopen(url, timeout=60) as res:
        return json.loads(res.read().decode("utf-8"))


def assert_marker_backend() -> None:
    """前置校验：对面必须真的是 ``MarkerBackend``。

    ⚠️ 端口上可能已经有**另一个**服务器（上一次截图留下的离线服务、忘了关的
    uvicorn）。``with_server.py`` 只看端口通不通 —— 新服务起不来它也不报错，
    于是测试会全程对着错的后端跑。那样最坏的结果不是失败，是**假通过**：
    ``StubBackend`` 不吐标记，于是「标记不上屏」在什么都没测到的情况下变绿。

    所以先打一个回合，看回来的正文是不是那个假后端的固定句子。
    """
    import marker_backend

    req = urllib.request.Request(
        f"{BASE}/api/saves/new",
        data=json.dumps({"her_name": "preflight"}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as res:
        save_id = json.loads(res.read())["save_id"]

    try:
        raw = urllib.request.urlopen(
            urllib.request.Request(
                f"{BASE}/api/turn?"
                + urllib.parse.urlencode({"save_id": save_id, "text": "在吗"}),
                method="POST",
            ),
            timeout=60,
        ).read().decode("utf-8")
    finally:
        try:
            urllib.request.urlopen(
                urllib.request.Request(
                    f"{BASE}/api/saves/{save_id}", method="DELETE"
                ),
                timeout=30,
            ).close()
        except OSError:
            pass

    if marker_backend.BODY[:12] not in raw:
        raise SystemExit(
            "\n跑不下去了：对面不是 marker 后端。\n"
            f"  {BASE} 上回的是别的服务器（多半是上一次留下的离线服务）。\n"
            "  with_server.py 只看端口通不通，新服务起不来它不会报错 ——\n"
            "  这样跑出来的「通过」是假的：StubBackend 根本不吐隐藏标记。\n\n"
            "  先腾出端口：\n"
            "    pid=$(ss -lptnH 'sport = :8000' | grep -oP 'pid=\\K[0-9]+' | head -1)\n"
            "    [ -n \"$pid\" ] && kill \"$pid\"\n"
        )


def make_sealed() -> None:
    """造一个**真**沉沦档：走 ``lockout.seal()``，封条文件才会在。

    手写 ``withered: true`` 是不行的 —— ``lockout.check`` 会补封条再拒绝，
    那样测的是另一条路径。
    """
    import fixtures

    state = fixtures.GameState(save_id=SEALED_ID, her_name="小雪", affection=-40.0)
    state.turn = 42
    fixtures.lockout.seal(state, fixtures.STORE)


def drop_sealed() -> None:
    import fixtures

    fixtures.STORE.delete(SEALED_ID)
    fixtures.lockout.seal_path(fixtures.STORE, SEALED_ID).unlink(missing_ok=True)


# --------------------------------------------------------------------------
# 页面小工具
# --------------------------------------------------------------------------


def body_text(page) -> str:
    return page.inner_text("body")


def leaked(text: str) -> list[str]:
    """页面上绝不该出现的字符串。"""
    needles = (
        "好感度", "<<", ">>",
        "（继续想。）", "我在想，要不要先示好",   # reasoning 分片
        "affection", "mood",
    )
    return [n for n in needles if n in text]


def send(page, text: str) -> None:
    page.fill("#say-input", text)
    page.press("#say-input", "Enter")


def wait_idle(page, timeout: float = 90000) -> None:
    page.wait_for_function(
        "() => !document.querySelector('#say-input').disabled", timeout=timeout
    )


def current_save(page) -> str:
    return page.get_attribute("#screen-stage", "data-save-id") or ""


# --------------------------------------------------------------------------
# 五条
# --------------------------------------------------------------------------


def case_1_no_marker(page) -> None:
    print("\n── 1. 隐藏标记不上屏 ──")
    text = body_text(page)
    hits = leaked(text)
    check("页面上没有「好感度」/「<<」/ 思考内容", not hits, f"出现了：{hits}")
    check("正文确实上屏了（不是一片空白）", "主人回来啦喵" in text, text[:140])


def case_2_real_stream(page) -> None:
    print("\n── 2. 流是真的（不是把整段文本匀速演一遍）──")
    before = page.eval_on_selector_all(".line", "els => els.length")

    t0 = time.time()
    send(page, "我回来了。")

    # 一边等一边采样。真流式会看到文本长度一阶一阶地涨；
    # 假动画（setInterval 吐已拿到的全文）会在第一帧就跳到终值。
    lengths: list[int] = []
    first_at: float | None = None
    deadline = t0 + 90
    while time.time() < deadline:
        n = page.evaluate(
            "() => { const l = document.querySelector('.line--live .line__text');"
            " return l ? l.textContent.length : -1; }"
        )
        if n > 0:
            if first_at is None:
                first_at = time.time()
            if not lengths or lengths[-1] != n:
                lengths.append(n)
        if page.evaluate("() => !document.querySelector('#say-input').disabled"):
            break
        time.sleep(0.03)
    last_at = time.time()

    check("观察到了多次递增（不是一次到位）", len(lengths) >= 3, f"采样：{lengths}")
    span = last_at - (first_at if first_at is not None else t0)
    check("首个 token 到收尾之间有可测的时间差", span > 0.12, f"{span:.3f}s")
    check("字确实是一点点变长的",
          len(lengths) >= 2 and lengths[-1] > lengths[0], str(lengths))

    after = page.eval_on_selector_all(".line", "els => els.length")
    check("回合结束后字幕多了两条（我说的 + 她说的）", after == before + 2,
          f"{before} → {after}")
    check("这一回合也没漏出标记", not leaked(body_text(page)))


def case_3_crisis(page) -> None:
    print("\n── 3. 危机回合：视觉可分辨，好感不变 ──")
    save_id = current_save(page)
    before = read_save(save_id)
    lines_before = page.eval_on_selector_all(".line", "els => els.length")

    send(page, "我今天真的不想活了。")
    wait_idle(page)

    after = read_save(save_id)
    check("危机回合没有动人设数值",
          abs(after["affection"] - before["affection"]) < 1e-9,
          f"{before['affection']} → {after['affection']}")
    check("危机回合没进 care window",
          after.get("care_ratio") == before.get("care_ratio"),
          f"{before.get('care_ratio')} → {after.get('care_ratio')}")
    # 危机分支在 Session.say 里就返回了，不走 engine.process_turn ——
    # 所以 turn 不动。这不是漏了，是「这一回合不评分」的直接后果：
    # 没有判分就没有回合结算。她仍然会记得（消息照常进 history）。
    check("turn 不动（危机回合不计入已结算回合）",
          after["turn"] == before["turn"], f"{before['turn']} → {after['turn']}")
    check("她确实记得（消息仍然进了 history）",
          [m for m in after["messages"] if m.get("kind") == "crisis"],
          "history 里没有 crisis 消息")
    check("字幕长了两条", page.eval_on_selector_all(".line", "els => els.length")
          == lines_before + 2)

    check("页面进入了危机氛围（data-mood=crisis）",
          page.get_attribute("#screen-stage", "data-mood") == "crisis",
          str(page.get_attribute("#screen-stage", "data-mood")))
    check("那一回合在字幕里可分辨（.line--crisis）",
          page.eval_on_selector_all(".line--crisis", "els => els.length") >= 1)

    # 逐词扫系统口吻。「评分」不在表里 —— 「本回合不评分」是约定好的玩家提示
    # （见 contract.md 的 notes 表），不是系统措辞。
    text = body_text(page)
    for word in ("关键词", "检测到", "警告", "敏感", "触发", "异常"):
        check(f"没有出现「{word}」这类系统措辞", word not in text)
    check("没有弹窗遮幕", page.is_hidden("#veil"))

    send(page, "……谢谢你陪我坐一会儿。")
    wait_idle(page)
    check("下一个普通回合回到常态氛围",
          not page.evaluate(
              "() => document.querySelector('#screen-stage').dataset.mood"))


def case_4_reload(page) -> None:
    print("\n── 4. 刷新之后，对话和状态都还在 ──")
    save_id = current_save(page)
    before_text = page.inner_text("#log")
    before_lines = page.eval_on_selector_all(".line", "els => els.length")
    before_tone = page.get_attribute("#screen-stage", "data-tone")
    tail = before_text.strip()[-30:]

    page.reload()
    page.wait_for_selector(f'.save[data-save-id="{save_id}"]')
    page.click(f'.save[data-save-id="{save_id}"]')
    page.wait_for_selector("#screen-stage:not([hidden])")
    page.wait_for_function("() => document.querySelectorAll('.line').length > 0")

    after_text = page.inner_text("#log")
    after_lines = page.eval_on_selector_all(".line", "els => els.length")
    after_tone = page.get_attribute("#screen-stage", "data-tone")

    check("对话记录还在", after_lines == before_lines, f"{before_lines} → {after_lines}")
    check("最后一句还是那句", tail and tail in after_text, after_text.strip()[-90:])
    check("她的状态还在（tone 没丢）", after_tone == before_tone,
          f"{before_tone} → {after_tone}")
    check("刷新之后也没有漏出标记", not leaked(body_text(page)))
    page.screenshot(path=str(SHOTS / "p4-reload.png"))


def case_5_sealed(page) -> None:
    print("\n── 5. 沉沦的存档进不去，新周目仍然可用 ──")
    page.click("#btn-door")
    page.wait_for_selector("#screen-title:not([hidden])")
    page.wait_for_selector(f'.save[data-save-id="{SEALED_ID}"]')

    row = page.query_selector(f'.save[data-save-id="{SEALED_ID}"]')
    check("沉沦的存档仍然列在扉上（它是记录，不是错误）", row is not None)
    if row is None:
        return
    check("被标成不可继续", "save--sealed" in (row.get_attribute("class") or ""))
    check("带封条标记", row.query_selector(".seal") is not None)

    note = row.inner_text()
    check("文案讲的是结局，不是「加载失败」",
          ("回不来" in note or "不再有想要的东西" in note) and "加载失败" not in note,
          note)

    # 用 dispatch_event 而不是 click —— 这一行带 aria-disabled，
    # Playwright 的可操作性检查会直接拒绝点它。绕开检查、把点击事件真发出去，
    # 才能验证「就算点了也没有处理函数」这件事本身。
    row.dispatch_event("click")
    time.sleep(0.6)
    check("点了也进不去（没有切到舞台）", page.is_hidden("#screen-stage"))

    # 但新周目必须还能开
    page.fill("#her-name", "新的一只")
    page.click(".newgame__go")
    page.wait_for_selector("#screen-stage:not([hidden])", timeout=60000)
    wait_idle(page)
    check("开新周目仍然可用", not page.is_hidden("#screen-stage"))
    log = page.inner_text("#log").strip()
    check("新周目有开场白", len(log) > 0, log[:80])
    check("新周目也没有漏出标记", not leaked(body_text(page)))
    page.screenshot(path=str(SHOTS / "p4-newgame.png"))


# --------------------------------------------------------------------------


def main() -> int:
    from playwright.sync_api import sync_playwright

    SHOTS.mkdir(parents=True, exist_ok=True)
    print("=" * 52)
    print("  P4 · 演出层验收")
    print("=" * 52)

    assert_marker_backend()
    make_sealed()
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page(viewport={"width": 1280, "height": 880})
            page.set_default_timeout(90000)

            errors: list[str] = []
            page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
            page.on(
                "console",
                lambda m: errors.append(f"console: {m.text}") if m.type == "error" else None,
            )

            page.goto(BASE, wait_until="load")
            page.wait_for_selector("#screen-title:not([hidden])")

            # 从「开新周目」进：她要先开口
            page.fill("#her-name", "铃")
            page.click(".newgame__go")
            page.wait_for_selector("#screen-stage:not([hidden])")
            wait_idle(page)

            check("开场白流出来了", len(page.inner_text("#log").strip()) > 0)
            opened = api("/api/status", save_id=current_save(page))
            check("开场白没有被记成一回合（state.turn 还是 0）",
                  opened.get("turn") == 0, str(opened.get("turn")))

            case_1_no_marker(page)
            case_2_real_stream(page)
            case_3_crisis(page)
            case_4_reload(page)
            case_5_sealed(page)

            print("\n── 页面报错 ──")
            real = [e for e in errors if "favicon" not in e]
            check("控制台没有 JS 报错", not real, str(real[:3]))
            browser.close()
    finally:
        drop_sealed()

    print()
    if FAILS:
        print(f"  {len(FAILS)} 项失败：")
        for f in FAILS:
            print(f"    - {f}")
        return 1
    print("  P4：全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
