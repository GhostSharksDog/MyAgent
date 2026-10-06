# AGENTS.md —— 给接手的智能体

> 这份文件写给**下一个要动这个仓库的智能体**，不是写给人的项目介绍。
> 人的介绍在 [`README.md`](README.md)，设计决策与踩坑史在
> [`docs/01-architecture.md`](docs/01-architecture.md)（尤其第 8 节）。
>
> 阅读顺序建议：本文件全部 → `docs/01-architecture.md` 第 8 节 → 再动手。

---

## 1. 这是什么，现在到哪了

一个**手写内核**的 AI Agent 项目（不依赖 LangChain）：ReAct / Plan-and-Execute /
Supervisor 三种形态共用同一套工具与护栏层，带 RAG、记忆、会话、异步任务、
可观测性与微服务拆分。

- 后端 `services/api`（Python 3.12 + FastAPI），前端 `apps/web`（React 19 + TS + Vite）
- 场景：**通用助手**（`AGENT_PROFILE=general` 是默认）。求职能力是**可选技能包**
  （`jobhunt`），刻意保留但没有加载 —— 理由在 `README.md` 的当前实现说明、
  `app/agent/prompts.py` 的 `_GENERAL_CAPABILITIES`，以及 `app/core/config.py`
  里 `profile` 字段的注释
- 测试：2026-10-06 **1182 后端通过 + 1 live 跳过、166 前端通过**；最新检索复测见 `docs/10-rag-retrieval.md`，运行记录见 `docs/09-run-history.md`，通用基准见 `docs/08-general-rag-benchmark.md`，任务评测见 `docs/07-agent-evaluation.md`，界面验收见 `docs/06-ui-design.md`
- 编号技术债（T01–T23）**已清空**，见 §10「已知未做」的那三类
- 已有 Windows CI（`.github/workflows/ci.yml`）：Python 3.12、Node 24、pnpm 10；远端首跑待用户推送确认
- 三种编排共享每轮 `RunContext`（`agent/runtime.py`）；规划、路由、子任务、工具和汇总不能重领预算
- Plan/Supervisor 本轮不使用会话历史；HTTP/SSE 只持久化 `finished`。缺 Usage 时 `usage_complete=false`
- Web 已采用暖白／石墨／鼠尾草绿简约界面；首屏与聊天共用一个输入组件，计划／专家／工具统一在「执行过程」展开，终态说明始终显示在答案附近
- `scripts/eval_agent.py` 默认离线：30 个通用公开任务 × 三模式，合成 LLM 驱动真实内核和只读工具。90 轮通过是框架验收，不是模型成功率；可离线重新评分已有 RunBundle
- RAG NDCG 已按全语料相关块校正，报告标记 `ndcg-corpus-v2`；历史 top-k 命中数分母的数字不能与新版本直接比较
- 通用RAG基准：`eval_rag.py --compare --dataset general`，16份虚构文档/60查询，manifest逐文件校验；严格离线、不读配置。48正例与12无答案分开统计，完整证据率和非空返回率不能冒充模型正确率；原始报告在 `docs/evidence/rag-general-v1`
- 检索已索引完整章节名并排除零分补位；同分按语料顺序稳定截断；SPARSE 改写只用 BM25。`--diagnostics` 逐项记录证据阶段，不增加调用，轨迹必须每次独立持有。新旧报告在 `docs/evidence/rag-retrieval-v1`，默认带重排组合分数不变，部分消融退化如实留档

版本与提交状态每次都会变，**自己在仓库里查**：

```powershell
git status -sb                      # 有没有未提交/未推送
git log --oneline -8                # 最近的改动（提交信息写得很长，是有意的）
```

---

## 2. 三十秒自检（接手第一件事）

```powershell
# 1) 工作区干净吗（应该有输出=有改动；无输出=干净）
git status --short

# 2) 后端能不能跑（默认离线，1 个 live skipped；最新通过数见证据文档）
& .venv\Scripts\python.exe -m pytest services\api\tests -o addopts="" -q --no-header

# 3) 前端测试与类型（最新通过数见界面验收 / 无类型错误）
cd apps\web; pnpm test; pnpm run typecheck; cd ..\..
```

三条都过 → 可以从 §9 挑活干。任何一条不过 → 先修它，别在坏的基础上加东西。

---

## 3. 环境事实（**这台机器上必须遵守**，否则会浪费几十分钟）

