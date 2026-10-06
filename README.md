# Legacy · 手写内核的通用 AI Agent

Python 3.12 / FastAPI + React 19 / TypeScript 全栈项目，面向 AI 应用与 Agent 开发实践。
手写 OpenAI 兼容客户端、工具协议和编排内核，ReAct、Plan-and-Execute、Supervisor 共用工具与运行护栏。
默认通用助手；求职工具与专家作为可选 `jobhunt` 技能包保留。

知识库和文件工作区必须由用户显式声明。默认空语料、写权限关闭、记忆与限流关闭、整轮超时为 0。
智能体接手先读 [AGENTS.md](AGENTS.md)；测量、参数及限制见 [可靠性证据](docs/05-reliability-evidence.md)。

## 当前实现

| 能力 | 当前实现与边界 |
|---|---|
| 三种编排 | ReAct 多轮；Plan 规划/执行/重规划/汇总；Supervisor 路由/并发专家/汇总 |
| 可靠性 | 每轮共享 deadline、累计已知 Usage、上下文裁剪；取消并等待专家，显式关闭模型/SSE 生成器 |
| 工具 | Pydantic 校验、错误回灌、只读并发；同一注册表的副作用跨专家/请求互斥 |
| 文件 | 显式工作区、越界拒绝、敏感文件保护；write_file/edit_file 须显式开启 |
| RAG | 章节切分、TF-IDF、手写 BM25、RRF、特征/可选 LLM 重排、可选改写、引用与相关性闸门 |
| 会话与记忆 | 窗口/摘要/长期事实；memory、SQLite、Redis。Plan/Supervisor 本轮不使用会话历史 |
| Web | 暖白／石墨简约界面、统一执行过程、预算终态与用量完整性、停止按钮、五类设置、工作区与响应式抽屉 |
| 服务工程 | 内存/Redis 队列、trace id、指标、熔断、限流、已有 api/rag/worker/Redis Compose 拆分 |
| 可复现性 | Windows CI、Python 运行时 lock、pnpm lock、默认离线测试、公开样本评测、Edge 冒烟 |
| 任务评测 | 30 个通用公开任务、三模式统一评分；合成模型驱动真实内核与工具，保存原始事件、目标检查、覆盖率及已知用量；真实模型成绩尚未采集 |
| 通用检索基准 | 16份虚构公开文档、60查询，分别检查召回、多处证据和无答案非空返回；旧14查询基准保留 |

SQLite 已落地；PostgreSQL/pgvector、生产阈值标定、TLS 与 Redis 鉴权属于后续工作。
本轮没有扩展部署；历史路线图中的构想不算作当前实现。

## 模式与护栏

| mode | 适用任务 | 历史 | 默认累计对话模型 token 上限 |
|---|---|---|---:|
| react | 逐步探索、连续追问 | 使用会话或显式 history | 无累计阈值，受 max_steps 限制 |
| plan | 拆步骤的独立任务 | 本轮不使用会话历史 | 60000 |
| multi | 多角度核验的独立任务 | 本轮不使用会话历史 | 80000 |

通用 Supervisor 使用资料分析员、方案分析员、结果核验员；`jobhunt` 才加载求职专家。
执行/专家提示词叠加按实际工具裁剪的公共规则。

每轮创建独立 RunContext，规划、重规划、路由、专家、工具等待及汇总共享单调时钟 deadline。
Plan 子任务复制完整配置，只覆盖单步步数；重复运行与独立请求各自拥有新预算。

```dotenv
AGENT_RUN_TIMEOUT=0
AGENT_PLAN_MAX_TOTAL_TOKENS=60000
AGENT_MULTI_MAX_TOTAL_TOKENS=80000
```

0 表示不限制。两个 token 阈值可在「设置 → Agent」中保存，对下一轮请求生效。
阈值根据已返回的 Usage 在调用边界检查；在途调用可能超额，不是人民币费用硬上限。
互斥限于单进程共享 ToolRegistry；已完成写入不回滚。
同步线程中的副作用取消时要等待完成，清理可能超过 deadline。

