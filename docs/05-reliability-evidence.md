# 三种 Agent 编排的可靠性证据（2026-10-06）

本轮面向 AI 应用 / Agent 开发岗位，继续使用通用 `general` profile。
这里记录已执行的验证。排期与未来目标不作为已实现能力或测量结果。

## 修复与反例

| 原问题 | 当前实现 | 会使旧实现失败的验证 |
|---|---|---|
| Plan 子 Agent 丢失 profile、并发与上下文预算 | `settings.model_copy`，只覆盖单步 max_steps | 对比完整设置字典，除 max_steps 外必须一致 |
| 规划、汇总和工具等待未计入整轮超时 | 每轮 RunContext + 单调时钟绝对 deadline，子任务共享 | 分别延迟规划、重规划、路由、执行、工具与汇总；超时后禁止后续调用 |
| Supervisor 取消后专家继续执行 | `finally` 中 cancel + gather，模型/SSE 生成器显式关闭 | 消费任务取消、在 token 后关闭生成器、实际 ASGI disconnect；结束时专家 active=0 |
| Supervisor token 上限未使用，Plan 超额仍汇总 | 模型与工具调用边界检查共享 Usage，跳过后续付费汇总 | 规划/路由用量已达阈值时专家不得启动；有已完成步骤时保留部分结论 |
| 失败专家消耗漏算、缺 Usage 被视为零费用 | 按每个已返回模型 Usage 记账；缺失、中断、重试后的未知消耗标记不完整 | 先返回 Usage 再失败、缺字段、并发在途返回、重复/并发请求隔离 |
| serial 工具只在单个专家内串行 | 同一 ToolRegistry 使用共享 asyncio.Lock | 两种不同副作用工具跨调用 peak=1；只读对照组 peak=2 |
| 中止轮次被写进会话 | HTTP 与 SSE 只保存 finished | Plan/Multi × HTTP/SSE 预算退出后会话 turns 仍为空 |
| 停止按钮等待悬挂的 reader.read | AbortSignal 直接取消 reader 并释放锁 | 悬挂 ReadableStream 回归；Edge 点击停止后恢复输入区 |
| 默认 pytest 发起真实请求 | live marker 默认跳过；`-m live` / `--run-live` 明确选择 | opt-in 组合回归；普通套件不访问模型 |
| `eval_rag.py --sample` 在 general 下得到空库 | 显式装载仓库 seed，关闭 notes 与额外路径 | 私人加载器设置为抛异常，公开样本评测仍可完成 |

主要回归位于 `test_agent_runtime.py`、`test_session_api.py`、`test_settings_api.py`、
`test_eval_entrypoints.py`、`test_verification_entrypoints.py` 与前端 `runtime.test.mjs`。

## 本地门禁与界面

基线为 **939 个后端离线通过 + 1 个 live**，前端 **91**。
最终测试数量以本节验收记录和命令输出为准。

当前验收：**995 后端通过、1 live 跳过；96 前端通过**，类型检查、lint、只读格式检查、
运行时 lock 校验和前端构建通过。后端全量输出耗时 47.35s，不作为性能指标。
环境：Windows / Python 3.12.3 / Node 24.20.0 / Ruff 0.16.7 / pytest 8.4.2；
检索依赖 numpy 2.5.3、scikit-learn 1.9.1，运行时 lock 包含 34 个包。
本机 pnpm 包装器在沙箱受限时，前端使用 package.json 中同一组 node 命令执行。

```powershell
& .venv\Scripts\python.exe -X utf8 -m ruff format services/api --check
& .venv\Scripts\python.exe -X utf8 -m ruff check services/api
& .venv\Scripts\python.exe -X utf8 -m pytest services/api/tests -q
& .venv\Scripts\python.exe -X utf8 scripts/lock_deps.py --check
Set-Location apps/web
pnpm test
pnpm run typecheck
pnpm build
Set-Location ../..
```

Edge 冒烟已通过：真实页面、设置窗口、模式提示、timeout/token_budget 状态、
不完整用量、部分结果与停止按钮。使用 `scripts/smoke_ui.py --reliability`，
聊天响应由浏览器内合成 SSE 提供，**不把这一项当作模型正确率证据**。
内核任务取消另外由真实 ASGI disconnect 和模型调用取消验证。

本轮页面验证使用临时进程配置、回环端口 8097、空语料、写权限关闭；没有保存设置。
既有设置项仍用服务端数据；新增预算字段由 HTTP 回归证明保存、回读和下一轮生效。

CI 配置为 `.github/workflows/ci.yml`：Windows、Python 3.12、Node 24、pnpm 10.12.1，
运行只读格式检查、lint、离线后端测试、公开样本评测、前端测试、类型检查和构建。
**远端首跑尚未验证，需要用户推送后确认**；本地通过不等于远端已绿。

## 公开样本 RAG 复测

命令：`.venv\Scripts\python.exe -X utf8 scripts/eval_rag.py --compare --sample`。
语料为仓库 `services/api/seed` 中的简历与岗位公开样本；标注为
`services/api/seed/eval_set.json`，14 条查询，k=5，section 切分，min_size=120，TF-IDF。
不使用 `data/notes`、私人简历或 `.env` 声明的额外语料。本轮不调用 LLM 重排/改写。