| 事实 | 后果 / 对策 |
|---|---|
| `pwsh` 工具实际是 **Windows PowerShell 5.1**，不是 PowerShell 7 | 很多新语法不可用；管道里嵌中文前先 `chcp 65001` |
| **绝不用 PowerShell 的 `Get-Content`/`Set-Content` 改 UTF-8 源码** | PS 5.1 按 GBK 读、按 UTF-8 写 → 中文**双重编码**（真的发生过）。改文件用编辑工具，或 Python 显式 `encoding="utf-8", newline="\n"` |
| `scripts/*.ps1` 必须带 **UTF-8 BOM** | 没有 BOM 时 PS 5.1 按 GBK 解析 → 中文乱码甚至语法错误。改完 `.ps1` 跑 `python scripts\fix_ps1_bom.py`；`test_service_split.py::TestPowerShellEncoding` 会盯着 |
| 解释器是 **`.venv\Scripts\python.exe`**（3.12.3） | 不要用系统 python。项目的 pytest 配置是 `addopts = "-ra --strict-markers"`（刻意**不带** `-q`：叠成 `-qq` 会把 "N passed" 汇总行也压掉，让人无法确认到底跑完没有 —— 见 `pyproject.toml` 那段注释）。我习惯再补 `-o addopts=""` 只是为了让输出参数完全由自己掌控，不是必须的 |
| 环境变量 **`PIP_TARGET` 被设成了 Python 3.9 的 site-packages** | 直接 `pip install` 会装错地方（venv 里看不见）。`dev.ps1` 每次执行会临时清掉它；手动装包前先 `Remove-Item Env:PIP_TARGET` |
| 本机开着**系统代理**（`127.0.0.1:7890`），且**环回地址的连接被拒要 ~2.04 秒**才返回 | `httpx`/`requests` 默认 `trust_env=True` 会读 Windows 注册表代理 → 连往环回的内部服务会被塞进代理，报出错误诊断。**内部服务调用一律 `trust_env=False`**（见 `app/rag/backend.py` 的说明）；这也解释了 Redis 探测为什么恰好 2.04 秒 |
| 出网很慢（实测 60–90 kB/s） | 装依赖、`docker build` 都很慢。Dockerfile 里给 pip 挂了 BuildKit 缓存卷，**重试会接着下**，别去掉 |
| Git Bash 里 `.\scripts\dev.ps1` 会被吃成 `.scriptsdev.ps1` | 用 `powershell -File scripts\dev.ps1 <task>` 或 `python scripts\<x>.py` |
| 仓库根下有 `.venv/ .pip-cache/ .pnpm-store/ data/` 等环境目录 | 都已在 `.gitignore` 里，**不要提交** |

---

## 4. 命令速查

### 门禁（提交前必须全绿）

```powershell
powershell -File scripts\dev.ps1 check      # 四段：BOM 自愈 → fmt → lint → 后端测试（任一失败即停）
# 等价的手工三步（更可控）：
& .venv\Scripts\python.exe -m pytest services\api\tests -o addopts="" -q
& .venv\Scripts\python.exe -m ruff check services\api   # 在 services\api 下跑也可以
& .venv\Scripts\python.exe -m ruff format services\api
```

### 前端

```powershell
cd apps\web
pnpm test          # node --test（无 jsdom：只测纯函数，理由见 §8 最后一条）
pnpm run typecheck # tsc --noEmit
pnpm build         # 产出 dist/ —— 后端起服务时会把 dist 当静态站挂上
```

> ⚠ 改了前端**必须 `pnpm build`**：后端的 `mount_frontend` 只在
> `apps/web/dist` 存在时挂载界面，否则接口正常、**界面 404**（而且不报错）。

### 起服务与验证

```powershell
powershell -File scripts\dev.ps1 serve -NoReload   # 起 API
powershell -File scripts\dev.ps1 cli               # 命令行 Agent
powershell -File scripts\dev.ps1 tools             # 看模型实际拿到的工具描述
```

> `-NoReload` 建议加上：`--reload` 监视的是**整个仓库**（uvicorn 启动时会
> 打印它监视的目录），包括 `.venv` 与 `node_modules` —— 启动要遍历一遍、
> Windows 上容易踩文件监视的句柄上限、而且前台运行时一次误触的 Ctrl+C
> 就把它带走（"服务怎么自己退出了"这个疑问就是这么来的）。
> 改后端代码时开着方便；只是把服务跑起来用、或这会儿在改前端（前端有 HMR）时用它更稳。

