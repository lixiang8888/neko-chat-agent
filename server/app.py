"""桥接层：把 ``Session`` 包成 HTTP / SSE。

这个文件**只做三件事**：

1. 把 ``Session.say`` / ``Session.opening`` 桥成 SSE
2. 把 ``Session.status`` 桥成 JSON
3. 管存档列表（列、开新周目）

这里出现的任何一行「她现在该不该……」，都是放错了地方 —— 那属于 ``src/``。
``server/`` 里不该有 ``if affection > 80``：判定只有一份实现，
复制出来的第二份迟早和第一份漂移，而漂移在长线对话里不可调试。

三个必须守住的点（见 .claude/skills/web-frontend/SKILL.md 第三节）：

* ``Session.say`` 是同步阻塞的 → ``asyncio.to_thread``
* ``on_delta`` 跑在工作线程里 → 跨线程投递必须走 ``loop.call_soon_threadsafe``
* 同一个 ``save_id`` 的回合必须串行 → per-save 锁

另外两条本项目特有的：

* **扣尾在服务端做**。SSE 没有「撤回已发送文本」这种事件，裸 delta 一旦
  推给浏览器，``<<好感度:N>>`` 就明晃晃地闪在玩家眼前了。
  这里复刻 ``src/display.py`` 的 ``StreamPrinter``。
* **``reasoning`` delta 默认丢弃**，等价于 CLI 的 ``--show-thinking`` 关闭。

启动::

    uv run uvicorn server.app:app --port 8000

⚠️ **单 worker**。``SESSIONS`` 里的会话缓存和锁都在进程内存里，
多 worker 会让同一个存档出现两份活状态，各自写盘互相覆盖。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any, AsyncIterator
from urllib.parse import parse_qs

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fastapi import FastAPI, HTTPException, Query, Request  # noqa: E402
from fastapi.responses import FileResponse, JSONResponse  # noqa: E402
from sse_starlette.sse import EventSourceResponse  # noqa: E402

from src import lockout  # noqa: E402
from src.backends import CONTENT, StubBackend  # noqa: E402
from src.context import strip_affection  # noqa: E402
from src.session import Session, TurnOutcome  # noqa: E402
from src.state import GameState, SaveStore  # noqa: E402

WEB_DIR = ROOT / "web"
STORE = SaveStore(ROOT / "saves")


# --------------------------------------------------------------------------
# 配置读取（进程内只读一次，配置在运行期不变）
# --------------------------------------------------------------------------

_config_cache: dict[str, Any] | None = None


def _config() -> dict[str, Any]:
    """``config/*.json`` 合并后的配置。和 ``Session`` 读的是同一份。"""
    global _config_cache
    if _config_cache is None:
        from src.scoring import load_rules

        _config_cache = load_rules(ROOT / "config").config
    return _config_cache


def _tail_keep() -> int:
    from src.deepseek import load_llm_config

    try:
        cfg = load_llm_config(str(ROOT / "config" / "llm.json"))
    except (OSError, ValueError):
        return 24
    return int(cfg.get("tail_keep", 24))


# --------------------------------------------------------------------------
# 后端装配 —— 和 main.py 的 build_backend 同一套规则
# --------------------------------------------------------------------------


def _build_backend() -> Any:
    """在线优先，拿不到密钥就退回离线。

    ``NEKO_OFFLINE=1`` 强制离线（Playwright 测试用它：跑得快、不花钱、
    也不依赖网络）。
    """
    if os.environ.get("NEKO_OFFLINE"):
        return StubBackend()

    from src.deepseek import build_client, load_api_key

    # 密钥只认环境变量和 keys.py（后者已 gitignore）。**永远不进浏览器。**
    key = load_api_key(keys_paths=(str(ROOT / "keys.py"),))
    if not key:
        return StubBackend()
    try:
        from src.backends import LLMBackend
        from src.deepseek import load_llm_config

        cfg = load_llm_config(str(ROOT / "config" / "llm.json"))
        return LLMBackend(build_client(cfg, key), cfg)
    except Exception:  # noqa: BLE001 - 构造失败就退回离线，别让服务起不来
        return StubBackend()


# --------------------------------------------------------------------------
# 扣尾缓冲 —— src/display.py 的 StreamPrinter 去掉终端部分
# --------------------------------------------------------------------------


class TailBuffer:
    """扣住最后 ``tail_keep`` 个字符，等整轮收完再补发合法尾部。

    模型每回合末尾要吐 ``<<好感度:N>>``（可能带全角冒号、前后空格）。
    收到即转发的话它会闪在玩家眼前，而 SSE 发出去就收不回来 ——
    所以只能在服务端扣，所有客户端就都不会错。

    与 ``StreamPrinter`` 的差别只有一处：不写终端，而是把「现在可以安全
    上屏的那一段」返回给调用方。
    """

    def __init__(self, tail_keep: int) -> None:
        self.tail_keep = max(0, int(tail_keep))
        self._buf = ""
        self._printed = 0

    def push(self, kind: str, text: str) -> str:
        """收一个 delta，返回可以安全上屏的前缀（可能为空串）。"""
        if kind != CONTENT:
            return ""  # reasoning：默认不显示、不入库、不进对话记录
        self._buf += text
        if len(self._buf) <= self.tail_keep:
            return ""
        head, self._buf = self._buf[: -self.tail_keep], self._buf[-self.tail_keep :]
        self._printed += len(head)
        return head

    def close(self, clean: str) -> str:
        """整轮收完。``clean`` 是剥掉隐藏标记之后的正文，返回要补发的尾部。"""
        if self._printed >= len(clean):
            return ""
        return clean[self._printed :]


# --------------------------------------------------------------------------
# 会话仓库：按 save_id 缓存 + per-save 锁
# --------------------------------------------------------------------------


class Sessions:
    """``Session`` 按存档缓存，别每请求重建 —— 重建会重读盘并丢掉内存状态。

    后端也是 per-save 的：``StubBackend`` 会被 ``Session.__init__`` 用
    ``bind(state)`` 绑到某个具体状态上，共享一个实例会让多个存档串味。
    在线后端本身无状态，但为了统一，同样按存档建。
    """

    def __init__(self, root: Path) -> None:
        self.root = root
        self._sessions: dict[str, Session] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def lock_for(self, save_id: str) -> asyncio.Lock:
        lock = self._locks.get(save_id)
        if lock is None:
            lock = self._locks[save_id] = asyncio.Lock()
        return lock

    def get(self, save_id: str) -> Session:
        """同步取会话。会构造、会读盘，可能抛 ``lockout.LockedSaveError``。"""
        session = self._sessions.get(save_id)
        if session is None:
            session = Session(self.root, _build_backend(), save_id=save_id)
            self._sessions[save_id] = session
        return session

    async def get_async(self, save_id: str) -> Session:
        """会话构造里有磁盘 IO，别卡住事件循环。"""
        return await asyncio.to_thread(self.get, save_id)

    def drop(self, save_id: str) -> None:
        self._sessions.pop(save_id, None)


SESSIONS = Sessions(ROOT)

# 跑在后台的回合任务。留个强引用，否则可能被 GC 掉。
_TASKS: set[asyncio.Task] = set()


app = FastAPI(title="猫娘聊天 桥接层", docs_url=None, redoc_url=None)


# --------------------------------------------------------------------------
# 小工具
# --------------------------------------------------------------------------


async def _params(request: Request, *names: str) -> dict[str, str]:
    """从 query 或请求体里取参数（表单 / JSON 都认）。

    这样 ``curl -N -X POST localhost:8000/api/turn -d 'text=你好'`` 和
    ``fetch(url + '?' + params)`` 都能用，也不需要 python-multipart。
    """
    out: dict[str, str | None] = {n: request.query_params.get(n) for n in names}
    if not all(v is not None for v in out.values()):
        body = await request.body()
        if body:
            ctype = request.headers.get("content-type", "")
            if "json" in ctype:
                try:
                    data = json.loads(body)
                except ValueError:
                    data = None
                if isinstance(data, dict):
                    for n in names:
                        if out[n] is None and data.get(n) is not None:
                            out[n] = str(data[n])
            else:
                form = parse_qs(body.decode("utf-8", "replace"))
                for n in names:
                    if out[n] is None and form.get(n):
                        out[n] = form[n][0]
    return {k: (v or "") for k, v in out.items()}


def _sse(event: str, payload: dict[str, Any]) -> dict[str, str]:
    return {"event": event, "data": json.dumps(payload, ensure_ascii=False)}


def _status(session: Session) -> dict[str, Any]:
    """⚠️ 只调 ``status()`` 的默认档 —— 定性结论，没有任何数字。

    ``verbose=True`` 那份带 ``affection`` / ``mood`` / ``care_ratio``，
    是给调试用的。把它送到浏览器就是把「数值不进玩家视线」这条线拆了：
    玩家一旦能读数字就会开始刷分，而这套评分引擎想模拟的东西正建立在
    「数字不可见」上。
    """
    st = session.status()
    st["her_name"] = session.state.her_name
    st["save_id"] = session.state.save_id
    st["ended"] = bool(session.state.withered)
    return st


def _her_state(tone: dict[str, Any]) -> dict[str, Any]:
    """给她「现在是什么样」的一份最小描述，供界面决定氛围。

    只有定性标签，没有数值 —— 这不是省略，是设计。
    """
    return {
        "label": tone.get("label", ""),
        "note": tone.get("note", ""),
        "initiative": float(tone.get("initiative", 0.0)),
        "body_language": bool(tone.get("body_language", False)),
        "nya_visible": bool(tone.get("nya_visible", True)),
    }


# --------------------------------------------------------------------------
# 存档
# --------------------------------------------------------------------------


def _peek(save_id: str) -> GameState | None:
    """只读地看一眼存档。

    ⚠️ 故意**不用** ``SaveStore.load`` —— 它在存档损坏时会把文件改名成
    ``.broken-*.json`` 再新建一份。列个表不该有这种副作用。
    """
    try:
        raw = STORE.path_for(save_id).read_text(encoding="utf-8")
        return GameState.from_dict(json.loads(raw))
    except (OSError, ValueError, TypeError):
        return None


def _save_row(save_id: str) -> dict[str, Any]:
    from src.state import tone_profile

    sealed = lockout.is_sealed(STORE, save_id)
    path = STORE.path_for(save_id)
    try:
        mtime = path.stat().st_mtime
    except OSError:
        mtime = 0.0

    state = _peek(save_id)
    row: dict[str, Any] = {
        "save_id": save_id,
        "her_name": state.her_name if state else "猫娘",
        "turn": state.turn if state else 0,
        "created_at": state.created_at if state else "",
        "updated_at": mtime,
        "sealed": sealed or bool(state and state.withered),
        "readable": state is not None,
        "tone": "",
        "note": "",
        "sealed_at": None,
    }
    if state is not None and not row["sealed"]:
        tone = tone_profile(state, _config()["gates"]["intimate"])
        row["tone"] = tone["label"]
        row["note"] = tone["note"]
    if row["sealed"]:
        # 沉沦的存档仍然显示，只是进不去。文案讲的是「结局」，不是「错误」。
        info = lockout.seal_info(STORE, save_id) or {}
        row["tone"] = "沉沦"
        row["note"] = info.get("note", "这条线里，她不再有想要的东西了。")
        row["sealed_at"] = info.get("sealed_at")
    elif state is None:
        row["note"] = "（这个存档读不出来）"
    return row


@app.get("/api/saves")
async def list_saves() -> dict[str, Any]:
    """存档列表。

    ``saves/*.json`` 才是存档。``.sealed``（沉沦封条）和 ``.broken-*.json``
    （损坏备份）都不算 —— 但沉沦的那一条**要显示出来**，只是标成不可继续。
    """
    rows = [_save_row(sid) for sid in STORE.list_saves() if not sid.startswith(".")]
    rows.sort(key=lambda r: (r["updated_at"], r["save_id"]), reverse=True)
    return {"saves": rows}


@app.post("/api/saves/new")
async def new_save(request: Request) -> dict[str, Any]:
    """开新周目。她可以被命名（对应 ``main.py`` 的 ``--name``）。"""
    params = await _params(request, "her_name")
    her_name = params["her_name"].strip() or "猫娘"

    def _mk() -> str:
        return lockout.new_game(STORE, her_name=her_name).save_id

    try:
        save_id = await asyncio.to_thread(_mk)
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"存档写不进去：{exc}") from exc
    SESSIONS.drop(save_id)
    return {"save_id": save_id, "her_name": her_name}


@app.delete("/api/saves/{save_id}")
async def delete_save(save_id: str) -> dict[str, Any]:
    """删掉一个存档。封条**一起删** —— 封条是记录，不该比存档活得久。"""
    if not STORE.exists(save_id):
        raise HTTPException(status_code=404, detail="没有这个存档。")
    SESSIONS.drop(save_id)

    def _rm() -> None:
        STORE.delete(save_id)
        lockout.seal_path(STORE, save_id).unlink(missing_ok=True)

    await asyncio.to_thread(_rm)
    return {"deleted": save_id}


# --------------------------------------------------------------------------
# 状态 / 记录
# --------------------------------------------------------------------------


@app.get("/api/status")
async def get_status(save_id: str = Query("default")) -> Any:
    try:
        session = await SESSIONS.get_async(save_id)
    except lockout.LockedSaveError as exc:
        # 这不是错误，是一个结局。前端要按这个文案渲染，
        # ⛔ 别写成「加载失败，请重试」。
        return JSONResponse(
            status_code=409,
            content={"locked": True, "save_id": save_id, "message": str(exc)},
        )
    return _status(session)


@app.get("/api/history")
async def get_history(save_id: str = Query("default")) -> Any:
    """对话记录回放（刷新页面用）。纯投影，不做任何加工。

    * 开场用的假 user 消息（``（场景开始）``）不显示 —— 终端里也不显示
    * assistant 的正文要 ``strip_affection``：history 里存的是**带标记的原文**
      （剥干净会让小模型几轮后不再输出标记，自评通道就断了），
      所以剥只发生在展示这一层，而且用的是 ``src.context`` 里那一份 regex
    * ``kind == "crisis"`` 的消息带上标记，前端据此把那一回合渲染得不一样
    """
    try:
        session = await SESSIONS.get_async(save_id)
    except lockout.LockedSaveError as exc:
        return JSONResponse(
            status_code=409,
            content={"locked": True, "save_id": save_id, "message": str(exc)},
        )

    messages = []
    for m in session.state.messages:
        if m.role not in ("user", "assistant"):
            continue
        if m.role == "user" and m.text == Session.OPENING_CUE:
            continue
        messages.append(
            {
                "role": m.role,
                "text": strip_affection(m.text) if m.role == "assistant" else m.text,
                "turn": m.turn,
                "kind": m.kind,
            }
        )
    from src.state import tone_profile

    return {
        "save_id": save_id,
        "messages": messages,
        "her": _her_state(tone_profile(session.state, _config()["gates"]["intimate"])),
        "status": _status(session),
    }


# --------------------------------------------------------------------------
# 回合 / 开场白 —— SSE
# --------------------------------------------------------------------------


async def _run_turn(
    save_id: str, runner: Any, queue: "asyncio.Queue[tuple]", loop: asyncio.AbstractEventLoop
) -> None:
    """在工作线程里跑一个回合，把事件投回事件循环。

    ⚠️ 这个任务**不挂在请求的生命周期上**。玩家中途关页/刷新，回合照样跑完
    并落盘 —— 否则他回来会发现那句话说了一半，而存档里却什么都没有。
    """

    def on_delta(kind: str, chunk: str) -> None:
        # ⚠️ 这里在工作线程里。直接往 asyncio 队列塞东西是未定义行为。
        loop.call_soon_threadsafe(queue.put_nowait, ("delta", kind, chunk))

    try:
        async with SESSIONS.lock_for(save_id):
            session = await SESSIONS.get_async(save_id)
            outcome = await asyncio.to_thread(runner, session, on_delta)
        loop.call_soon_threadsafe(queue.put_nowait, ("done", outcome))
    except lockout.LockedSaveError as exc:
        loop.call_soon_threadsafe(queue.put_nowait, ("locked", exc))
    except Exception as exc:  # noqa: BLE001 - 一个回合失败不该让整页失效
        loop.call_soon_threadsafe(queue.put_nowait, ("error", exc))


async def _stream(
    save_id: str, runner: Any, *, quiet_if_done: bool = False
) -> AsyncIterator[dict[str, str]]:
    queue: asyncio.Queue[tuple] = asyncio.Queue()
    loop = asyncio.get_running_loop()
    buffer = TailBuffer(_tail_keep())

    task = asyncio.create_task(_run_turn(save_id, runner, queue, loop))
    _TASKS.add(task)
    task.add_done_callback(_TASKS.discard)

    while True:
        item = await queue.get()
        tag = item[0]

        if tag == "delta":
            _, kind, chunk = item
            head = buffer.push(kind, chunk)
            if head:
                yield _sse("delta", {"text": head})
            continue

        if tag == "done":
            outcome: TurnOutcome = item[1]
            tail = buffer.close(outcome.line)
            if tail:
                yield _sse("delta", {"text": tail})

            session = SESSIONS._sessions.get(save_id)
            opening_skipped = quiet_if_done and not outcome.line
            payload: dict[str, Any] = {
                "line": outcome.line,
                "special": outcome.special,
                "notes": list(outcome.notes),
                "skipped": opening_skipped,
                "ending": bool(session is not None and session.state.withered),
            }
            if session is not None:
                payload["status"] = _status(session)
            yield _sse("done", payload)
            return

        if tag == "locked":
            # 封条。不是「加载失败」，是「那个她已经回不来了」。
            yield _sse("locked", {"save_id": save_id, "message": str(item[1])})
            return

        yield _sse(
            "error",
            {
                "message": str(item[1]),
                "hint": "这一回合没跑完。已经说出来的字留着，你可以再说一句。",
            },
        )
        return


@app.post("/api/turn")
async def turn(request: Request) -> EventSourceResponse:
    params = await _params(request, "save_id", "text")
    save_id = params["save_id"].strip() or "default"
    text = params["text"]

    if not text.strip():
        raise HTTPException(status_code=400, detail="说点什么吧。")

    def runner(session: Session, on_delta: Any) -> TurnOutcome:
        return session.say(text, on_delta=on_delta)

    return EventSourceResponse(_stream(save_id, runner))


@app.post("/api/opening")
async def opening(request: Request) -> EventSourceResponse:
    """开场白。走 ``session.opening()`` —— **它不评分**，``state.turn`` 不变。

    ⛔ 别在界面上给它记一回合。场景开始不是玩家的行为。
    """

    def runner(session: Session, on_delta: Any) -> TurnOutcome:
        if session.state.turn != 0 or session.state.messages:
            # 已经有进度了，不重放开场白（刷新页面走 /api/history）。
            return TurnOutcome(line="")
        return TurnOutcome(line=session.opening(on_delta=on_delta))

    params = await _params(request, "save_id")
    save_id = params["save_id"].strip() or "default"
    return EventSourceResponse(_stream(save_id, runner, quiet_if_done=True))


# --------------------------------------------------------------------------
# 静态前端
# --------------------------------------------------------------------------


if WEB_DIR.exists():
    from fastapi.staticfiles import StaticFiles

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(WEB_DIR / "index.html")

    # 挂在最后，前面那些 /api 路由先匹配。
    app.mount("/", StaticFiles(directory=str(WEB_DIR), html=True), name="web")
