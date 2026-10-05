# Legacy 开发任务脚本（Windows / PowerShell）
#
# 【为什么需要它】
# 本机有一组会互相干扰的环境设置，手工敲命令很容易踩坑：
#   1. 用户级 PIP_TARGET 指向 Python 3.9，会让所有 pip 调用无视虚拟环境
#   2. pip 默认缓存目录被文件沙箱拒绝，导致下载卡死（CPU 空转）
#   3. 中文控制台默认 GBK，Python 打印中文会 UnicodeEncodeError
# 本脚本在每次执行前统一处理这三件事，保证命令可重复、结果可预期。
#
# 用法：
#   .\scripts\dev.ps1 setup      # 首次：建 venv + 装依赖 + 生成 .env
#   .\scripts\dev.ps1 test       # 跑测试
#   .\scripts\dev.ps1 test-live  # 含真实 API 调用的测试（会计费）
#   .\scripts\dev.ps1 lint       # 代码检查
#   .\scripts\dev.ps1 fmt        # 代码格式化
#   .\scripts\dev.ps1 check      # fmt + lint + test 一条龙（提交前跑）
#   .\scripts\dev.ps1 cli        # 启动命令行 Agent
#   .\scripts\dev.ps1 serve      # 启动 API 服务（含 /docs 交互文档）
#   .\scripts\dev.ps1 tools      # 列出已注册的工具
#
#   # ---- 微服务拆分（P4）----
#   .\scripts\dev.ps1 rag        # 启动独立的 RAG 检索服务（端口 8001）
#   .\scripts\dev.ps1 worker     # 启动独立的任务 worker（需要 TASK_BACKEND=redis）
#   .\scripts\dev.ps1 serve-split # 启动 API 服务并指向独立 RAG 服务（拆分拓扑）
#   .\scripts\dev.ps1 verify-split # 跨进程验证：证明调用真的走了 HTTP
#   .\scripts\dev.ps1 loadtest   # 并发压测，给出 P50/P95/P99 与 QPS
#
#   # ---- 求职转化（P5）：演示 ----
#   .\scripts\dev.ps1 record-demo  # 录一次真实对话的事件流（含 PII 闸门）
#   .\scripts\dev.ps1 demo        # **离线演示模式**：重放录制的事件流，不调模型
#   .\scripts\dev.ps1 verify-demo # 验证离线回放真的不需要网络
#
#   # ---- 发布 -  -
#   .\scripts\dev.ps1 scan-pii  # 全历史 PII 扫描（推送前必跑）
#   .\scripts\dev.ps1 push      # 扫描 + 推送到 GitHub

[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    # ⚠ 新增子命令时**必须同时加到这里**：ValidateSet 是参数绑定阶段执行的，
    # 不在列表里的值会在进入 switch 之前就被拒绝。
    # 曾真实踩过：switch 里加了 rag/worker，却忘了加 ValidateSet，
    # 结果 `dev.ps1 rag` 直接报"参数不在集合中"。
    [ValidateSet(
        'setup', 'install', 'test', 'test-live', 'lint', 'fmt', 'check', 'cli', 'serve', 'tools',
        'rag', 'worker', 'serve-split', 'verify-split', 'loadtest',
        'record-demo', 'demo', 'verify-demo', 'install-hooks', 'scan-pii', 'push', 'help'
    )]
    [string]$Task = 'help',

    # 传给具体任务的额外参数，例如： .\scripts\dev.ps1 test -Extra "-k calculator"
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$Extra
)

$ErrorActionPreference = 'Stop'

# ---------- 项目根目录（脚本在 scripts/ 下） ----------
$Root = Split-Path -Parent $PSScriptRoot
$VenvPython = Join-Path $Root '.venv\Scripts\python.exe'
$ApiDir = Join-Path $Root 'services\api'

