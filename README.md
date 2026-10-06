# Legacy · 求职/招聘 AI Agent

> 一个从零手写内核的全栈 Agent 项目：不依赖 LangChain 等框架的"黑盒"，
> 把 **ReAct 循环、Tool Use、Planning、Memory、RAG** 逐层拆开实现，
> 再逐步演进为 **微服务 + 消息队列 + 缓存** 的生产级架构。

> **接手这个仓库的 AI 智能体请先读 [`AGENTS.md`](AGENTS.md)** ——
> 那里写的是环境事实、命令速查、不许破坏的不变量、以及已经付过学费的坑。
> 本文件是给人的项目介绍。

## 这个项目解决什么问题

求职者面对的核心痛点是**信息不对称与重复劳动**：

| 痛点 | Legacy 的解法 | 依赖的技术 |
|---|---|---|
| 不知道自己简历和 JD 差在哪 | 简历 × JD 结构化匹配打分，给出差距清单 | LLM 结构化输出 + RAG |
| 投递靠海投，无针对性 | 按 JD 自动改写简历要点、生成求职信 | Agent Tool Use + Planning |
| 面试准备无反馈 | 基于简历与 JD 的模拟面试官，多轮追问 + 复盘 | 多轮记忆 + 角色化 Agent |
| 岗位信息分散、检索靠肉眼 | 岗位知识库语义检索 | 向量检索 + 缓存 |

## 技术栈

| 层 | 选型 | 说明 |
|---|---|---|
| 后端 | Python 3.12 · FastAPI · Pydantic v2 | 异步优先，全链路类型安全 |
| Agent 内核 | **手写**（httpx + 自研循环） | 见 `docs/02-concepts/`，理解原理而非调 API |
| 检索 | 手写 BM25 + RRF 融合 + 两级重排 | 零依赖，全链路可评测 |
| 记忆 | 短期窗口+LLM 摘要压缩 · 长期事实向量召回 | 默认关闭，价值需被度量 |
| 前端 | React 19 · TypeScript · Vite | 流式对话 + 工具调用可视化 |
| 存储 | SQLite(dev) → PostgreSQL + pgvector | 会话、简历、向量 |
| 缓存/队列 | Redis · 任务队列 | 会话缓存、限流、异步长任务 |
| 部署 | Docker Compose | 服务编排与可观测 |
| 大模型 | DeepSeek（OpenAI 兼容协议） | 也支持任意兼容端点 |

## 架构演进路线

本项目刻意采用 **模块化单体 → 微服务** 的演进路径，每一步都有可运行产物。

```
P0/P1 单体                    P2/P3 加外部状态与前端          P4 拆进程（已实现）
┌──────────────┐          ┌──────────────────────┐      ┌────────────────────────┐
│  api         │          │  web (React 19)      │      │  web (React 19)        │
│  ├ agent     │          │      ↓ SSE           │      │      ↓ SSE             │
│  ├ tools     │   →      │  api                 │  →   │  api ──→ rag   (独立)   │
│  └ llm       │          │  ├ agent             │      │   │                    │
└──────────────┘          │  ├ rag ──→ 构建索引   │      │   ├──→ worker (独立)    │
                          │  └ session/tasks     │      │   │                    │
                          │  Redis（可选）        │      │   └──→ Redis（共享状态）│
                          └──────────────────────┘      └────────────────────────┘
```

**实际拆了三个进程角色（`api` / `rag` / `worker`），共用同一个镜像。**
计划里的 `gateway` 与 MQ **刻意没做** —— 没有鉴权/限流多租户需求时，网关只做转发，
多一跳网络换来零个能力；而 outbox 解决的是"跨服务双写"的一致性问题，
当前"入库"与"建索引"共用同一份数据卷，根本不存在双写。
**为不存在的问题加复杂度是最常见的过度设计。**

`RAG_SERVICE_URL` 为空即单体（默认），非空即拆分 —— **能用一个变量表达的配置，
就不要用两个变量加一条校验规则**。完整取舍见 `docs/01-architecture.md` 的 6.3 节。

## 求职转化材料

`docs/04-career/` 是把这个项目**翻译成面试官能秒懂的语言**的产出：