### 需要真实环境才能跑的验证脚本

| 脚本 | 前提 | 它证明什么 |
|---|---|---|
| `python scripts\smoke_ui.py [--visual / --reliability]` | 界面服务在跑；本机有 Edge | **界面真的渲染**（Edge + CDP）；`--visual` 全部 API/SSE 合成，覆盖三视口、主题、设置、焦点、模式与取消，不读私人配置、不调用模型。默认与 `--reliability` 的非聊天接口仍读取现有服务 |
| `python scripts\verify_models.py` | 服务在跑 | 多模型流程端到端（会临时改 `.env`，**结束时自动还原**） |
| `python scripts\verify_compose.py` | `docker compose up -d` 之后 | 四个容器、三个 backend、界面、鉴权、**跨进程任务**。它会先在容器内探一次 `/healthz`，核对"我打到的到底是不是那个容器" |
| `python scripts\bench_session_store.py [--memory]` | 无 | 会话后端延迟分布。`sqlite_store.py` 里的性能结论就是这个脚本量的 |
| `python scripts\loadtest.py` | 服务在跑 | 并发压测（P50/P95/P99、QPS） |
| `python scripts\eval_rag.py` | 无 | RAG 消融评测（指标是简历上的数字，别随手改语料/参数后不重跑） |
| `python scripts\eval_agent.py --offline` | 无 | 30 个通用任务的合成模型／真实内核链路验收；`--records` 只评分已有记录。没有联网执行入口，不读取用户配置；报告在 `data/agent-eval` |

---

## 5. 代码地图

```
services/api/app/
  main.py            FastAPI 装配（lifespan）+ 中间件顺序 + 挂前端静态站
  api/               HTTP 层：routes / schemas / settings / models / files / auth
  agent/
    loop.py          ReAct 主循环（并发工具、时间预算、上下文预算都在这）
    context.py       上下文裁剪（按 token 预算）+ 工具摘要生成
    planning.py      Plan-and-Execute      multi.py  Supervisor
    memory.py        短期记忆（窗口+摘要）与长期记忆；Turn 模型
    prompts.py       提示词；**按已注册工具裁剪规则行**
    factory.py       Agent 全栈装配（build_agent_stack / mount_agent_stack）
  llm/
    client.py        手写 OpenAI 兼容客户端（流式、tool_calls 分片重组、重试）
    tokens.py        token 估算（tiktoken 可选 + 启发式）与精度计数器
    library.py       模型清单（data/models.json）
  rag/               loaders / chunker / embedder(BM25+TF-IDF) / retriever / store
                     backend.py（本地或远程两种后端）/ service
  session/           store.py(ABC+内存+Redis) sqlite_store.py factory.py models.py
  runs/              history.py（运行摘要白名单投影、有界内存、显式单机 SQLite）
  tasks/             queue / redis_queue / handlers / factory
  tools/             base.py(Tool/ToolRegistry/ToolResult) files.py builtin.py ...
  core/              config.py(唯一配置入口) logging.py telemetry.py resilience.py

apps/web/src/
  App.tsx            组装；lib/（api/stream/sse/types/picker/access/models-*）
  components/        ExecutionProcess / SettingsDialog + settings/{General,Models}Section …
  hooks/             useDialogFocus 模态焦点；聊天、会话、设置、服务刷新防迟到响应覆盖
  styles/            tokens.css 定义变量；**组件样式不许写死颜色**
```

---

## 6. 不许破坏的不变量（每条都有测试盯着）

1. **配置只有一个来源**：所有可调项在 `app/core/config.py`，界面写的就是 `.env`，
   不许在业务代码里散落 `os.getenv`。
2. **默认值必须最无害**：`AGENT_PROFILE=general`、语料为空、LLM 重排/改写/限流/记忆默认关（离线 lexical 重排默认启用）、
   `AGENT_RUN_TIMEOUT=0`、`AGENT_FILE_WRITE_ENABLED=false`。
   *要开一个新能力，先问"没读文档的人会得到什么"。*
3. **能力不存在时就不该出现在菜单上**：没配工作区 → 文件工具一个都不注册；
   没开写权限 → `write_file`/`edit_file` 根本不在工具表里；没注册的工具，
   提示词里引用它的规则行会被 `build_system_prompt` 裁掉。
4. **要么明确报错，要么别做**：危险配置组合拒绝启动（非回环 + 无密钥）、
   越界路径报错并说明允许范围、裁剪与截断都要写进日志/事件。
   **静默降级是最坏的形态**（表现为"用户偶尔丢历史"这类不可复现故障）。