# ============================================================
# 环境修复：每次执行前统一处理本机的三个坑
# ============================================================
function Initialize-Environment {
    # 坑 1：PIP_TARGET 会把包装到 Python 3.9，让 venv 失效。
    # 只清理当前进程的环境变量，不改动用户的全局配置。
    if ($env:PIP_TARGET) {
        Write-Host "[env] 已临时清除 PIP_TARGET（原值: $env:PIP_TARGET）" -ForegroundColor DarkYellow
        Remove-Item Env:PIP_TARGET -ErrorAction SilentlyContinue
    }

    # 坑 2：pip 默认缓存在工作区外，写入被拒会导致下载卡死。重定向到项目内。
    $env:PIP_CACHE_DIR = Join-Path $Root '.pip-cache'

    # 坑 3：中文控制台的默认编码是 GBK，Python 输出中文会崩。强制 UTF-8。
    $env:PYTHONUTF8 = '1'
    $env:PYTHONIOENCODING = 'utf-8'
}

function Assert-Venv {
    if (-not (Test-Path $VenvPython)) {
        Write-Host "[x] 找不到虚拟环境：$VenvPython" -ForegroundColor Red
        Write-Host "    请先运行： .\scripts\dev.ps1 setup" -ForegroundColor Yellow
        exit 1
    }
}

function Invoke-Pip {
    param([string[]]$Packages)
    & $VenvPython -m pip install --no-input --progress-bar off @Packages
    if ($LASTEXITCODE -ne 0) { throw "pip install 失败" }
}

# ============================================================
# 任务实现
# ============================================================
function Task-Setup {
    Initialize-Environment

    if (-not (Test-Path $VenvPython)) {
        Write-Host "[1/3] 创建虚拟环境 .venv ..." -ForegroundColor Cyan
        # 用 Anaconda 自带的 Python 3.12（系统 PATH 上的 3.9 已 EOL，不要用）
        $base = 'E:\anaconda3\python.exe'
        if (-not (Test-Path $base)) {
            # 退而求其次：从 PATH 上找一个 >= 3.11 的解释器
            $base = (Get-Command python -ErrorAction SilentlyContinue).Source
            if (-not $base) { throw "找不到可用的 Python 解释器，请先安装 Python 3.12" }
        }
        Write-Host "      使用解释器：$base"
        & $base -m venv (Join-Path $Root '.venv')
        if ($LASTEXITCODE -ne 0) { throw "创建 venv 失败" }
    } else {
        Write-Host "[1/3] 虚拟环境已存在，跳过创建" -ForegroundColor DarkGray
    }

    Write-Host "[2/3] 安装依赖 ..." -ForegroundColor Cyan
    Invoke-Pip @(
        'fastapi>=0.115,<1', 'uvicorn[standard]>=0.32,<1', 'pydantic>=2.9,<3',
        'pydantic-settings>=2.6,<3', 'sse-starlette>=2.1,<3', 'python-dotenv>=1.0,<2',
        'httpx>=0.27,<1', 'pytest>=8.3,<9', 'pytest-asyncio>=0.24,<2',
        'ruff>=0.8,<1', 'mypy>=1.13,<2', 'sqlalchemy>=2.0,<3', 'aiosqlite>=0.20,<1',
        'alembic>=1.14,<2', 'redis>=5.2,<7', 'fakeredis>=2.26,<3',
        'numpy>=1.26,<3', 'scikit-learn>=1.5,<2', 'tiktoken>=0.8,<1', 'openai>=1.54,<3'
    )

    Write-Host "[3/3] 检查 .env ..." -ForegroundColor Cyan
    $envFile = Join-Path $Root '.env'
    if (Test-Path $envFile) {
        Write-Host "      .env 已存在，跳过。如需重建： python scripts\bootstrap_env.py --force"
    } else {
        Write-Host "      尚未生成 .env。请先设置密钥再运行：" -ForegroundColor Yellow
        Write-Host "        `$env:LLM_API_KEY = 'sk-xxxx'" -ForegroundColor Yellow
        Write-Host "        .\scripts\dev.ps1 setup" -ForegroundColor Yellow
    }

    # 自检：确认包真的装进了这个 venv（而不是被 PIP_TARGET 拐去了别处）
    Write-Host ""
    Write-Host "自检 ..." -ForegroundColor Cyan
    & $VenvPython -c "import fastapi, httpx, pydantic, sklearn, sys; print('  Python :', sys.version.split()[0]); print('  venv   :', sys.prefix); print('  依赖   : OK')"
    if ($LASTEXITCODE -ne 0) { throw "依赖自检失败" }
    Write-Host "`n[OK] 环境就绪" -ForegroundColor Green
}

