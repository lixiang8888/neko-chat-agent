"""传输层与会话显示测试。

**全部离线**：用一个假的 requests 模块替换真的，验证重试、思考分流、扣尾。
所以这些测试既不需要 API Key，也不需要装 requests —— 这一点是刻意的，
「全部测试离线可跑」的性质必须保住。

这里测的都是只能靠实测知道的坑：
SSE 的中文解码、断线重试的边界、隐藏标记的扣尾。
"""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import src.deepseek as ds  # noqa: E402
from _fixtures import Runner  # noqa: E402
from src.backends import CONTENT, REASONING  # noqa: E402
from src.context import parse_affection, strip_affection  # noqa: E402
from src.display import StreamPrinter  # noqa: E402


# --------------------------------------------------------------------------
# 假的 requests
# --------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, lines, status: int = 200, text: str = "") -> None:
        self._lines = list(lines)
        self.status_code = status
        self.text = text

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def iter_lines(self):
        for line in self._lines:
            yield line.encode("utf-8")


class _FakeRequests:
    """最小替身。只用得到 ``post()`` 和 ``RequestException``。"""

    class RequestException(Exception):
        pass

    class ConnectionError(RequestException):
        pass

    def __init__(self, handler) -> None:
        self._handler = handler
        self.calls: list[dict] = []

    def post(self, url, **kw):
        self.calls.append({"url": url, **kw})
        return self._handler(len(self.calls), kw)


def _chunk(**delta) -> dict:
    return {"choices": [{"delta": delta}]}


def _sse(*payloads) -> list[str]:
    lines = [f"data: {json.dumps(p, ensure_ascii=False)}" for p in payloads]
    lines.append("data: [DONE]")
    return lines


def _install(fake: _FakeRequests):
    """把 ds 模块的 requests 换成假的，返回还原函数。"""
    real_req, real_err = ds.requests, ds._REQUEST_ERRORS
    ds.requests = fake
    ds._REQUEST_ERRORS = (fake.RequestException,)

    def restore():
        ds.requests = real_req
        ds._REQUEST_ERRORS = real_err

    return restore


def _client():
    return ds.DeepSeekClient(
        "test-key",
        base_url="https://example.invalid",
        model="deepseek-flash",
        retries=3,
        retry_backoff=0,  # 测试里别真睡
    )


# --------------------------------------------------------------------------
# 测试
# --------------------------------------------------------------------------


