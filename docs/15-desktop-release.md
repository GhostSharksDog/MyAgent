# Windows 桌面发行与本机存储

2026-10-08，本版面向单机、单个 Windows 用户。源码入口保留原配置，发行包使用独立用户目录。
首轮基线为后端1524通过/1 live跳过、前端216通过；初版完成1543通过/1 live跳过、前端219通过。
MCP名称及可用性修复后的最新结果为1553后端通过/1 live跳过、219前端通过、501项Edge与51项最终ZIP检查通过，见 [修复记录](16-mcp-availability.md)。
真实模型验证额度没有重新使用；所有聊天验收来自明确标识的合成模型。

## 使用下载包

完整解压 `Legacy-Windows-x64.zip`，双击 `Legacy/Legacy.exe`。
无需安装 Python、Node 或 Redis。首次只填写供应商、API 地址、模型名、API Key；保存不发送测试请求。
浏览器自动打开；关闭浏览器不退出服务，托盘提供「打开界面」「退出」。
重复启动打开已有实例。默认端口8000被占用时自动选择可用端口，始终使用127.0.0.1回环地址。
本包未签名；校验发行方提供的 SHA-256。运行时出错给出用户目录日志位置，不删除数据库或静默退回内存。

发行产物位于被忽略的 `data/releases/Legacy-Windows-x64.zip`，校验文件为同目录 `.zip.sha256`。
最新 ZIP 的摘要写入 [受控验证报告](evidence/mcp-availability-v1/verification.json)，每次重建须重新验收并更新摘要。
此前初版的 [历史报告](evidence/desktop-v1/verification.json) 保留原始摘要和结果，不能用于校验修复后的ZIP。
用户说明也随包提供 `使用说明.txt`，源码见 [简短使用说明](desktop-quickstart.txt)。

## 实际存储与默认值

| 内容 | 桌面默认位置或策略 |
|---|---|
| 模型与能力配置 | `%LOCALAPPDATA%\Legacy\config.env` |
| 会话、执行事实、摘要位置 | `legacy.db`，SQLite；完整对话仍保留 |
| 已确认的长期记忆 | `memory.db`，SQLite；每次变更事务提交，默认最近200条 |
| 结构化运行摘要 | `run-history.db`，SQLite；保持最近200轮、每轮256条事件上限 |
| 模型清单 / MCP 清单 | `models.json` / `mcp.json`，密钥查询只返回状态或掩码 |
| Desktop Commander 状态 | `desktop-commander/`，与其他客户端隔离 |
| 日志 | `logs/legacy.log` |
| 后台任务 | 进程内队列，不探测Redis，重启不保留待执行任务 |

程序资源位于发行目录 `_internal`，与用户数据和文件工作区分开。替换或移动程序目录不搬迁数据。
桌面新用户 `SESSION_TTL_SECONDS=0` 表示不过期；源码默认值与旧配置的显式过期值保留。
源代码仍读取仓库 `.env`，本轮没有修改用户现有 `.env`、私有 MCP 清单或私人工作区。
`LEGACY_DATA_DIR` 是开发者受控验证的目录覆盖入口，普通用户无需设置。

默认长期记忆开启不意味着自动提取聊天内容：只有设置中的明确提交，或模型 `remember_fact` 后的逐次批准才写入。
批准前、拒绝或等待时停止均不保存。写入成功即提交，后续模型失败不撤销；已完成的写入也不随取消回滚。
关闭记忆停止召回和新增，保留旧内容；编辑、删除与独立确认清空可以继续管理已有数据。
模型切换复用同一存储实例，不重新装载或在退出时覆盖数据库。数据库不可写或损坏明确报错。

ReAct 使用完整会话的剩余窗口、已保存摘要和已确认偏好。Plan/Supervisor 不读取过去对话，只可召回已确认的相关长期偏好。
摘要默认开启，摘要和处理前缀的摘要校验值保存在 `Session.meta.conversation_summary`。
成功、失败、取消都保存已处理位置，仅压缩新增历史；摘要失败/取消留下明确裁剪说明，完整历史不删除。
摘要调用使用原 RunContext 的 deadline、Usage 与上下文护栏。`session_saved` 与运行摘要的 `record_saved` 仍分别报告。

旧 JSON 记忆仅显式迁移，不自动寻找私人文件：

```powershell
& .venv/Scripts/python.exe scripts/migrate_memory.py --source <明确指定的旧facts.json> --destination <目标memory.db>
```

先备份源文件与已有目标库，再事务合并、按内容去重、保持容量边界。源文件保留，报告新增与最终保留数量。
该命令为源码维护入口，需要项目Python环境；桌面界面暂不提供迁移按钮。

## MCP 配置体验

服务列表仅展示名称、真实连接状态和配置/连接入口，一次编辑一个服务。
Tavily 直接输入 API Key；程序转换成 Bearer 请求头。Exa 使用公开免密钥入口，有服务方频率限制。
自定义 HTTP 使用地址、鉴权方式与密钥；自定义 stdio 的参数和环境变量逐项编辑。
工具描述、工具选择、只读免确认授权、代理、环境变量与启动参数放在高级设置。
编辑密钥留空保留旧值，明确勾选清除才删除。连接失败保留草稿，不显示「已连接」。
「保存并连接」和单服务启停自动协调现有全局 MCP 开关，不额外要求普通用户理解两层开关。

发行包包含 Filesystem2026.8.31、Desktop Commander0.2.52 与 Node24.20.0，首次均不启动。
填写明确的现有允许目录并点击连接后才运行；默认只选择现有11项文件工具/5项终端工具，仍逐次确认。
内置文件/终端权限与外部服务范围独立；cwd和允许目录不是终端沙箱。
已知写入、终端和未知工具不能授权免确认；具体只读信任继续绑定服务配置和完整工具定义。