| 管线 | Recall@5 | MRR | NDCG@5 |
|---|---:|---:|---:|
| TF-IDF 向量 | 0.821 | 0.601 | 0.669 |
| BM25 | 0.798 | 0.685 | 0.718 |
| RRF 混合 | 0.869 | 0.657 | 0.720 |
| 向量 + 特征重排 | 0.821 | 0.685 | 0.717 |
| 混合 + 特征重排 | 0.821 | 0.685 | 0.717 |

混合召回提高 Recall，特征重排提高 MRR，但可能丢失 top-k 召回。
14 条查询属于小样本消融，不宣称生产效果；历史 LLM 重排/HyDE 数字见开发日志，未在本轮复测。

## 真实模型验证

当前模型为 `deepseek-chat`。只有脱敏合成问题与临时目录中的 `sample.md` 送往模型：
两个批次为 128 与 64，调用计算器核验 `(128+64)*3`。三种模式正常流程均给出 576。
文件工具只读，未加载私人语料、长期记忆或用户工作区。

每个 HTTP 请求 `max_tokens=512`；重试次数为 0；验证客户端也关闭 JSON 格式 fallback。
请求钩子在传输前限制总次数，达到上限拒绝后续传输。
脚本可通过 `--max-requests` 限制单次执行；多次执行需要把额度扣减后继续。

| 场景 | HTTP 尝试数 | 耗时(s) | 已知 prompt / completion / total | 终态 | 用量完整 |
|---|---:|---:|---|---|---|
| ReAct 正常 | 2 | 3.023 | 3707 / 207 / 3914 | finished | 是 |
| Plan 正常 | 6 | 6.564 | 9310 / 714 / 10024 | finished | 是 |
| Supervisor 正常 | 6 | 5.338 | 8972 / 812 / 9784 | finished | 是 |
| Plan token 阈值=1 | 1 | 1.216 | 169 / 176 / 345 | token_budget | 是 |
| Supervisor token 阈值=1 | 1 | 0.681 | 229 / 69 / 298 | token_budget | 是 |
| ReAct timeout=0.05s | 1 | 0.057 | 未取得完整 Usage | timeout | 否 |
| Plan timeout=0.05s | 1 | 0.071 | 未取得完整 Usage | timeout | 否 |
| Supervisor timeout=0.05s | 1 | 0.048 | 未取得完整 Usage | timeout | 否 |
| Supervisor 路由后关闭流 | 1 | 1.235 | 未保留 Usage | cancelled | 否 |
| Supervisor 专家已启动后取消 | 3 | 1.109 | 229 / 66 / 295，专家用量未知 | cancelled，遗留专家任务=0 | 否 |

可审计报告保存在本地忽略目录 `data/agent-live-verification-final.json`、
`data/agent-live-cancel.json`。本次完整报告 20 次，补充取消验证 3 次，另有沙箱联网失败
1 次，以及验证脚本参数错误前已成功的 ReAct 2 次；**受限脚本累计 26 次尝试**。
另外，修复离线入口前的旧门禁预检曾触发一个 live 用例并联网失败，原客户端重试为 3，
最多产生 4 次尝试。这次预检未采用输出 512 / 重试 0 的约束，也未取得回答或 Usage；
没有逐请求报告，保守占用全部 4 次额度。**本轮预算按最多 30 次占用，不再发起真实调用**。
中断的初次成功流程没有留下完整用量报告，因此不把全批次消耗称为完整统计。
完整报告中三种正常流程合计 23722 token；这不是全批次费用。

两个有报告的验证运行均比较 `.env` 字节摘要，`env_unchanged=true`。
首次脚本参数错误已修复，并用离线测试覆盖九个场景构造、HTTP 次数上限和禁止重试。
脚本默认不联网：必须传 `--live`。例如：

```powershell
& .venv\Scripts\python.exe -X utf8 scripts/verify_agent_modes.py --live --max-requests 30
# 只验证专家取消时可选择一个场景，并扣除此前已使用的额度：
& .venv\Scripts\python.exe -X utf8 scripts/verify_agent_modes.py --live --case multi/cancel --max-requests 3
```

## 边界与尚未验证

- token 上限仅根据已返回的对话模型 Usage 在调用边界执行；在途调用可超额，
  缺用量或重试时消费未知。RAG 自身的可选 LLM 改写/重排未并入本轮账本。
- deadline 控制后续调用和异步等待。同步线程中的写入无法强杀；取消必须等待它完成后
  才释放互斥，因此清理可能超过 deadline。已完成副作用不回滚。
- 互斥只覆盖单进程共享 ToolRegistry；多进程、多个注册表没有分布式互斥。
- 单轮正常耗时不是 P95 标定。生产时长/限流阈值、TLS、Redis 鉴权和部署拓扑仍待环境决定。
- 远端 CI 首跑，以及真实模型在重规划、专家中途失败、上下文裁剪和竞争写入中的表现，
  未做付费验证；这些有离线回归。没有扩展部署或进行人民币费用上限承诺。