| 文件 | 用途 |
|---|---|
| `01-onepager.md` | 项目一页纸（目标是让没参与过的人 5 分钟读懂） |
| `02-resume-bullets.md` | 简历条目 + **数字溯源表**（每个数字都能当场跑命令复现） |
| `03-star-stories.md` | 6 个 STAR 故事（踩坑 / 取舍 / 性能 / 质量各覆盖） |
| `04-technical-qa.md` | 31 题技术深挖问答，每题标注「实践过 / 只读过 / 空白」 |
| `05-demo-script.md` | 3 分钟演示脚本，**含断网兜底** |

**离线演示**（`.\scripts\dev.ps1 demo`）不是前端 mock：
它走完全相同的 SSE 端点、序列化与分帧，只有数据源不同。
`scripts/verify_demo.py` 会把 LLM 指向不可达端口来证明这一点。

## 快速开始

> **Windows 用户注意**：本机环境的三个坑已固化进 `scripts/dev.ps1`（自动处理
> `PIP_TARGET` 导致的 venv 失效、pip 缓存被拒、控制台 GBK 编码），建议全程用它。

```powershell
# 1. 一键建环境（复用 Anaconda 的 Python 3.12，建 .venv 并装依赖）
.\scripts\dev.ps1 setup

# 2. 配置模型密钥（写入 .env，.env 已被 gitignore）
$env:LLM_API_KEY = 'sk-你的密钥'
python scripts\bootstrap_env.py --force

# 3. 可选：把你的简历 PDF 解析进项目（也可跳过，会用内置示例简历）
python scripts\ingest.py "D:\path\你的简历.pdf" --type resume

# 4. 命令行体验 Agent（含工具调用过程可见）
.\scripts\dev.ps1 cli

# 5. 启动 API 服务，打开 http://127.0.0.1:8000/docs
.\scripts\dev.ps1 serve

# 6. 查看模型实际看到的工具描述
.\scripts\dev.ps1 tools
```

### Docker 一键部署（api / rag / worker / redis 四进程）

```powershell
# 1. 生成访问密钥并写进 .env（对外暴露必须有密钥，否则会拒绝启动）
python -c "import secrets;print(secrets.token_urlsafe(32))"
#    .env 里加两行：LLM_API_KEY=... 与 SECURITY_API_KEY=<上面生成的>

# 2. 起整套拓扑（前端产物在构建期打进镜像，所以只有 8000 一个入口）
docker compose up -d --build

# 3. 验证拓扑真的对（见下）
python scripts\verify_compose.py

# 4. 收工
docker compose down
```

`scripts/verify_compose.py` 检查的不是"容器起来了"，而是四件**配错了也不会报错**的事：

| 检查 | 配错的后果（都不会报错） |
|---|---|
| `/healthz` 的 `session_backend=redis` | 静默退回内存 → 多副本下"用户偶尔丢历史" |
| `task_backend=redis` 且 `task_workers_in_api=false` | 任务永远 pending，而所有容器都 healthy |
| `rag_backend=remote` | 静默退回单体，拆分带来的资源隔离全部失效 |
| 界面真的能打开 + 无密钥 401 / 带密钥 200 | 前端产物没进镜像 → 接口 200、**界面 404** |

最后还会投递一个 `reindex` 任务并等它完成 —— api 里没有 worker，所以它能跑完就是
**跨进程**的直接证据。这是拆分部署唯一无法用单进程测试证明的部分。

提交前跑一遍门禁（格式化 + 静态检查 + 测试）：

```powershell
.\scripts\dev.ps1 check
```

## 检索质量评测

检索管线是**两段式**的：宽召回（向量 + BM25）→ RRF 融合 → 精排 → top-k。

```powershell
python scripts\eval_rag.py --inspect                     # 看语料实际切块结构
python scripts\eval_rag.py --validate                    # 校验评测标注
python scripts\eval_rag.py --compare                     # 跑完整消融阶梯出对比表
python scripts\eval_rag.py --compare --with-llm          # 含 LLM 重排（真实调用 API）
```

### 消融实验（同一评测集、同一语料、k=5）

