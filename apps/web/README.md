# Legacy · Web 前端

Agent 控制台：流式对话 + **工具调用过程可视化的**界面。
不是把回答渲染成气泡那么简单 —— 它要把 ReAct 循环的每一步摊开给人看。

```
┌──────────────────────────────────────────────────────────────┐
│ JP Legacy   deepseek-chat ● session a1b2c3d4 ▸ memory 存储 │
├───────────────┬──────────────────────────────────────────────┤
│ 会话          │  用户：帮我看看简历和这个 JD 匹配度            │
│ ▸ 简历匹配 3轮 │  ┌ 思考过程 · 3 步 · 3 次工具调用 ──────────┐  │
│   模拟面试 6轮 │  │ 第 1 步  我先查一下你的简历…              │  │
│ + 新建会话    │  │   ✓ read_resume   1.8ms                  │  │
│               │  │   ✓ search_jobs   412ms  ✂已截断          │  │
│               │  │ 第 2 步  …                                │  │
│               │  └──────────────────────────────────────────┘  │
│               │  最终答案（Markdown：标题/表格/代码块/引用）    │
│               │  ── 3 步 · 3 次工具调用 · 5,756 tokens · 正常结束│
│               │  [ 输入框                        ] [发送 ↵]    │
└───────────────┴──────────────────────────────────────────────┘
```

## 运行

```powershell
# 1) 后端（另一个终端）：项目根目录
.\scripts\dev.ps1 serve

# 2) 前端
cd apps\web
pnpm install
pnpm dev            # http://localhost:5173
```

开发期通过 vite 代理访问后端：`/api` 与 `/healthz` 都被转发到
`http://127.0.0.1:8000`（见 `vite.config.ts`），所以没有跨域问题，前端代码里
也**不出现任何绝对地址**。要指向别的后端时用 `VITE_API_BASE`（例如部署到
独立域名），或设 `LEGACY_BACKEND` 改代理目标。

## 构建与测试

```powershell
pnpm typecheck      # tsc --noEmit，strict + noUncheckedIndexedAccess
pnpm build          # 类型检查 + vite build → dist/
pnpm test           # 43 个纯函数测试，零测试框架依赖
```

`pnpm test` 用的是 **Node 自带的测试运行器**（需要 Node ≥ 22.18：默认开启
TypeScript 类型剥离，可以直接 `import './src/lib/sse.ts'`）。
不装 Vitest/Jest 的理由和下面"零运行时依赖"的理由一样：这些逻辑全是纯函数，
不需要 DOM、不需要 mock 框架，`node:test` + `node:assert` 就够了。
（`--test-isolation=none` 让所有测试跑在同一进程，省掉子进程开销。）

## 目录

```
src/
├─ lib/            ← 纯逻辑，不依赖 React，全部可单测
│  ├─ types.ts       后端契约的类型镜像（事件、会话、元信息）
│  ├─ sse.ts         SSE 解帧器 + 流消费器（手写，见下）
│  ├─ stream.ts      事件 → UI 状态的归约器（纯函数）
│  ├─ markdown.ts    手写 Markdown 解析器（输出 AST，不产出 HTML 字符串）
│  ├─ api.ts         唯一的出网入口，统一错误成 ApiError
│  └─ format.ts      时间/token/耗时的格式化
├─ hooks/          ← 状态与副作用
│  ├─ useChat.ts      发送、消费 SSE、中断、异常分类
│  ├─ useSessions.ts  会话列表/详情（含请求竞态处理）
│  ├─ useServerInfo.ts /healthz + /api/meta + /api/tools
│  ├─ useTheme.ts     暗/亮/跟随系统三态
│  └─ useStickToBottom.ts  聊天黏底滚动
├─ components/     ← 展示层（12 个组件 + 一套手写图标）
└─ styles/         ← tokens + base + ui + layout + chat + markdown + tools
test/              ← Node 内置测试运行器
```

## 技术选型：为什么一个依赖都不装

| 通常会用 | 本项目 | 理由 |
|---|---|---|
| Tailwind / UI 库 | 手写 CSS + CSS 变量 | token 集中在一处，深浅两套主题只差第二层赋值；零构建风险 |
| react-markdown | `lib/markdown.ts` | 只需六种语法，却要拖进几十个包；自己写还能**产出 React 元素而不是 HTML 字符串**，从结构上避免 XSS |
| Redux / Zustand | React 自带工具 | 状态分三类（服务端 / 流式 / UI），各自对齐 useState 与自定义 Hook 就够了 |
| 图标库 | `components/Icons.tsx` | 一共十来个图标，内联 SVG 更可控（统一 16×16、1.6 描边、currentColor） |
| Vitest | `node:test` | 被测对象全是纯函数 |