5. **报错要能照着做**：错误信息里给下一步（改哪个配置项、用哪个工具），
   不给裸异常字符串。
6. **工具配对不能被破坏**：`role=tool` 必须紧跟请求它的那条 assistant（`tool_call_id` 配对）。
   上下文裁剪按「一组 assistant + 它的 tool 结果」整组丢 —— 拆散会让 OpenAI 兼容端点
   直接 400，而报错完全看不出是裁剪干的。
7. **有副作用的工具必须 `serial = True`**（写文件、记忆写入等）：
   并发写同一目标的结果不可复现。
8. **前端样式不许写死颜色**：只能在 `styles/tokens.css` 定义变量；
   `apps/web/test/styles.test.mjs` 会拦住硬编码色值。
9. **每条文档里的环境变量都必须真的被读到**（`test_config_coverage.py`），
   **每个 import 的第三方包都必须声明**（同文件，T17）。
10. **改依赖后必须重新生成 lock**：`python scripts\lock_deps.py`。
    Dockerfile 里有一步构建期校验，忘了会在**构建时**失败（而不是运行期 ModuleNotFoundError）。
11. **`.ps1` 必须有 UTF-8 BOM**（见 §3）。
12. **测试要有牙**：断言"某条规则被违反时会红"。本项目多处配了**对照组**
    （例如 `tool_concurrency=1` 的时序对照、compose 的"顺序写反"对照、
    `_app_host_mismatches` 的反例测试）。加断言时顺手问一句"它可能永远为真吗"。

---

## 7. 已经付过学费的坑（症状 → 根因 → 现在怎么防）

| 症状 | 根因 | 现在怎么防 |
|---|---|---|
| 启动像卡住 4 秒，然后"服务自己退出了" | Redis 探测每次 ~2 秒 × 2，期间控制台不动 → 人按了 Ctrl+C | `REDIS_CONNECT_TIMEOUT=0.5`；降级日志改成描述"决定"而不是"故障" |
| 改了模型名/密钥却不生效，要重启 | `LLMClient` 构造时把配置**快照**进自己（连 Authorization 头） | 抽出 `build_agent_stack()`，切换模型时**重建全栈**并关掉旧客户端 |
| 每个模型切换漏一个连接池 | 只换引用没关旧 httpx 客户端 | `mount_agent_stack` 返回旧客户端，调用方 close |
| `.env` 里的密钥被界面回传的**掩码**覆盖 → 之后全 401 而页面显示"已配置" | 只判断了"非空" | 三种"不改动"写法都认 + 测试钉住 |
| 容器 healthy、接口 200、**界面 404** | 前端产物没进镜像，而 `mount_frontend` 允许 dist 缺失 | Dockerfile 加 node 构建阶段 + `WEB_DIST` 显式指定 |
| 镜像里 rag/worker 无限重启，`IndexError: 4` | `PROJECT_ROOT = parents[4]` 把**仓库目录深度**写进了代码 | 按标记推断（两遍规则），并有四种布局的测试 |
| 验证脚本"证明"拓扑全错，其实是打到了别的进程 | 本机 `dev.ps1 serve` 占着 `127.0.0.1:8000`，Docker 发布在 `0.0.0.0:8000`，**更具体的 127.0.0.1 抢答** | `verify_compose.py` 先在**容器内**探一次 `/healthz`，不一致就停下并说清原因 |
| `docker build` 跑到 27 分钟超时，重试又从头开始 | `PIP_NO_CACHE_DIR=1` 让下载**不可续传**（网速 60–90 kB/s） | BuildKit 缓存卷 + `PIP_DEFAULT_TIMEOUT=60` + `--retries 10` |
| `pip install -r requirements.lock` 直接 `UnicodeDecodeError` | pip 找不到 coding 声明时按**系统 locale**（GBK）解码，中文注释炸掉 | lock 第一行是 `# -*- coding: utf-8 -*-`，有测试钉住 |
| 写文件时 CRLF 变成 `\r\r\n` | `newline="\r\n"` 会把字符串里已有的 `\n` 再翻译一次 | `newline=""`（原样写回）；编辑不该改整个文件的换行 |
| RAG 连不上却报"服务可能过载" | 系统代理劫走了环回请求（见 §3） | 内部服务客户端 `trust_env=False` |
| 并发写会话 P95 1051ms | 每次操作自己开事务，多个连接抢 SQLite 文件锁，排队长度=尾延迟 | 进程内 `asyncio.Lock` 把"抢文件锁"换成"公平排队"；`list()` 不再每次清理 |
| 改 SQLite 表结构后第一次读写才炸 `no such column` | `create_all` 只建不改 | 启动时比列名，缺了直接说"删库重建" |