| 管线 | Recall@5 | Δ | MRR | Δ | NDCG@5 | 命中率 |
|---|---|---|---|---|---|---|
| ① 纯向量（基线） | 0.644 | — | 0.519 | — | 0.566 | 0.733 |
| ② 纯 BM25 | 0.589 | −0.056 | 0.532 | +0.013 | 0.573 | 0.733 |
| ③ 混合 RRF（等权） | 0.578 | −0.067 | 0.519 | +0.000 | 0.540 | 0.667 |
| ④ 向量 + 特征重排 | 0.689 | +0.044 | 0.572 | +0.053 | 0.621 | 0.800 |
| ⑤ 混合 + 特征重排 | 0.689 | +0.044 | 0.572 | +0.053 | 0.621 | 0.800 |
| **⑥ 混合 + LLM 重排** | **0.878** | **+0.233** | **0.933** | **+0.414** | **0.917** | **0.933** |

### 三条反直觉的结论

1. **等权混合检索比纯向量更差**（③ 的 Recall 掉 0.067）。RRF 会把"在某一路排第一"
   让位给"在两路都还凑合"的文档；当一路明显更弱时，等权融合等于引入噪声。
   混合检索有效的前提是**两路质量相当** —— 这点很少被强调。
2. **混合检索的价值取决于召回是否受限**。本项目语料只有 15 块而 recall_k=20，
   两路都返回全量，召回阶段被绕过，融合自然无差异。把 recall_k 压到 4
   让召回真的需要选择后，混合检索胜出（0.611 → 0.644），而 BM25 单独用最差（0.467）。
   **生产环境 recall_k 远小于语料规模，恰好落在混合检索有效的区间。**
3. **真正带来收益的是重排，不是混合**（⑤ 与 ④ 完全相同）。
   LLM listwise 重排修好的正是词法方法修不好的**纯语义错配**
   （"我在哪家公司实习过"这类关于简历的查询把岗位块排在前面）。
   代价：2361 tokens/查询。

完整分析见 [`docs/03-journal/2026-09-13-P2-检索基线评测.md`](docs/03-journal/2026-09-13-P2-检索基线评测.md)。

## 前端与全栈

```powershell
# 终端 1：后端
.\scripts\dev.ps1 serve

# 终端 2：前端（Vite 已配好 /api 与 /healthz 代理到 8000）
cd apps\web
pnpm install
pnpm dev          # 打开 http://localhost:5173
```

前端**只依赖 react + react-dom** —— SSE 解帧器、事件归约器、Markdown 解析器
与整套 CSS token 设计系统（暗/亮双主题）都是手写的。理由见
[`apps/web/README.md`](apps/web/README.md)。

前端与后端的契约一致性由脚本自动验证（这是唯一能发现"两边各自都对、
合起来不对"的手段）：

```powershell
cd apps\web
node scripts\verify-backend.mjs      # 复用前端真实解析器消费后端真实响应
```

## Agent 的四种运行形态

| 形态 | 入口 | 适合 |
|---|---|---|
| **ReAct**（默认） | `Agent` | 探索型任务：不知道下一步会看到什么 |
| **Plan-and-Execute** | `PlanAndExecuteAgent` | 结构型任务：步骤事先大致可预知 |
| **多 Agent（主管-工人）** | `SupervisorAgent` | 任务能按专长切分且各专家输出互不依赖 |
| 记忆增强 | `Agent(memory=…, long_term=…)` | 需要跨轮次/跨会话保持上下文 |

选择依据是**任务形态**，不是哪个听起来更高级。具体的取舍分析见
[`docs/01-architecture.md`](docs/01-architecture.md) 与各模块的顶部注释。

## 异步任务队列

耗时的 CPU 密集操作（重建检索索引实测 **1894ms**）走队列，不阻塞请求路径：

```
reindex 任务耗时   1894 ms
同期 10 次 /healthz  平均 1.6ms，最大 4.5ms
```

若在事件循环里跑，那 10 次健康检查会全部变成约 1.9 秒 ——
**近 2 秒的耗时让"阻塞与否"的差异变得极其明显**。

