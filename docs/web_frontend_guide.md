# 猫娘聊天 前端施工指南

> **读者**：接下来实际动手写前端的 agent（以及给它下指令的人）。
> 这份是操作手册，不是设计文档。按阶段走，每阶段有可执行的验收命令。
>
> 配套：`.claude/skills/web-frontend/`（项目契约）、
> `frontend-design` / `theme-factory` / `webapp-testing`（全局已装）。

---

## 0. 一句话说清任务

把 `python main.py` 这个终端循环，变成浏览器里能跑的 GalGame 演出页面。

**不是**做一个聊天软件。目标是让同一个 `Session` 对象
在网页上跑出和终端一致的回合行为 —— 流式吐字、状态演化、存档封存都要在。

---

## 1. 完成标准

做完之后，下面每一条都要能演示：

1. 浏览器里能开始一个新周目，她在页面上说出开场白（**流式**，逐字出现）
2. 输入一句话，她流式回话；**`<<好感度:N>>` 从头到尾没有在页面上出现过**
3. 页面上能看到她的定性状态（`tone`），但**看不到任何数字**
4. 触发一次危机分支，该回合视觉上可分辨，且**好感度没有变化**
5. 刷新页面，对话历史和状态都还在
6. 沉沦后该存档在列表里显示为不可继续，「开新周目」仍然可用
7. `python3 tests/run_all.py` 仍然 **407 项全过**

---

## 2. 开工前：环境缺口

当前环境实测（2026-09-16）：

| 组件 | 状态 |
|---|---|
| Python | 3.14.4 ✅ |
| uv | 0.12.12 ✅ |
| Node / npm | v24.20.0 / 11.19.0 ✅ |
| `requests` | 已装在 `.venv` ✅ |
| **FastAPI / uvicorn** | ❌ **没有** |
| **Playwright** | ❌ **没有** |

### 2.1 装 Web 依赖 —— 注意别破坏"离线零依赖"

⚠️ **不要把 fastapi 塞进 `pyproject.toml` 的 `dependencies`。**

那个文件里有一条明确的设计约束：离线模式（全部测试 + `StubBackend`）
必须能在裸 Python 下跑起来，`requests` 用的是惰性导入。
把 Web 框架变成硬依赖会破坏这个性质。

用**可选依赖组**：

```bash
uv add --optional web fastapi "uvicorn[standard]" sse-starlette
```

或者手工加到 `pyproject.toml`：

```toml
[project.optional-dependencies]
web = ["fastapi>=0.115", "uvicorn[standard]>=0.30", "sse-starlette>=2.0"]
```

安装：`uv sync --extra web`

### 2.2 装测试依赖

```bash
uv add --dev pytest playwright
uv run playwright install chromium
```

**只装 chromium，不要 `playwright install`（会拖三个浏览器）。**

### 2.3 一条硬性约束

`src/` 下**任何文件都不许 import fastapi**。桥接层是独立的 `server/` 目录。
只要守住这条，407 项离线测试就不会受影响。

---

## 3. 必读文件（按顺序）

别跳过。这些是接口的真相来源，第 4 节之后的每一条都从它们推出来。

| 顺序 | 文件 | 读什么 |
|---|---|---|
| 1 | `.claude/skills/web-frontend/SKILL.md` | 总原则、四条坑、目录约定 |
| 2 | `.claude/skills/web-frontend/references/contract.md` | 精确签名、字段表、枚举值 |
| 3 | `src/session.py` | `say` / `opening` / `status` 的实现 |
| 4 | `src/display.py` | **扣尾逻辑**，桥接层要复刻的就是这个 |
| 5 | `src/context.py` 的 `parse_affection` / `strip_affection` | 标记的 regex，**直接复用别重写** |
| 6 | `src/lockout.py` | 封存语义，决定"存档不可继续"怎么展示 |
| 7 | `main.py` | 终端里这些事是怎么串起来的，网页照做 |

---

## 4. 要加载的 skill

