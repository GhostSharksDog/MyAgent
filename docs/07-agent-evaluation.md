# 通用 Agent 任务评测与指标校正

2026-10-06。本轮完成 NDCG 校正、30 个通用任务和离线评测框架。
没有新增真实模型调用，没有修改用户 `.env`、知识库或工作区配置。
普通请求行为、HTTP/SSE 协议、工具权限及默认值保持不变。

## 1. 先校正评测本身

`rag/evaluate.py::ndcg_at_k` 原先按 top-k 实际命中数计算 IDCG，漏召回也可能得到 1.0。
现在必须显式传完整语料，IDCG 的相关块总数来自全语料；报告标记 `ndcg-corpus-v2`。
只有一个相关块排在第一位、但语料中有两个相关块时，NDCG@2 应为 **0.613147**。

回归覆盖完整排序、只返回部分候选、零／负 k、真实评测入口。
120 个全排列 × k=1/2/5 与 [scikit-learn](https://scikit-learn.org/stable/modules/generated/sklearn.metrics.ndcg_score.html) 对拍；
在内存中恢复旧分母，两项漏召回回归明确失败，源码不被修改。

复测命令：

```powershell
& .venv\Scripts\python.exe -X utf8 scripts/eval_rag.py --compare --sample --json-out data/eval-rag-corrected.json
```

仍为仓库公开 seed、14 查询、section/min_size=120、k=5；不读取私人语料，不调用 LLM 重排。
五条管线 NDCG@5 分别校正为 **0.652、0.690、0.692、0.689、0.689**，Recall 与 MRR 不变。
历史值及完整参数保留在 [可靠性证据](05-reliability-evidence.md)，不同指标版本不能直接做改进对比。

## 2. 任务效果与工程回归分别记录

已有 pytest 验证运行护栏、协议、取消和工具权限。
新增任务评测检验一次任务是否满足数值、字段、依赖或选择约束；同时统计完成率、耗时、工具调用和用量。
评测检查实际结果，不能仅凭模型声称完成判成功，设计参考 [Anthropic 的 Agent 评测说明](https://www.anthropic.com/engineering/demystifying-evals-for-ai-agents)。

任务集位于 `services/api/seed/agent_eval/tasks.json`，合成响应单独位于 `fixtures.json`。
计算 8、提取 8、规划 7、比较 7，共 30 个任务。
文本是公开合成内容，仅在临时目录中写入与读取；模型、记忆、私人 notes 和用户工作区均不加载。
任务要求结构化 JSON 输出，评分不依赖模型裁判，不宣称已衡量开放式文本质量。

运行适配器注入合成 LLM，实际驱动 ReAct、Plan、Supervisor 和已有 calculator/read_file。
配置通过 `AgentSettings.model_construct` 使用声明默认值，文件工具仅注入本轮临时工作区；
token 估算固定为启发式，避免可选 tiktoken 词表下载。适配器用于独立脚本，不能装入 Web 服务。

Plan/Supervisor 外层 SSE 不转发子 Agent 的工具事件。
因此通过工具注册表记录真实执行结果到 `tool_observations`，与原始事件分开保存；
不改变对话事件，也不根据工具摘要猜调用次数。每次重复运行新建 Agent、注册表和预算。

## 3. 可复现入口

在仓库根目录执行，无需启动服务：

```powershell
# 30 任务 × 3 模式，默认就是离线，也可省略 --offline
& .venv\Scripts\python.exe -X utf8 scripts/eval_agent.py --offline

# 指定任务和模式；重复运行用于检查预算隔离，不作为模型稳定性测量
& .venv\Scripts\python.exe -X utf8 scripts/eval_agent.py --offline --task calc-01 --modes plan multi --repetitions 2 --output-dir data/agent-eval-repeat

# 只评分已有记录，不调用 Agent 或模型
& .venv\Scripts\python.exe -X utf8 scripts/eval_agent.py --records data/agent-eval/records.json --output-dir data/agent-eval-regraded

# 对照：计算器实际返回576，但合成答案故意写575；三种模式均应失败，退出1
& .venv\Scripts\python.exe -X utf8 scripts/eval_agent.py --offline --task calc-01 --fixtures services/api/seed/agent_eval/negative-fixtures.json --output-dir data/agent-eval-negative
```

产物：`report.md` 为可读摘要，`report.json` 含分类汇总与逐项评分，`records.json` 保留原始事件、工具观察、耗时及调用数。
报告保存任务集 SHA-256；不同任务版本、未知任务、重复记录或超出选择范围的记录拒绝计分。
部分任务选择明确记在记录中；声明要跑却缺失的轮次计入分母并标记缺失，不能靠删掉失败记录提高成绩。
全部目标通过退出 0，错误目标／缺失记录退出 1，输入错误退出 2。脚本没有 `--live` 执行入口。

GitHub Actions 新增此离线命令，上传报告与原始事件作为 artifact；远端首跑仍需用户推送后确认。

## 4. 已有运行记录如何评分

输入是 `app.evaluation.tasks.RunBundle`，可用其 `model_json_schema()` 查看完整结构。
最外层必须声明 `schema_version=1`、`suite_id`、`suite_sha256`、来源、模型标识、模式、任务选择、重复次数和 `records`。
每条 TrialRecord 包含 task_id、mode、attempt、elapsed_seconds、原始 events、可选 llm_calls 和 tool_observations。

来源支持 `synthetic`、`live`、`recorded`；这是输入记录的来源声明，并非评分器独立验证了模型厂商。
来源和模型写在摘要开头，合成成绩不作为真实模型完成率或成本依据。
真实记录需由另一个经授权的采集过程生成；本轮未实现或运行联网采集，旧的 30 次真实调用额度仍按已用尽处理。

终态必须是末尾且恰好一次的 done，明确 `stopped_reason=finished`；预算、超时、错误、取消即便有正确片段也不算任务成功。
最终答案需为单个 JSON 对象，重复字段、NaN、Infinity、围栏或缺字段不能蒙混通过。
工具目标需要实际成功结果；有注册表观察时使用观察，只有事件时需同时有请求与成功结果。

只有完整 prompt/completion/total 三项用量、相加一致且 usage_complete=true，才标为用量完整。
缺失或异常用量显示未知；已有部分用量只汇总已知下界。没有价格表或人民币费用硬上限。
缺失 llm_calls 显示未知，不推断为零。P50/P95 为本次观测的 nearest-rank 分位数；
合成调用耗时和固定 Usage 不能用于标定生产 timeout、限流或费用。

## 5. 本轮证据与剩余工作

初次全量离线运行：90/90 项执行与评分链路检查通过；ReAct 31 次工具调用，Plan 62 次，Supervisor 62 次。
这些是合成输出的框架验收，不是 100% 模型正确率。合成规划固定两步骤、路由固定两个专家，不能据此推断哪种模式更优。

新增反例验证：错误数值／类型、无效 JSON、依赖颠倒、工期超额、工具失败、越界资料、预算退出、
重复结束事件、未知用量、删掉记录、任务版本漂移与重复运行预算隔离。错误 fixture 与失败子任务工具使三模式都失败。
网络客户端和全局配置读取的对照在测试中设为抛异常，完整 90 轮仍能通过。

最终门禁：**1058 个后端通过、1 live 跳过**（52.45s），**159 个前端通过**；
后端与评测脚本的只读格式、lint、依赖锁、前端类型与构建均通过。
本轮新增 7 个 NDCG 回归与 53 个任务评测回归；公开 RAG 复测和 90 轮合成任务运行均完成。
已有记录重新评分通过，重复运行 4/4 轮通过；公开错误答案 fixture 的 3/3 轮明确失败，退出码为 1。
用户 `.env` 字节摘要与修改前相同；本轮真实模型请求 **0 次**。

尚未采集这 30 任务的真实模型完成率、稳定性、token 成本或延迟分布。
下一步可以授权新的模型请求额度，采集同题多次运行，再用此评分器比较。
随后已完成[通用RAG语料与评测扩充](08-general-rag-benchmark.md)。普通对话自动持久化与按 run ID 检索、写入前差异批准仍属于后续工作。