```powershell
curl -X POST http://127.0.0.1:8000/api/tasks -H "Content-Type: application/json" -d '{\"type\":\"reindex\"}'
curl http://127.0.0.1:8000/api/tasks/<task_id>
```

## 当前进度

| 阶段 | 内容 | 状态 |
|---|---|---|
| **P0** | 工程基座：环境、配置、文档、lint/test 工具链 | ✅ 完成 |
| **P1** | Agent 内核：手写 LLM 客户端、Tool Use、ReAct 循环、SSE 流式 | ✅ 完成 |
| **P2** | RAG + 记忆：解析/切分/两段式检索/评测消融；检索接入 Agent 工具；短期窗口+摘要、长期事实记忆 | ✅ 完成 |
| **P3** | 全栈化：服务端会话层（内存/Redis）、异步任务队列、React 前端、Planning、多 Agent | ✅ 完成 |
| **P4** | 架构纵深：微服务拆分（api / rag / worker + Redis）、熔断与限流、trace id 与指标、压测与检索回归 | ✅ 完成 |
| **P5** | 求职转化：简历条目、STAR 故事、技术深挖问答；RAG 消融 + Query 改写（HyDE 的 Recall@5 0.869 → 0.964） | ✅ 完成 |
| **P6** | 通用化：`general` / `jobhunt` 双形态（默认不加载求职能力）、文件工作区与右侧文件栏、设置界面、「打开文件夹」由**宿主进程**弹系统对话框 | ✅ 完成 |
| **P7** | 可用性收尾：设置改成独立圆角窗口（左栏分类）、**多供应商模型管理**（可存多个、一键切换且立即生效）、`docker compose` 部署真的跑通 | ✅ 完成 |
| **P8** | 可靠性与可复现：`SESSION_BACKEND=sql`（SQLite 持久化，顺带修掉并发追加丢轮次）、后端依赖锁定（别人能复现同一份指标） | ✅ 完成 |
| **P9** | 上下文预算（token 估算 + 裁剪 + 精度可观测）与工具摘要；**文件写入能力**（`write_file` / `edit_file`，默认关闭、显式开启） | ✅ 完成 |

**940 个后端测试 + 91 个前端测试**，全部通过（`.\scripts\dev.ps1 check`）。

每个阶段的取舍与代价都写在 [`docs/01-architecture.md`](docs/01-architecture.md)：
包括**已知技术债清单**（哪些还没做、为什么还没做、影响是什么），
以及两条实测数据 —— 检索消融（基线 hybrid Recall@5 0.869 / MRR 0.657）
与单机压测（RAG `/context` 109.9 QPS、P95 80.9ms；重建索引期间 P95 涨到 1.5×）。

详细路线见 [`docs/00-roadmap.md`](docs/00-roadmap.md)。

## 文档地图

- [`docs/00-roadmap.md`](docs/00-roadmap.md) — 阶段路线图、招聘要求对照、面试自检清单
- [`docs/01-architecture.md`](docs/01-architecture.md) — 架构设计、12 条 ADR、技术债清单
- `docs/02-concepts/` — 原理讲义：
  - `01-llm-basics.md` — 大模型原理与推理部署（KV Cache 显存手算、vLLM/Ollama 选型）
  - `02-agent-loop.md` — Agent 原理与 ReAct 循环实现（Tool Use 协议、流式分片、护栏）
  - `03-rag.md` — RAG 全链路（切分、embedding、索引、混合检索、评测）
- `docs/03-journal/` — 开发日志：
  - `2026-09-13-P0P1-搭建记录.md` — 6 个真实踩坑与排查过程
  - `2026-09-13-P2-检索基线评测.md` — 检索指标、消融实验与瓶颈诊断
  - `2026-09-13-P2-记忆模块.md` — 记忆设计取舍与两个真实缺陷

## 为什么值得一看

1. **内核手写而非调框架**：能讲清 `tool_calls` 协议、消息如何拼装、循环何时终止
2. **每个阶段都可运行**：不留半成品债，任何一次提交都能跑起来演示
3. **有评测意识**：不是"感觉效果不错"，而是有测试集与回归指标
4. **架构有演进动机**：每一步拆分都能说清"不拆会疼在哪"