## 快速开始（Windows）

```powershell
powershell -File scripts/dev.ps1 setup
# 仅首次创建 .env；已有配置时保留原文件，再自行填写模型密钥
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
Set-Location apps/web
pnpm install --frozen-lockfile
pnpm build
Set-Location ../..
powershell -File scripts/dev.ps1 serve -NoReload
```

访问 `http://127.0.0.1:8000/`；接口文档在 `/docs`。
CLI：`powershell -File scripts/dev.ps1 cli`。没有 dist 时不会挂载界面，所以先构建。
本机用 `.venv/Scripts/python.exe`；手动安装前清除错误的 `PIP_TARGET`，见 AGENTS。
Docker：`docker compose up -d --build` 后运行 `scripts/verify_compose.py`；本轮未重新部署验证。

## 界面与演示

首屏展示通用示例，点击只填写草稿；发送后同一个输入区移到聊天底部。
「执行过程」统一查看计划、专家与工具，预算、错误、停止和裁剪提示始终显示在答案旁。
点击顶栏模型名进入模型设置；工作区必须显式选择，写权限与敏感文件权限分别保存。
新用户默认浅色，已有 light／dark／system 偏好保留；手机会话与文件抽屉互斥。

![Legacy 浅色首屏](docs/screenshots/ui-redesign/01-home-light-1440.png)

设计、截图与合成接口验收见 [界面记录](docs/06-ui-design.md)。
`scripts/smoke_ui.py --visual` 使用合成 API/SSE 检查真实 Edge 渲染，不调用模型或修改 `.env`。

## API 与结束契约

端点仍为 `POST /api/chat`、`POST /api/chat/stream`，现有请求字段与事件类型兼容。
请求示例：`{"message":"根据已提供的资料比较两个方案并核验计算","mode":"multi"}`。

ReAct 可传 session_id/history；Plan/Supervisor 即使传入也按独立任务执行，不将历史送给模型。
所需背景应放在本轮 message；成功答案仍可保存为会话记录。

- 正常连接每轮恰好一个 done；断开的连接不保证收到结束事件。
- stopped_reason 支持 finished、max_steps、loop_detected、error、timeout、token_budget。
- 预算退出保留已有结论及原因，不标记成功，不保存到成功会话历史。
- 响应与 done 增加 usage_complete。缺 Usage、模型中断或重试前消耗未知时为 false；
  usage 此时只表示已知下界，不能据 0 声称免费。规划与失败专家已返回的 Usage 同样入账。
- 响应/done 汇总工具摘要、上下文裁剪信息和估算规模。

## 验证与实测

```powershell
& .venv\Scripts\python.exe -X utf8 -m ruff format services/api --check
& .venv\Scripts\python.exe -X utf8 -m ruff check services/api
& .venv\Scripts\python.exe -X utf8 -m pytest services/api/tests -q
& .venv\Scripts\python.exe -X utf8 scripts/lock_deps.py --check
& .venv\Scripts\python.exe -X utf8 scripts/eval_rag.py --compare --sample
& .venv\Scripts\python.exe -X utf8 scripts/eval_rag.py --compare --dataset general --json-out data/rag-general.json
& .venv\Scripts\python.exe -X utf8 scripts/eval_agent.py --offline
Set-Location apps/web
pnpm test
pnpm run typecheck
pnpm build
Set-Location ../..
```

普通 pytest 默认跳过真实模型；显式入口：
`.venv\Scripts\python.exe -X utf8 -m pytest services/api/tests -m live`。
受限真实验证用 `scripts/verify_agent_modes.py --live`：最多 30 次 HTTP 尝试、
每次输出最多 512 token、无自动重试、不改 `.env`。多次运行须扣除先前已用额度。