| 时机 | skill | 用途 |
|---|---|---|
| 开工前 | `web-frontend` | 项目契约，**必读** |
| P2 开始前 | `frontend-design` | 定视觉调性，挡掉模板化 AI 味 |
| P2 配色时 | `theme-factory` | 10 套预设主题可参考（见下方限制） |
| P4 测试时 | `webapp-testing` | Playwright 驱动，验证流式渲染 |

**`theme-factory` 的 10 套主题**：arctic-frost、botanical-garden、desert-rose、
forest-canopy、golden-hour、midnight-galaxy、modern-minimalist、ocean-depths、
sunset-boulevard、tech-innovation。

⚠️ **但这些都是英文场景的配色，且不含中文字体方案。**
这个项目的界面是中文，直接用会显得不对味。只用它的**配色思路**，
字体必须自己解决中文字形（见 P2）。

**`canvas-design` 已装但对本项目** —— 它的 80 个字体全是拉丁字体
（最大的 191KB，中文一个字重就要 5MB+），做不了中文界面。
它的用途是出海报/静态图，比如以后要做标题图、宣传图再用。

---

## 5. 施工阶段

### P0 · 桥接层（先跑通，别碰界面）

目标：`curl` 能拿到 SSE 流，且**流里没有隐藏标记**。

新建 `server/app.py`。只做三件事：

1. 把 `Session.say` 桥成 SSE
2. 把 `Session.status` 桥成 JSON
3. 管存档列表

**核心实现要点**（三条都会咬人）：

```python
@app.post("/api/turn")
async def turn(save_id: str = "default", text: str = ""):
    q: asyncio.Queue = asyncio.Queue()
    loop = asyncio.get_running_loop()

    def on_delta(kind: str, chunk: str) -> None:
        # ① on_delta 在工作线程里被调用，必须跨线程投递
        loop.call_soon_threadsafe(q.put_nowait, ("delta", kind, chunk))

    async def run():
        async with lock_for(save_id):          # ③ 同档串行
            s = get_session(save_id)           # ② 按 save_id 缓存实例
            out = await asyncio.to_thread(s.say, text, on_delta)
            loop.call_soon_threadsafe(q.put_nowait, ("done", out))
    # ...
```

- **① `call_soon_threadsafe`** — `on_delta` 跑在 `to_thread` 的工作线程里，
  直接 `q.put_nowait` 是未定义行为。
- **② 按 `save_id` 缓存 `Session`** — 别每请求重建（会重读盘、丢内存状态）。
  也别多 worker 部署（内存状态会分叉）—— 单 worker，或用外部锁。
- **③ per-save 锁** — `Session` 持有可变状态且每回合落盘，并发会互相踩。

**扣尾必须在服务端做。** 复刻 `src/display.py` 的 `StreamPrinter`：
扣住最后 `tail_keep`（从 `llm_cfg["tail_keep"]` 取，默认 24）个字符，
整轮收完、`strip_affection` 之后再把合法尾部补发出去。

> SSE 没有"撤回已发送文本"这种事件。发出去就是发出去了。
> 服务端扣尾，所有客户端就都不会错。

`reasoning` 类型的 delta：**默认丢弃，不推给浏览器**。
等价于终端里 `--show-thinking` 关闭。要做成可选，也必须默认关。

**验收**：

```bash
uv run uvicorn server.app:app --port 8000 &
curl -N -X POST "localhost:8000/api/turn" -d 'text=你好'
# 期望：分多批到达的 data: 行；最后一条是 done；全程 grep 不到「好感度」
```

### P1 · 前端骨架 + 真流式

目标：能在页面上完成一个回合，字是真的流出来的。

- `web/index.html` + `web/app.js` + `web/style.css`
- 用 `EventSource` 不行（要 POST），用 `fetch` + `ReadableStream` 读 SSE
- 打字机效果**由真实 delta 驱动**。⛔ 不许用 `setInterval` 匀速吐一段
  已经拿到的完整文本 —— 那是把真流式降级成动画，
  首字延迟和语速变化里的信息全丢了

**验收**：打开 DevTools Network，能看到 `/api/turn` 是一个持续数秒的
流式响应，而不是一次性返回。

