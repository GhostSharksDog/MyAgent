# JobPilot · 求职/招聘 AI Agent

> 一个从零手写内核的全栈 Agent 项目：不依赖 LangChain 等框架的"黑盒"，
> 把 **ReAct 循环、Tool Use、Planning、Memory、RAG** 逐层拆开实现，
> 再逐步演进为 **微服务 + 消息队列 + 缓存** 的生产级架构。

## 这个项目解决什么问题

求职者面对的核心痛点是**信息不对称与重复劳动**：

| 痛点 | JobPilot 的解法 | 依赖的技术 |
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
| 前端 | React 19 · TypeScript · Vite | 流式对话 + 工具调用可视化 |
| 存储 | SQLite(dev) → PostgreSQL + pgvector | 会话、简历、向量 |
| 缓存/队列 | Redis · BullMQ 风格任务队列 | 会话缓存、限流、异步长任务 |
| 部署 | Docker Compose | 服务编排与可观测 |
| 大模型 | DeepSeek（OpenAI 兼容协议） | 也支持任意兼容端点 |

## 架构演进路线

本项目刻意采用 **模块化单体 → 微服务** 的演进路径，每一步都有可运行产物：

```
P1 单体                    P3 拆分                    P4 微服务化
┌──────────────┐      ┌──────────────┐        ┌──────────────┐
│  api         │      │  web (React) │        │  web         │
│  ├ agent     │  →   │      ↓       │   →    │      ↓       │
│  ├ tools     │      │  api (网关)   │        │  gateway     │
│  └ llm       │      │  ├ agent     │        │   ├ agent-svc│
└──────────────┘      │  └ rag       │        │   ├ rag-svc  │
                      │  Redis/MQ    │        │   └ worker   │
                      └──────────────┘        │  + MQ/Cache  │
                                              └──────────────┘
```

## 快速开始

```bash
# 1. 激活虚拟环境（已预置）
.venv\Scripts\activate

# 2. 配置模型密钥
copy .env.example .env
# 编辑 .env，填入 DEEPSEEK_API_KEY

# 3. 命令行体验 Agent（含工具调用过程可见）
python services/api/cli.py

# 4. 启动 API 服务
uvicorn app.main:app --reload --app-dir services/api
# 打开 http://127.0.0.1:8000/docs
```

## 当前进度

| 阶段 | 内容 | 状态 |
|---|---|---|
| **P0** | 工程基座：环境、配置、文档、CI | 🔄 进行中 |
| **P1** | Agent 内核：LLM 客户端、Tool Use、ReAct 循环、SSE 流式 | ⏳ |
| **P2** | 记忆与 RAG：短期/长期记忆、向量检索链路 | ⏳ |
| **P3** | 全栈化：React 界面、Redis 会话、异步任务、Planning | ⏳ |
| **P4** | 架构纵深：微服务拆分、可观测、评测回归 | ⏳ |
| **P5** | 求职转化：简历条目、STAR 故事、技术深挖问答 | ⏳ |

详细路线见 [`docs/00-roadmap.md`](docs/00-roadmap.md)。

## 文档地图

- [`docs/00-roadmap.md`](docs/00-roadmap.md) — 阶段路线图与招聘要求对照
- [`docs/01-architecture.md`](docs/01-architecture.md) — 架构设计与关键决策（ADR）
- `docs/02-concepts/` — 原理讲义：大模型、Agent 循环、RAG
- `docs/03-journal/` — 开发日志：每天踩的坑与决策记录

## 为什么值得一看

1. **内核手写而非调框架**：能讲清 `tool_calls` 协议、消息如何拼装、循环何时终止
2. **每个阶段都可运行**：不留半成品债，任何一次提交都能跑起来演示
3. **有评测意识**：不是"感觉效果不错"，而是有测试集与回归指标
4. **架构有演进动机**：每一步拆分都能说清"不拆会疼在哪"