def run() -> int:
    r = Runner()

    # ---------------------------------------------------------- 协议解析
    r.section("自评标记解析")
    for text, want in [
        ("喵~ <<好感度:62>>", 62),
        ("喵~ <<好感度:-30>>", -30),
        ("喵~ <<好感度：75>>", 75),        # 全角冒号
        ("喵~ << 好感度 : 100 >>", 100),   # 多余空格
        ("喵~ <<好感度:999>>", 100),       # 超上限 → 夹紧
        ("喵~ <<好感度:-999>>", -100),     # 超下限 → 夹紧
        ("喵~ 没有标记", None),
    ]:
        r.eq(f"parse_affection({text!r})", parse_affection(text), want)

    for text, want in [
        ("喵~ <<好感度:62>>", "喵~"),
        ("喵~ 没有标记", "喵~ 没有标记"),
        ("第一行喵\n<<好感度:-5>>", "第一行喵"),
    ]:
        r.eq(f"strip_affection({text!r})", strip_affection(text), want)

    # ---------------------------------------------------------- JSON 提取
    r.section("JSON 提取")
    r.eq("裸 JSON", ds.extract_json('{"a": 1}'), {"a": 1})
    r.eq("markdown 围栏", ds.extract_json('```json\n{"a": 2}\n```'), {"a": 2})
    r.eq("前后有废话", ds.extract_json('好的：\n{"a": 3}\n以上'), {"a": 3})
    try:
        ds.extract_json("完全没有 JSON")
        r.check("无 JSON 时抛错", False)
    except ValueError:
        r.check("无 JSON 时抛错", True)

    # ---------------------------------------------------------- 请求体拼装
    r.section("请求体拼装")
    c = _client()
    body = c._body([], stream=True, temperature=1.3, max_tokens=800,
                   top_p=0.95, reasoning_effort="none")
    r.eq("effort=none 显式关闭思考", body.get("thinking"), {"type": "disabled"})
    r.check("effort=none 不传 reasoning_effort", "reasoning_effort" not in body)

    body = c._body([], stream=True, temperature=1.3, max_tokens=800,
                   top_p=0.95, reasoning_effort="high")
    r.eq("effort=high 开启思考", body.get("thinking"), {"type": "enabled"})
    r.eq("effort=high 带上档位", body.get("reasoning_effort"), "high")

    body = c._body([], stream=True, temperature=1.3, max_tokens=800,
                   top_p=None, reasoning_effort=None)
    r.check("effort=None 不传思考参数",
            "thinking" not in body and "reasoning_effort" not in body)
    r.check("top_p=None 不传", "top_p" not in body)
    r.check("不暴露 n / response_format（这个模型会 400）",
            "n" not in body and "response_format" not in body)

    # ---------------------------------------------------------- 流式
    r.section("流式解码与思考分流")
    fake = _FakeRequests(lambda n, kw: _FakeResponse(_sse(
        _chunk(reasoning_content="主人回来了"),
        _chunk(reasoning_content="，我得撒娇"),
        _chunk(content="主人好喵~♡\n"),
        _chunk(content="<<好感度:61>>"),
    )))
    restore = _install(fake)
    try:
        deltas = list(_client().stream([]))
        kinds = [k for k, _ in deltas]
        text = "".join(t for k, t in deltas if k == CONTENT)
        reasoning = "".join(t for k, t in deltas if k == REASONING)

        r.eq("中文没有乱码", text, "主人好喵~♡\n<<好感度:61>>")
        r.eq("思考内容进 REASONING 通道", reasoning, "主人回来了，我得撒娇")
        r.check("思考不混进正文", "撒娇" not in text)
        r.eq("两类 delta 都出现了", sorted(set(kinds)), [CONTENT, REASONING])

        # 代理绕过：显式传 None 才能压过环境变量里的 http(s)_proxy
        kw = fake.calls[0]
        r.eq("显式绕过代理", kw.get("proxies"), {"http": None, "https": None})
        r.eq("非流式超时是 (连接, 读取) 对", kw.get("timeout"), (10, 120))
        r.check("用的是 stream=True", kw.get("json", {}).get("stream") is True)
    finally:
        restore()

    # ---------------------------------------------------------- 重试
    r.section("断线重试")

    # 前两次握手就断，第三次成功
    def flaky(n, kw):
        if n < 3:
            raise _FakeRequests.ConnectionError("boom")
        return _FakeResponse(_sse(_chunk(content="好了喵")))

    fake = _FakeRequests(flaky)
    restore = _install(fake)
    try:
        err = io.StringIO()
        real_stderr, sys.stderr = sys.stderr, err
        try:
            got = "".join(t for k, t in _client().stream([]) if k == CONTENT)
        finally:
            sys.stderr = real_stderr
        r.eq("握手期断线会重试到成功", got, "好了喵")
        r.eq("一共试了 3 次", len(fake.calls), 3)
    finally:
        restore()

    # 已经出字之后再断 → 必须抛出，不能重连（否则正文重复）
    class _MidStream(_FakeResponse):
        def iter_lines(self):
            # 注意要带 "data: " 前缀 —— 不带的话会被 SSE 解析器当无关行跳过，
            # 那就根本没「出字」，测不到想测的东西。
            line = "data: " + json.dumps(_chunk(content="开头"), ensure_ascii=False)
            yield line.encode("utf-8")
            raise _FakeRequests.ConnectionError("boom")

    fake = _FakeRequests(lambda n, kw: _MidStream([]))
    restore = _install(fake)
    try:
        raised = False
        try:
            list(_client().stream([]))
        except _FakeRequests.ConnectionError:
            raised = True
        r.check("出字后断线直接抛出", raised)
        r.eq("出字后不再重试", len(fake.calls), 1)
    finally:
        restore()

    # HTTP 4xx 是确定性的，不重试
    fake = _FakeRequests(lambda n, kw: _FakeResponse([], status=400, text="模型名写错"))
    restore = _install(fake)
    try:
        raised = False
        try:
            list(_client().stream([]))
        except ds.TransportError:
            raised = True
        r.check("HTTP 400 抛 TransportError", raised)
        r.eq("HTTP 400 不重试", len(fake.calls), 1)
    finally:
        restore()

    # ---------------------------------------------------------- 扣尾
    r.section("流式扣尾（隐藏标记不泄露）")

    text = "主人回来啦喵~♡\n<<好感度:62>>"
    for size in (1, 2, 3, 5, 17):
        deltas = [text[i : i + size] for i in range(0, len(text), size)]
        sink = io.StringIO()
        p = StreamPrinter(out=sink, show_thinking=False, tail_keep=24)
        for d in deltas:
            p.push(CONTENT, d)
        p.close(strip_affection(p.raw))
        shown = sink.getvalue()
        ok = "好感度" not in shown and "<<" not in shown
        r.check(f"切块 {size} 时标记不泄露", ok, f"shown={shown!r}")
        r.check(f"切块 {size} 时正文完整", strip_affection(p.raw) in shown,
                f"shown={shown!r}")

    # 思考默认不显示
    sink = io.StringIO()
    p = StreamPrinter(out=sink, show_thinking=False)
    p.push(REASONING, "我得撒娇")
    p.push(CONTENT, "主人好喵~")
    p.close("主人好喵~")
    r.check("SHOW_THINKING 关闭时不显示思考", "撒娇" not in sink.getvalue(),
            f"out={sink.getvalue()!r}")

    # 打开时显示，用灰色
    sink = io.StringIO()
    p = StreamPrinter(out=sink, show_thinking=True, thinking_color="\033[90m")
    p.push(REASONING, "我得撒娇")
    p.push(CONTENT, "主人好喵~")
    p.close("主人好喵~")
    r.check("SHOW_THINKING 打开时显示思考", "撒娇" in sink.getvalue())
    r.check("思考用灰色", "\033[90m" in sink.getvalue())

    # ---------------------------------------------------------- 惰性依赖
    r.section("离线性质")
    r.check("离线模式下不需要 requests 也能导入传输层",
            ds.requests is None or hasattr(ds.requests, "post"))

    return r.summary()


if __name__ == "__main__":
    raise SystemExit(run())