---

## 8. 看起来可疑、其实是刻意的（**别乱改**）

- `SESSION_BACKEND=auto` **不会**自动选 `sql`：`auto` 的语义是"探测环境"，
  悄悄开始写 `data/legacy.db` 会让"启动一下"变成"多了个数据库文件"。
- `AGENT_RUN_TIMEOUT=0`（不限制）与限流默认关闭：**没有标定过的阈值比没有阈值更危险**。
- `jobhunt` 的提示词与工具仍在仓库里：那是可选技能包，不是死代码。
- 上下文的"裁剪"而不是"报错"：超预算时丢最早的对话轮次，并显式告知
  （界面上一行"上下文已裁剪"），而不是让请求硬失败。
- `AGENT_CONTEXT_TOKEN_BUDGET=32000` 是具体值而非 0：与时长预算的代价不对称
  （猜小了只是少放几轮旧对话，而"完全不设上限"迟早撞一次硬失败）。
- 前端只有 `react` + `react-dom` 两个运行时依赖，测试用 `node --test`（无 jsdom）：
  **纯逻辑刻意放在 `lib/*.ts` 里做成可测的纯函数**。Hook 生命周期用隔离的 Hook 宿主执行真实源码；真实组件 DOM 与键盘行为另用 Edge/CDP 验证。
- 主题存储键仍为 `legacy.theme`：已有 light/dark/system 偏好保留；新用户、非法值或存储异常回退 light。同步 HTML 首屏脚本与 Hook，不能只改一处。
- Settings 保存只提交当前 Agent／工作区页，不能连带保存另一页权限。配置刷新和会话请求有代际保护，不能删除这些检查。

---

## 9. 怎么继续干活（推荐流程）

1. **先读**：`docs/01-architecture.md` 第 8 节（债表 + 已修条目，每条都写了"为什么"）。
   要动某块代码前，先 `git log --oneline -20 -- <路径>` 看它的近期改动理由。
2. **挑一件、写下来**：这个项目的提交规范是**一件事一个提交**，
   提交信息写**为什么**而不是"改了什么"。
3. **改前先跑门禁**（§2）建立基线；**改后必须再跑**。
4. **能有真实证据就别只写测试**：
   - 涉及界面 → `scripts\smoke_ui.py`
   - 涉及部署 → `docker compose up -d` + `scripts\verify_compose.py`
   - 涉及性能 → `scripts\bench_session_store.py` / `loadtest.py`
   - 涉及多模型 → `scripts\verify_models.py`
   **先量后改**：本项目多次出现"第一直觉是错的"
   （Redis 那 2 秒不是重试；SQLite 慢的不是 fsync 而是用法）。
5. **改了配置项**：同步 `.env.example`（T16 会检查键可读性）与
   `app/core/config.py` 的注释（写清"为什么这个默认值"）。
6. **改了能力开关**：同步设置界面、`SettingsUpdate`/`SettingsView`、
   以及提示词（否则模型会承诺做不到的事）。
7. **提交**：

   ```powershell
   # 提交信息含引号/中文时，写进文件再 -F —— PowerShell 会吃掉内嵌引号，别硬拼
   git commit -F "$env:TEMP\msg.txt" --no-verify
   ```

   `--no-verify` 是因为 git hook 依赖 msys（沙箱/权限下会失败），
   门禁自己跑（§4）即可。
8. **推送**：本仓库的 HTTPS 凭据在用户手上，**由用户推**；你只需保证
   工作区干净、门禁全绿，并告诉用户"有几个提交待推"。

---

## 10. 已知未做（不是"忘了"，是"要等数据或环境"）

HTTP/SSE 运行摘要已接通，入口见 `docs/09-run-history.md`。默认 `RUN_HISTORY_BACKEND=memory`，最新 200 轮/每轮 256 条结构化事件，不录输入、答案、参数、结果、任务描述和异常原文。
用户显式开启 `sql` 后创建 `RUN_HISTORY_PATH`，存储切换需保存并重启；关闭不删除旧库、不追溯持久化旧内存记录。只支持单机单 API 进程，硬退出残留轮次标为 `interrupted`、Usage 不完整。
事件增补 `run_id`、结束 `record_saved`；取消状态属于记录，不新增 SSE 事件类型。子工具必须从共享 RunContext 观察，不能只数外层事件或把子 Usage 再加一次。