2026-10-06：当前 DeepSeek 上三种正常流程都完成公开样本文件读取与计算核验；
本地门禁为 **995 后端通过 + 1 live 跳过、96 前端通过**，类型、lint、只读格式、lock 与构建通过。
token 阈值、整轮超时和专家启动后的取消已验证，受限脚本共 **26 次尝试**；
另将旧门禁失败预检最多 4 次保守计入额度，本轮按 30 次封顶，详见证据记录。
Edge 冒烟已验证模式提示、预算状态、统计不完整与停止按钮；
`scripts/smoke_ui.py --reliability` 使用合成 SSE，不消耗模型额度。

同日界面重设计的最终门禁：**998 后端通过 + 1 live 跳过、159 前端通过**；
三种尺寸共 10 张 Edge 截图已复核，完整合成 API/SSE 冒烟全部通过。
本轮新增真实模型请求 0 次，用户 `.env` 未修改；详情见 [界面验收](docs/06-ui-design.md)。

公开 RAG 复测：14 条查询，section/min_size=120，k=5。
TF-IDF Recall@5 **0.821**，混合 RRF **0.869**，特征重排 MRR **0.685**。
全部参数见 [证据记录](docs/05-reliability-evidence.md)。小样本结果不推断生产效果；
历史 LLM 重排/HyDE 数字见 [检索日志](docs/03-journal/2026-09-13-P2-检索基线评测.md)，本轮未复测。

同日评测补强：NDCG 改为按完整语料归一化，报告标记 `ndcg-corpus-v2`；RRF NDCG@5 校正为 0.692，Recall/MRR 不变。
30 个通用任务覆盖计算、提取、规划、比较；默认合成运行 **90/90 轮执行与评分链路检查通过**，不作为模型成功率。
`eval_agent.py` 输出 `data/agent-eval/report.md`、`report.json` 与 `records.json`；也可 `--records <已有记录>` 离线重新评分。
不读取 `.env` 或私人语料，无联网执行入口；真实模型成绩待新额度。用法、记录格式和限制见 [任务评测记录](docs/07-agent-evaluation.md)。
评测补强后的最终门禁：**1058 后端通过 + 1 live 跳过、159 前端通过**，格式、lint、lock、类型与构建通过。

随后增加[通用 RAG 基准](docs/08-general-rag-benchmark.md)：16份虚构文档、60查询，默认参数生成48块。
混合+重排在48条有答案查询上的Recall@5为0.917、完整证据率0.875，多处证据只找齐8/12；
12条无答案查询都返回了片段。提高门槛明显损伤正例召回，因此没有修改服务默认配置。
这些是本地检索指标，不是模型答案正确率或拒答率；与旧基准不能直接比较。
最新门禁：**1103 后端通过 + 1 live 跳过、159 前端通过**；参数、失败与原始JSON已留档。

GitHub Actions 使用 Windows、Python 3.12、Node 24、pnpm 10，完成离线检查与前端构建。
远端 CI 首跑需用户推送后确认；当前只报告本地检查。

## 文档与求职材料

- [可靠性证据与未验证项](docs/05-reliability-evidence.md)
- [界面设计、验收与演示截图](docs/06-ui-design.md)
- [通用任务评测、运行记录与指标校正](docs/07-agent-evaluation.md)
- [通用 RAG 基准与失败分析](docs/08-general-rag-benchmark.md)
- [架构、设计决策与历史修复](docs/01-architecture.md)（第 8/10 节为当前补充）
- [交接说明](AGENTS.md)
- [项目一页纸](docs/04-career/01-onepager.md)
- [简历条目与数字出处](docs/04-career/02-resume-bullets.md)
- [演示脚本](docs/04-career/05-demo-script.md)
- [历史路线图](docs/00-roadmap.md)、[原理讲义](docs/02-concepts/)、[开发日志](docs/03-journal/)