function Task-Test {
    Initialize-Environment
    Assert-Venv
    Push-Location $ApiDir
    try { & $VenvPython -m pytest @Extra } finally { Pop-Location }
}

function Task-TestLive {
    Initialize-Environment
    Assert-Venv
    Push-Location $ApiDir
    try {
        Write-Host "[!] 将真实调用大模型 API，会产生 token 费用" -ForegroundColor Yellow
        & $VenvPython -m pytest -m live @Extra
    } finally { Pop-Location }
}

function Task-Lint {
    Initialize-Environment
    Assert-Venv
    Push-Location $ApiDir
    try { & $VenvPython -m ruff check . @Extra } finally { Pop-Location }
}

function Task-Fmt {
    Initialize-Environment
    Assert-Venv
    Push-Location $ApiDir
    try {
        & $VenvPython -m ruff format .
        & $VenvPython -m ruff check --fix .
    } finally { Pop-Location }
}

function Task-Check {
    Write-Host "=== 0/4 修复 .ps1 的 UTF-8 BOM ===" -ForegroundColor Cyan
    # 【为什么这个自愈步骤必须在最前面】
    # 本机 PowerShell 是 5.1，读无 BOM 的 .ps1 会按 GBK 解码，
    # 而 GBK 解码 UTF-8 中文会吞掉后续字节（常是换行或引号），
    # 导致语法树崩溃，报错位置却指向一句无辜的中文注释。
    #
    # 关键在于：**任何编辑 .ps1 的工具都可能顺手去掉 BOM**
    # （本项目的 edit 工具就会）。所以这不是一次性修复，
    # 而是每次改动后都要重做的事 —— 放进 check 就变成自动的。
    # 测试里另有门禁（TestPowerShellEncoding）兜底。
    Initialize-Environment
    Assert-Venv
    & $VenvPython (Join-Path $PSScriptRoot 'fix_ps1_bom.py')

    # 【为什么每个阶段都要显式检查 $LASTEXITCODE】
    #
    # PowerShell 调用外部程序（python）时**不会**因为非零退出码而中止 ——
    # 脚本会继续往下跑。所以原来的实现里，ruff 报了 lint 错误、
    # pytest 有失败用例，最后**依然打印"[OK] 全部通过，可以提交"**。
    #
    # 一个会在失败时报告成功的检查流水线，比没有流水线更糟：
    # 没有流水线时人会自己去跑测试；有流水线但它在撒谎，
    # 人会**相信它**并跳过检查。这是真实发生的 ——
    # 写这段时就出现过"lint 报 RUF043 但 check 说全部通过"。
    $failed = @()

    Write-Host "`n=== 1/4 格式化 ===" -ForegroundColor Cyan
    Task-Fmt
    if ($LASTEXITCODE -ne 0) { $failed += "格式化" }

    Write-Host "`n=== 2/4 静态检查 ===" -ForegroundColor Cyan
    Task-Lint
    if ($LASTEXITCODE -ne 0) { $failed += "静态检查" }

    Write-Host "`n=== 3/4 测试 ===" -ForegroundColor Cyan
    Task-Test
    if ($LASTEXITCODE -ne 0) { $failed += "测试" }

    Write-Host ""
    if ($failed.Count -gt 0) {
        Write-Host "[FAIL] 以下阶段未通过：$($failed -join '、')" -ForegroundColor Red
        Write-Host "       不要提交。" -ForegroundColor Red
        # 显式设退出码：否则从 CI 或别的脚本调用时，外部只能看到 0
        $global:LASTEXITCODE = 1
        exit 1
    }
    Write-Host "[OK] 全部通过，可以提交" -ForegroundColor Green
}

function Task-Cli {
    Initialize-Environment
    Assert-Venv
    & $VenvPython (Join-Path $ApiDir 'cli.py') @Extra
}

function Task-Serve {
    Initialize-Environment
    Assert-Venv
    Write-Host "API 文档: http://127.0.0.1:8000/docs" -ForegroundColor Green
    & $VenvPython -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload --app-dir $ApiDir
}

function Task-Tools {
    Initialize-Environment
    Assert-Venv
    # 刻意不在这里内嵌 Python 代码：PowerShell 的 here-string 与 Python 引号
    # 混在一起是维护灾难（转义、插值、编码三重坑）。独立脚本更清晰。
    & $VenvPython (Join-Path $PSScriptRoot 'list_tools.py')
}