### P2 · 视觉（GalGame 演出）

**先加载 `frontend-design` skill**，让它定调性。

这个项目特有的约束：

- **参考视觉小说，不是 IM。** 对话框 + 名牌 + 立绘/场景，不是左右分栏气泡。
- **她的状态是氛围。** `tone` 的变化体现在立绘、配色、语速上，
  ⛔ 不是右上角挂"好感度 78%"。
- **中文字体自己解决。** 系统字体栈至少要覆盖：
  `"Noto Sans SC", "Source Han Sans SC", "PingFang SC", "Microsoft YaHei", sans-serif`。
  要自托管就下 Noto Sans SC（OFL 协议）。
- **危机回合视觉可分辨但克制。** 她在这条分支里"停止表演"——
  动作变少、不撒娇。表现成气氛的变化（色调沉下来、动效停掉），
  ⛔ 不要弹窗、红色警告条、"检测到关键词"这类措辞。

**验收**：截图。把截图和 `tone` 的几个档位（警惕→依赖→独属→认定）
摆在一起看，应该能感觉出不同，但说不出是哪个数字变了。

### P3 · 存档与周目

- 存档列表读 `saves/*.json`，**过滤掉** `.sealed` 和 `.broken-*.json`
- 加载被拒（`lockout.LockedSaveError`）时，文案要讲清
  **"那个她已经回不来了"**，不是"加载失败，请重试"
- 沉沦后（`state.withered`）本局结束，「开新周目」仍可用
- 存档名/她名字在开新周目时可填（对应 `main.py` 的 `--name`）

**验收**：手工造一个 `saves/test.json` + `saves/test.sealed`，
确认列表里显示为不可继续，且点不进去。

### P4 · 测试

**加载 `webapp-testing` skill**，先跑 `--help` 再调脚本。

它的 `scripts/with_server.py` 正好管这个场景（后端 + 前端两个 server）：

```bash
python ~/.claude/skills/webapp-testing/scripts/with_server.py \
  --server "uv run uvicorn server.app:app --port 8000" --port 8000 \
  -- python your_test.py
```

必须自动化验证的（眼睛看容易漏）：

1. **隐藏标记不上屏** —— 断言页面全文匹配不到 `好感度` / `<<`
2. **流是真的** —— 记录首个 token 到最后一个 token 的时间差 > 0
3. **危机回合好感不变** —— 危机前后各取一次 `status`，比对
4. **刷新后状态还在**
5. **沉沦存档不可进**

---

## 6. 禁区

| ⛔ 不许 | 为什么 |
|---|---|
| 在 `web/` 或 `server/` 里重算好感/档位/沉沦 | 前端一旦"顺手也算一下"，两边数值就漂移，长线对话里不可调试 |
| 前端调 `status(verbose=True)` | 数字进玩家视线 = 玩家开始刷分 = 整个评分引擎想模拟的东西失效 |
| 自己写剥标记的 regex | 用 `src.context.AFFECTION_RE` / `strip_affection`，两处 regex 必然分叉 |
| 把 API Key 送到浏览器 | 密钥只在服务端（`DEEPSEEK_API_KEY` 或 `keys.py`，后者已在 `.gitignore`） |
| `src/` 里 import fastapi | 会破坏"离线 407 项测试零依赖"这个性质 |
| 把 fastapi 写进 `dependencies` | 同上，用 `--optional web` |
| 给开场白记一回合 | `session.opening()` 不评分，`state.turn` 不变 |
| 用假动画模拟流式 | 见 P1 |

---

## 7. 故障对照表

| 现象 | 原因 | 处理 |
|---|---|---|
| 页面上闪出 `<<好感度:5>>` | 裸 delta 直接推给浏览器了 | 扣尾挪到服务端 |
| 对话记录里混进模型的思考 | `reasoning` delta 没丢 | 只推 `content` |
| 两个标签页同时说话，状态错乱 | 没加 per-save 锁 | 见 P0 ③ |
| 重启服务后进度倒退 | 多 worker，内存状态分叉 | 单 worker |
| 她说半句就没了 | 流中断。**不要补提示语** | 已吐字时后端不补降级文案；前端用已收内容收尾 |
| 整段变成「（她张了张嘴…）」 | 一个字没吐出来就断 | 这是后端的正常降级，不是 bug |
| 中文显示成方块 | 字体栈没覆盖 CJK | 见 P2 |
| 读档报 `LockedSaveError` | 该档已沉沦封存 | 不是错误，见 P3 文案要求 |