## 构建与审核

固定 PyInstaller6.22.3，目录打包；Pillow12.1.1、pystray0.19.5。
Python发行依赖完整锁在 `scripts/desktop-requirements.lock`；两个本地服务及其依赖锁在 `scripts/desktop-node/package-lock.json`。
独立构建环境没有沿用开发环境的其他包：

```powershell
Remove-Item Env:PIP_TARGET -ErrorAction SilentlyContinue
& .venv/Scripts/python.exe -m venv data/desktop-build-env
& data/desktop-build-env/Scripts/python.exe -m pip install -r scripts/desktop-requirements.lock
& data/desktop-build-env/Scripts/python.exe -m pip install --no-deps -e services/api
& data/desktop-build-env/Scripts/python.exe -m pip check
Set-Location apps/web
pnpm install --frozen-lockfile
pnpm build
Set-Location ../..
& data/desktop-build-env/Scripts/python.exe scripts/build_desktop.py --node <Node24.20.0的node.exe绝对路径>
```

只收集应用代码、前端产物、Python运行依赖、Node、本地MCP与指定的两份公开seed文件。
不复制当前 `.env`、`data/`、私人目录或开发机密钥。Node二进制同时与官方SHASUMS及固定摘要核对。
资源清单/版本见包内 `build-manifest.json`；第三方许可证随包保留。

Desktop Commander补丁由 `scripts/patch_desktop_commander.py` 执行：先核对版本及两个完整源文件SHA-256，任一不匹配立即拒绝构建。
`dist/index.js` 禁用启动时Chrome自动下载；`dist/utils/feature-flags.js` 仅加载本地缓存，禁用首次远程获取及定期刷新。
修改保留显眼注释，原始与修改后的SHA保存在包内 `_internal/mcp/legacy-patches.json`，上游许可证不删除。
遥测/onboarding在隔离配置中关闭。未自动安装、启动未配置的外部服务。

冻结程序启动外部进程时短暂 `SetDllDirectoryW(NULL)`，再恢复应用DLL目录；过滤继承的冻结资源PATH。
Windows挂起启动、Job绑定、恢复运行、取消清理继续保留。
依据：[PyInstaller目录包](https://pyinstaller.org/en/stable/operating-mode.html)、[外部程序DLL环境说明](https://pyinstaller.org/en/stable/common-issues-and-pitfalls.html)、[Node许可证](https://raw.githubusercontent.com/nodejs/node/v24.20.0/LICENSE)。

新增手动工作流 `.github/workflows/desktop.yml` 可在用户推送后构建和上传ZIP，不创建发布、不自动推送。
远端尚未执行，GUI/托盘需要本机实测，不能把工作流构建成功当作桌面验收。

## 验证证据与限制

本机Windows11 x64 build26100；Python3.12.3独立构建。测试和证据采用公开内容、临时目录和合成模型：

| 验证 | 结果及说明 |
|---|---|
| 后端离线 | 1553通过，1 live跳过；一条既有Starlette弃用警告 |
| 前端 | 219通过；类型检查、构建通过 |
| 只读格式 / lint / lock | 全通过；核心运行锁51包；桌面构建环境pip check通过 |
| Edge | 501项通过，挂载前安装错误采集；1440×900、1024×768、390×844；合成API/SSE |
| 最终ZIP | `scripts/verify_desktop.py` 51项通过；实际解压后运行，中文/空格路径、独立数据目录，PATH仅含Windows工具 |
| 实际外部进程 | 随包Node连接两个MCP；批准读取临时公开文件，Desktop Commander及内置终端运行固定PowerShell输出 |
| 取消与恢复 | 内置终端断流后受控Job子进程已退出；同会话可立即继续；退出/重启保留会话、摘要、事实、记忆与运行摘要 |
| 首次配置 | 保存与清除密钥零模型请求；ReAct/Plan/Multi合成运行，独立模式不包含旧对话 |

回归直接检查后续模型实际收到的消息；覆盖记忆修改/删除/关闭、批准前未写入、拒绝、取消、并发写入、损坏库、失败提交、模型资源复用、删除会话不复活和重复摘要。
本机合成模型请求数和合成token见最新脱敏报告；这些不是付费模型Usage。报告记录本次ZIP的SHA-256及逐项检查结果。
迁移回归确认源文件及备份不变、目标保留原有内容、重复导入不重复保存。
界面回归验证Tavily字段、连接失败草稿与密钥清除、手机焦点、记忆CRUD、记忆确认/拒绝/停止。

最初冻结验收暴露两处真实缺陷，已修并重打包：漏收集aiosqlite；静态挂载遮挡退出路由导致405。
会话详情接口返回交替消息，验收使用 `turn_count`，并直接核对SQLite中的摘要位置，不把消息数当轮数。

实测限制：尚未在真实新机器、Windows10或Windows Sandbox运行；移除开发运行时PATH不等于卸载开发环境。
托盘退出通过与菜单相同的回调路径验证，未自动操作系统托盘弹出菜单。
Tavily/Exa本轮未发真实联网调用；合成模型不代表模型质量或付费服务兼容性。可选PDF/Word加载依赖未随首版Python包收集，md/txt可用。
真实模型额度已用尽，零付费模型调用；远端CI及手动发行工作流待用户推送后确认。

截图来自合成数据，见 [截图目录](screenshots/desktop/README.md)。原始日志在忽略的 `data/desktop-*-log.txt`、`data/desktop-full-tests.txt`、`data/desktop-frontend-tests.txt`。