# ============================================================
# 微服务拆分（P4）
# ============================================================
function Task-Rag {
    Initialize-Environment
    Assert-Venv
    # 独立进程运行检索服务：它的 CPU 占用不再影响 API 的响应延迟。
    # 注意端口 8001 与 docker-compose 里 rag 服务的端口保持一致 ——
    # 本地与容器两套拓扑用同一个端口，可以少一类"本地能跑容器不行"的问题。
    Write-Host "RAG 文档: http://127.0.0.1:8001/docs" -ForegroundColor Green
    & $VenvPython -m uvicorn app.rag_service.main:app --host 127.0.0.1 --port 8001 --reload --app-dir $ApiDir
}

function Task-ServeSplit {
    Initialize-Environment
    Assert-Venv
    # 关键就是这一个环境变量：它让 agent 走 HTTP 而不是本进程内检索。
    # 启动后用 GET /healthz 确认 rag_backend=remote ——
    # **配置错了不会报错**，只会静默退回单体，所以必须核对。
    $env:RAG_SERVICE_URL = 'http://127.0.0.1:8001'
    Write-Host "拆分拓扑：API:8000 → RAG:8001" -ForegroundColor Green
    Write-Host "启动后请确认 /healthz 的 rag_backend=remote" -ForegroundColor Yellow
    & $VenvPython -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload --app-dir $ApiDir
}

function Task-Worker {
    Initialize-Environment
    Assert-Venv
    # worker 需要一个跨进程共享的队列，否则它收不到任何任务 ——
    # fakeredis 也不行，它是进程内的假实现，不跨进程。
    # 所以这里固定用 redis；连不上时 worker_main 会以退出码 2 明确报错，
    # 而不是对着空队列静默等待（那是最难排查的一类故障）。
    $env:TASK_BACKEND = 'redis'
    Write-Host "独立 worker（TASK_BACKEND=redis，需要本机 Redis 已启动）" -ForegroundColor Green
    Push-Location $ApiDir
    try { & $VenvPython -m app.worker_main } finally { Pop-Location }
}

function Task-VerifySplit {
    Initialize-Environment
    Assert-Venv
    & $VenvPython (Join-Path $PSScriptRoot 'verify_split.py')
}

function Task-LoadTest {
    Initialize-Environment
    Assert-Venv
    if ($Extra.Count -eq 0) {
        # 默认带 --contention：CPU 争抢实验是"为什么要拆 RAG 服务"的直接证据，
        # 只跑延迟基线会漏掉最有说服力的那组数字。
        $Extra = @('--concurrency', '8', '--duration', '8', '--contention',
                   '--json-out', 'eval_results/loadtest.json')
    }
    & $VenvPython (Join-Path $PSScriptRoot 'loadtest.py') @Extra
}

# ============================================================
# 求职转化（P5）：演示
# ============================================================
function Task-RecordDemo {
    Initialize-Environment
    Assert-Venv
    # 前提是 API 正在跑（真实链路，需要 LLM 额度）。
    # 录完会做 PII 检查：简历原文会出现在工具返回里，命中则拒绝写出。
    Write-Host "录制演示事件流（需要 API 已启动且有 LLM 额度）" -ForegroundColor Green
    & $VenvPython (Join-Path $PSScriptRoot 'record_demo.py') @Extra
}

function Task-Demo {
    Initialize-Environment
    Assert-Venv
    # 【离线演示模式】
    # 优先用提交在仓库里的 PII-free 录制；本地录了含真实简历的版本就用本地那份。
    $local = Join-Path $Root 'data\demo\transcript.json'
    $public = Join-Path $Root 'demo\transcript-public.json'
    if (Test-Path $local) {
        $env:DEMO_REPLAY_FILE = $local
        Write-Host "使用本地录制（含真实简历，已在 gitignore 内）" -ForegroundColor Yellow
    } elseif (Test-Path $public) {
        $env:DEMO_REPLAY_FILE = $public
        Write-Host "使用仓库内的 PII-free 录制" -ForegroundColor Green
    } else {
        Write-Host "[!] 找不到录制文件。先跑：.\scripts\dev.ps1 record-demo" -ForegroundColor Red
        return
    }
    # 默认加速 3 倍：去掉录制时的网络冷场，但保持事件之间的相对节奏，
    # token 依然是逐个出现的。
    if (-not $env:DEMO_REPLAY_SPEED) { $env:DEMO_REPLAY_SPEED = '3' }

    Write-Host "离线演示模式：/api/chat/stream 将重放录制的事件流，不会调用模型" -ForegroundColor Green
    Write-Host "启动后**务必**确认 /healthz 的 demo_replay 字段不是 off" -ForegroundColor Yellow
    Write-Host "API 文档: http://127.0.0.1:8000/docs" -ForegroundColor Green
    & $VenvPython -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --app-dir $ApiDir
}