本轮证据入口：`docs/05-reliability-evidence.md`。普通 pytest 默认跳过 live；
只有 `pytest -m live` 或 `--run-live` 才联网。不要为“全绿”消耗真实模型额度。
公开 RAG 复测用 `scripts/eval_rag.py --compare --sample`，不读私人 notes 或额外语料。
通用公开基准用 `--dataset general`，旧14查询与新60查询分数不能直接比较。闸门对照只改本轮评测，不改服务配置。
证据阶段用 `--diagnostics`；candidate_rank 是融合后、重排前的位置。每路 recall_k 不等于并集总宽度。当前14查询的纯RRF Recall=.798、MRR=.702、NDCG=.710；旧 .869/.657/.692 属于修复前基线，不能写成当前值。
通用集默认组合的4条多证据遗漏和2条改述遗漏均为 outside_top_k；多证据仍8/12找齐，无答案仍12/12返回片段。下一步排序实验须保留正例与负例对照，不凭这次零分过滤宣称解决拒答或幻觉。
真实编排验证用 `scripts/verify_agent_modes.py --live`：单次最多 30 请求，输出 512、重试 0，
并关闭 JSON fallback、不写 .env；多次执行要扣减累计额度。本轮受限脚本 26 次，
旧门禁失败预检另保守占用最多 4 次，30 次额度按已用尽处理，不要继续联网。
真实模型的重规划/失败/裁剪/竞争写入尚未付费验证，已有离线回归；远端 CI 首跑待推送。

新增预算：`AGENT_PLAN_MAX_TOTAL_TOKENS=60000`、`AGENT_MULTI_MAX_TOTAL_TOKENS=80000`，
0 表示关闭。在设置界面保存后下一轮生效，在途上下文保持自己的配置。
已在途调用可超额，缺失 Usage 不得当成零成本；可选 RAG 改写/重排用量不属于当前对话账本。
工具共享互斥限于同进程同注册表；线程副作用取消时等待完成，因此清理可能超时，不回滚写入。
界面回归：`smoke_ui.py --reliability`（浏览器内合成 SSE，不会调用模型）。
完整界面验收：`smoke_ui.py --visual --target http://127.0.0.1:8097/ --screenshots-dir data/ui-redesign`；
可仅提供 `apps/web/dist` 静态站，全部 API/SSE 在页面挂载前替换为公开合成数据。
10 张展示截图保存在 `docs/screenshots/ui-redesign/`，原始日志在忽略提交的 `data/ui-redesign/`。
新增源码后端测试仅验证冒烟错误采集与 API 拦截，不把模拟接口当作真实模型证据。

| 事项 | 为什么还没做 |
|---|---|
| `AGENT_RUN_TIMEOUT` 的**标定** | 合理预算 = 自己的 P95 再留一截，而 P95 要有真实模型调用才能量（`loadtest.py` 目前只压 `/healthz` 与 RAG，没覆盖模型链路 —— 那要花钱） |
| 限流阈值 | 同上：没有标定过的阈值只会拒掉正常用户 |
| 部署收尾 | Redis 没有密码、没有 TLS 终结 —— 取决于真实部署环境（内网隔离？前面有网关？） |

另有若干"**文档与实现不一致**"的历史遗留值得顺手核（每发现一处就修掉，
并在 `docs/01-architecture.md` §8.2 记一条）：本项目已经靠这种方式抓到过
`parents[4]`、假开关 `AGENT_FILE_ALLOW_SECRETS`、"读写"文案承诺了不存在的写能力。

---

## 11. 用户的偏好（从对话里总结，别踩）

- 要**通用 Agent**，不要做成求职专用；数据源与权限**必须由用户显式声明**，不要猜。
- 讨厌**假控件**：点了没反应的开关/下拉框，比没有更糟。做不到就明说做不到。
- 要**证据**：说"修好了"之前先跑一遍；数字要有出处（哪个脚本、什么参数）。
- 中文沟通，术语可保留英文；解释要具体到"哪一行、什么后果"。
- 提问往往是**发现了一个真问题**（"为什么不能写文件"直接暴露了一个
  从未实现却被七处文案承诺的能力）——先查事实，别急着解释。
