---
name: web-frontend
description: 给猫娘聊天 做 Web 前端（浏览器界面、聊天页、GalGame 演出页、SSE 流式对接、存档列表）时必须先读这份。当用户说「加个网页 / Web UI / 前端 / 界面 / 页面 / 聊天框 / 浏览器里跑 / 部署成网页 / 做个 GalGame 界面」时使用。包含后端对接契约、四条会踩的坑、目录约定与视觉方向。
---

# 猫娘聊天 前端

这个工程的**全部状态与判定都在 Python 侧**。前端不是「另一个客户端」，
是 `Session` 的一层皮肤 —— 它显示状态、转发输入、渲染流，不拥有任何规则。

写任何前端代码之前，先把这条记住：

> **前端不得复制任何游戏逻辑。** 好感怎么算、什么时候压缩记忆、沉沦怎么判定，
> 全在 `src/`。前端一旦开始「顺手也算一下」，两边的数值就会开始漂移，
> 而漂移在长线对话里是不可调试的。

---

## 一、后端契约

精确的方法签名、字段表、事件格式在 [references/contract.md](references/contract.md)。
这里只列最容易写错的部分。

> **要完整施工计划的话**，看 [docs/web_frontend_guide.md](../../../docs/web_frontend_guide.md) ——
> 分五个阶段、每阶段带可执行的验收命令、环境缺口清单、禁区与故障对照表。

### 一次回合

```python
session.say(player_text, on_delta=None) -> TurnOutcome
```

`on_delta(kind, chunk)` 是流式回调，`kind` 只有两个取值：

| kind | 含义 | 前端默认 |
|---|---|---|
| `"content"` | 正文 | 实时上屏 |
| `"reasoning"` | 模型的思考 | **不显示**（等价于 CLI 的 `--show-thinking` 关闭） |

`reasoning` 不进正文、不进 history。它不该出现在对话记录里，
也不该被存进前端的状态树 —— 塞回去既费 token，又会让模型纠结上一轮的自我分析。

`TurnOutcome` 的四个字段：

- `line` — **已经剥掉隐藏标记的正文**。上屏的最终文本用这个。
- `special` — `"crisis"` / `"jailbreak"` / `None`
- `notes` — 给玩家看的短提示列表，例如 `["自评佐证", "记忆蒸馏 T2"]`
- `report` — 引擎的回合报告，**前端不要碰**

### 状态查询

```python
session.status(verbose=False) -> dict
```

⚠️ **默认 `verbose=False` 是刻意的，不是省事。**

返回的只有定性结论：`tone`（她现在是什么样）、`note`（一句描述）、
`phase`、`branch`、`intimacy`（`"已解锁"` / `"未解锁"`）、`frozen_left`。

`verbose=True` 才会带 `affection`、`mood`、`care_ratio`、`neg_depth`、`self_affection`
这些原始数字 —— 那是给调试用的。

**前端只允许用 `verbose=False`。** 这是「数值不进玩家视线」原则的入口，
CLI 里 `/status` 不带 `--debug` 也是同一条线。把 78.4 画成进度条挂到界面上，
等于把整个工程的设计前提拆掉了 —— 玩家一旦能读数字，就会开始刷分，
而刷分会让这套评分引擎想模拟的东西全部失效。

要表现「她变了」，用 `tone` 的变化（警惕 → 依赖 → 独属 → 认定），
不是用数值增长。这条同样适用于 `report` 里的任何字段。

---

## 二、四条会踩的坑

### 1. 扣尾：`<<好感度:N>>` 不能闪

模型每回合末尾会输出隐藏标记 `<<好感度:N>>`（可能带全角冒号、前后空格）。
**收到的瞬间就上屏的话，它会明晃晃地闪在玩家眼前。**

CLI 的解法在 [src/display.py](../../../src/display.py)：扣住最后 `tail_keep`
个字符不打印，等整轮收完、剥掉标记，再补打合法的尾部。`tail_keep` 来自
`config/llm.json`（默认 24）。

**桥接层必须做同样的事，不要把裸 delta 直接推给浏览器。** 建议按 CLI 的
`StreamPrinter` 实现，剥标记的 regex 直接用 `src/context.py` 的
`strip_affection` / 常量 `AFFECTION_RE` —— 别自己再写一个，两处 regex 迟早会分叉。

推流协议里没有「撤回已发送文本」这种事件。发出去就是发出去了。

### 2. 危机回合不评分，视觉上要能分辨

`special == "crisis"` 时，这一回合**不涨好感、不扣好感、不进 care window、
不写进剧情档案**（见 [src/session.py](../../../src/session.py) 的 `_crisis_response`）。
她在这条分支里**停止表演**：动作变少、不撒娇、不绕圈子，但仍然是猫娘。

前端要做的：让这一回合和普通回合**看得出区别**，但**克制**。
不要弹窗、不要红色警告条、不要「检测到关键词」这类措辞 ——
那会把它变成一次系统事件，而它应该只是一次她安静下来的对话。