运行时依赖只有 `react` 与 `react-dom`。

## 四个值得展开讲的设计点

### 1. 为什么手写 SSE 解析

`EventSource` 只支持 GET，而对话端点是 `POST /api/chat/stream`
（消息放请求体里更自然，也不受 URL 长度限制）。所以只能用
`fetch` + `ReadableStream` 自己解帧。真正要处理的细节比想象中多：

- **三种换行符**：后端（sse-starlette）发的是 `\r\n`，只按 `\n\n` 切会一个事件都切不出来；
- **chunk 边界任意**：一个事件可能跨两次 read，缓冲区末尾的孤立 `\r` 必须等到下一个 chunk 才能判定；
- **UTF-8 会被切断**：中文是 3 字节，必须 `TextDecoder.decode(chunk, { stream: true })`；
- **心跳行**：后端 `ping=15` 发的 `: ping` 是注释，不是事件；
- **提前退出要 cancel**：否则连接挂着，后端会把整轮 Agent 白跑完。

这些都写在 `lib/sse.ts` 里，并有逐字节切分的测试覆盖。

### 2. 为什么 token 累加之后还要用 final 覆盖

`token` 是增量，必须累加才能显示。但累加结果 ≠ 最终答案：中间步骤的
token 是模型的**过渡语**（"我先查一下你的简历"），把它们和答案拼在一起
会得到一段语义错乱的文本。而后端的 `final` 事件是内核自己认定的完整答案
（与非流式接口同源）。所以：token 负责过程好看，final 负责结果正确。

### 3. 为什么工具调用要单独可视化

这是 Agent 与传统聊天机器人的**本质差异**：模型自己不执行任何东西，
它只是"申请"调用一个函数，真正的执行发生在进程里，结果再被回灌给模型。
隐藏这个过程，用户就无法判断"它到底是查了数据，还是猜的"。

所以每张工具卡片都要回答四件事：调用什么工具、参数是什么
（`tool_name` / `tool_args`）、成功还是失败（`tool_ok`）、模型看到的
是不是完整内容（`truncated`）。最后一项最容易被忽略 —— 工具输出会被
服务端截断，界面不体现的话，"模型漏答了文件后半部分"这类问题根本无从定位。

### 4. 流式中"当前这句话放哪儿"

流式过程中我们不知道当前这一步最后会不会调工具：调了，这段文本就是过渡语；
没调，它就是最终答案。做法是**先按答案预览渲染，拿到 tool_call 再降级到时间线**。
用户看到的效果是那句话"滑"进思考区、答案区重置为"思考中"，
正好复现了 Agent 的真实决策过程。判据在 `lib/stream.ts#computeTurnView`（纯函数，可单测）。

## 与后端契约有关的四个注意点

1. **字段可能整个缺失**。后端用 `exclude_none=True` 序列化事件，
   所有 `Optional` 字段（`tool_args` / `tool_ok` / `duration_ms` / `truncated` /
   `usage`）在没有值时会从 JSON 里消失，而不是给 `null`。
   读取时一律给默认值，不写 `event.content!`。
2. **错误有两种形态**。`response.ok === false`（会话不存在 → 404、
   请求体不合法 → 422）时还是普通 JSON，必须检查状态码并展示 `detail`；
   流开始之后的异常只能靠内联的 `error` 事件（此时 HTTP 状态码已经发出去了）。
3. **ERROR 事件 ≠ 故障**。步数耗尽、死循环也会发 `error` 事件，
   它们属于"可预期的预算终止"。权威判据是 `done` 事件上的 `stopped_reason`，
   UI 据此用警告色而不是报错色区分。
4. **会话详情的轮次数要自己算**。列表项（`SessionSummary`）有 `turn_count`，
   但详情（`SessionDetail`）没有，只能按 `turns.length / 2` 推。

另外：后端默认用**进程内存**存会话（`session_backend: "memory"`），
后端一重启会话就没了，多进程部署下也不共享。顶栏把这个状态做成了琥珀色徽标 ——
把技术债显式摆在界面上，比藏在文档里诚实。