---

## 8. 交付清单

- [x] `server/app.py` — 桥接层，不含任何游戏逻辑
- [x] `web/index.html` `web/app.js` `web/style.css`
- [x] `pyproject.toml` 加了 `[project.optional-dependencies] web`
- [x] `saves/` 相关文件确认在 `.gitignore` 里（原本就有）
- [x] `python3 tests/run_all.py` → 407/407
- [x] Playwright 测试脚本，覆盖 P4 的五条（`tests/web/test_galgame.py`，37 项）
- [x] 截图：四个 `tone` 档位（另加扉 / 危机 / 终幕 / 窄屏）

### 8.1 施工记录（2026-09-16 完成）

```bash
python3 tests/run_all.py                              # 11/11 套件，407 项
uv run python tests/web/test_stream_hygiene.py        # 流卫生 15 项
python ~/.claude/skills/webapp-testing/scripts/with_server.py \
  --server "uv run python tests/web/run_marker_server.py 8000" --port 8000 \
  -- python tests/web/test_galgame.py                 # P4 37 项
```

`src/` 一行未动（`git status` 只有 `pyproject.toml` 的依赖组变更）。

**计划外但必要的三件事：**

1. **多了一个 `GET /api/history`。** 完成标准第 5 条要求「刷新页面，对话历史和状态都还在」，
   而 `status` 不带对话记录。它是个纯投影（跳过开场用的假 user 消息、
   用 `src.context.strip_affection` 剥 assistant 正文），不含任何判定。
   另加了 `DELETE /api/saves/{id}` 给存档管理用。
2. **`tests/web/with_browser.sh`。** 裸 WSL 上 chromium 起不来
   （`libnss3.so => not found`，装它要 root）。它用 `apt-get download` 把
   `.deb` 解到用户目录，靠 `LD_LIBRARY_PATH` 生效 —— 不动系统。
3. **`tests/web/run_marker_server.py` + `MarkerBackend`。**
   `StubBackend` 一个字都不吐 `<<好感度:N>>`，拿它验证「标记不上屏」是在验证
   「没有标记的时候页面上没有标记」。假后端既吐标记（还故意切在分片中间），
   也在分片之间停顿，第 2 条验收才有可测的时间差。

### 8.2 两个会咬人的地方（实测踩到）

| 现象 | 原因 | 处理 |
|---|---|---|
| 改了存档文件，页面上还是老状态 | 桥接层按 `save_id` 缓存 `Session`，外部改写文件它不重读 | 造存档**必须在起服务之前**；`fixtures.py` 因此要摆在 `uvicorn` 前面 |
| 测试全绿，但「标记不上屏」是假的 | 端口上还留着上一次的服务，`with_server.py` 只看端口通不通，新服务起不来它不报错 | `test_galgame.py` 开头有 `assert_marker_backend()` 前置校验，连错后端直接退出 |

还有一条不是坑、是约定：**危机回合 `state.turn` 不会 +1**。
`Session.say` 在危机分支里就返回了，不走 `engine.process_turn` ——
「本回合不评分」的直接后果就是没有回合结算。消息照常进 history，她仍然会记得。

---

## 9. 给下指令的人

如果下一个 agent 跑偏了，检查这三件事：

1. 它有没有先读 `.claude/skills/web-frontend/references/contract.md`？
   没读就会开始自己发明接口。
2. 它是不是在往界面上加数字？这是最容易发生的偏离，
   而且加的人会觉得"这样更直观"。
3. `server/` 里有没有出现 `if affection > 80` 这类判断？
   有就是游戏逻辑漏出去了。