function Task-VerifyDemo {
    Initialize-Environment
    Assert-Venv
    & $VenvPython (Join-Path $PSScriptRoot 'verify_demo.py') @Extra
}

function Task-InstallHooks {
    Initialize-Environment
    Assert-Venv
    # 把 scripts/hooks/ 下版本化的 hook 装到 .git/hooks。
    # hook 的真实内容随代码提交（可以被 review），.git/hooks 里只是安装产物 ——
    # 因为 .git/ 不进版本控制，直接写进去的话换台机器克隆下来就没有了。
    & $VenvPython (Join-Path $PSScriptRoot 'install_hooks.py') @Extra
}

function Task-ScanPii {
    Initialize-Environment
    Assert-Venv
    # 扫**所有提交的所有 blob**，而不是当前工作区 ——
    # git 是只追加的，删掉文件不等于它从历史里消失。
    & $VenvPython (Join-Path $PSScriptRoot 'scan_history_pii.py') @Extra
}

function Task-Push {
    Initialize-Environment
    Assert-Venv
    # 【为什么把扫描和推送绑在一起】
    # 这一步不该靠人记得。首次推送尤其关键：推出去之后历史就公开了，
    # 收不回来 —— 而"我忘了先扫一遍"是唯一没有补救机会的错误。
    Write-Host "=== 推送前全历史 PII 扫描 ===" -ForegroundColor Cyan
    & $VenvPython (Join-Path $PSScriptRoot 'scan_history_pii.py')
    if ($LASTEXITCODE -ne 0) {
        Write-Host "`n[!] 扫描有命中 —— 先确认是误报还是真泄漏。" -ForegroundColor Yellow
        Write-Host "    已判定的误报来源：" -ForegroundColor Yellow
        Write-Host "      · pnpm-lock.yaml 的 264 处'邮箱'：npm 包名的 @（aix-ppc64@0.28.2）" -ForegroundColor Yellow
        Write-Host "      · resume.sample.md / test_rag.py：zhangsan@example.com、a@b.com" -ForegroundColor Yellow
        Write-Host "      · 文档里的 AIVideo：你自己另一个公开项目名" -ForegroundColor Yellow
        $ans = Read-Host "仍要继续推送吗？（输入 yes 继续）"
        if ($ans -ne 'yes') { Write-Host "已取消"; return }
    }
    Write-Host "`n=== 推送 ===" -ForegroundColor Cyan
    & $VenvPython (Join-Path $PSScriptRoot 'push_github.py') @Extra
}

function Task-Help {
    Get-Content $PSCommandPath | Select-String -Pattern '^#   \.' | ForEach-Object {
        $_.Line -replace '^#   ', ''
    }
}

switch ($Task) {
    'setup'     { Task-Setup }
    'install'   { Initialize-Environment; Assert-Venv; Invoke-Pip $Extra }
    'test'      { Task-Test }
    'test-live' { Task-TestLive }
    'lint'      { Task-Lint }
    'fmt'       { Task-Fmt }
    'check'     { Task-Check }
    'cli'       { Task-Cli }
    'serve'     { Task-Serve }
    'tools'     { Task-Tools }
    'rag'       { Task-Rag }
    'serve-split' { Task-ServeSplit }
    'worker'    { Task-Worker }
    'verify-split' { Task-VerifySplit }
    'loadtest'  { Task-LoadTest }
    'record-demo' { Task-RecordDemo }
    'demo'      { Task-Demo }
    'verify-demo' { Task-VerifyDemo }
    'install-hooks' { Task-InstallHooks }
    'scan-pii'  { Task-ScanPii }
    'push'      { Task-Push }
    default     { Task-Help }
}