### 3. 沉沦之后存档是死的

`state.withered` 为真时，存档已经被 `lockout.seal()` 盖上封条：

- `saves/<save_id>.sealed` 文件存在
- 这个存档**永久不可继续**，重启、读档都无效
- 但是**允许开新周目**

再次加载会抛 `lockout.LockedSaveError`。前端要接住它，
并且把「那个她已经回不来了」讲清楚 —— 不是「加载失败，请重试」。

CLI 的对应行为：`main.py` 里返回退出码 2（读档被拒）和 3（本局刚沉沦）。

### 4. 演出可能整段失败，但不会中断

`LLMBackend.speak_stream` 在网络出错且**一个字都没吐出来**时，
会补一句降级文案 `（她张了张嘴，但什么也没说出来喵……）`。

如果流已经开始再断开，**不会**补 —— 补了会和已打印的正文粘在一起。
前端遇到流中断，用它已经收到的内容收尾即可，不要再追加任何提示语。

`Session.say` 本身也可能抛（网络异常）。CLI 的做法是不退出循环，
打一行 `[请求失败]` 然后等下一条输入。前端照做：一个回合失败不该让整页失效。

---

## 三、桥接层

推荐 FastAPI + SSE。三个必须注意的点：

```python
@app.post("/api/turn")
async def turn(save_id: str, text: str):
    queue: asyncio.Queue = asyncio.Queue()
    loop = asyncio.get_running_loop()

    def on_delta(kind: str, chunk: str) -> None:
        # on_delta 是在工作线程里被调的，必须跨线程投回事件循环
        loop.call_soon_threadsafe(queue.put_nowait, ("delta", kind, chunk))

    async def run():
        session = get_session(save_id)
        outcome = await asyncio.to_thread(session.say, text, on_delta)
        loop.call_soon_threadsafe(queue.put_nowait, ("done", outcome))
    ...
```

1. **`Session.say` 是同步阻塞的**，用 `asyncio.to_thread` 或
   `run_in_threadpool`，别直接 `await`。
2. **`on_delta` 跑在工作线程里**，往 asyncio 队列塞东西必须走
   `call_soon_threadsafe`，否则是未定义行为。
3. **同一个存档的回合必须串行。** `Session` 持有可变状态并且每回合落盘
   （`src/state.py` 的 `SaveStore`，临时文件 + rename）。
   同一 `save_id` 并发两个回合会互相踩。用 per-save 的锁把 `say` 括起来。

另外：**`Session` 实例按存档缓存**，别每个请求重建 —— 重建会重新读盘，
而你手里那份内存状态会被丢掉。但也要注意它的 `state` 是活的，
多进程部署（gunicorn 多 worker）会让内存状态分叉，那种情况下要么单 worker，
要么把锁和缓存挪到外部。

**API Key 绝对不能进浏览器。** 密钥只在服务端（`DEEPSEEK_API_KEY` 环境变量
或 `keys.py`，后者已在 `.gitignore`）。前端不接触任何模型端点，
它只跟你的桥接层说话。

---

## 四、目录约定

```
web/                 前端源码（新增，跟 src/ 平级）
  index.html
  app.js  style.css
server/              桥接层（新增）
  app.py              FastAPI，包 Session，不实现任何游戏逻辑
```

桥接层**只做三件事**：把 `say` 桥成 SSE、把 `status` 桥成 JSON、管存档列表。
任何一行「判断她现在该不该……」，都说明这段逻辑放错了地方，应该回到 `src/`。

存档列表读 `saves/*.json`，注意跳过 `.sealed`（沉沦封条）和
`.broken-*.json`（损坏备份）—— 这两个都在 `.gitignore` 里。

---

## 五、视觉方向

**先读 `frontend-design` skill**（Anthropic 官方）定调性，它能挡住最典型的
AI 味模板化产出。这里只补这个工程特有的约束：

- **GalGame 演出，不是聊天软件。** 对话框、名牌、立绘/场景、点击推进 ——
  参考的是视觉小说，不是 IM 应用。别做成左右分栏的聊天气泡。
- **流式要真，不要假。** 打字机效果必须由真实 SSE delta 驱动。
  用 `setInterval` 匀速吐一段已经拿到的完整文本，是把真流式降级成动画 ——
  首字延迟和语速变化里的信息就全丢了。
- **她的状态是氛围，不是仪表盘。** `tone` 的变化应该体现在
  立绘、配色、语速这些地方，不是右上角挂一个「好感度 78%」。
- **开场白走 `session.opening()`**，它**不评分**（见 `src/session.py`）——
  别在界面上给它记一回合，`state.turn` 不会因此增加。

---

## 六、改完要跑测试

```bash
python3 tests/run_all.py        # 400+ 项，离线，不需要 API Key
```

前端的改动不该让任何一项失败。如果它失败了，说明改动动到了 `src/` ——
那就不只是前端改动了，回头看看是不是把游戏逻辑漏到桥接层里去了。
