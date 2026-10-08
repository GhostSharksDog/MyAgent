/**
 * 设置：**独立的圆角窗口 + 左栏分类**。
 *
 * ============================================================
 * 为什么从"右侧抽屉"改成"居中对话框"
 * ============================================================
 * 抽屉适合"看一眼就走"的辅助内容（工具列表、文件浏览），而设置是**要停留**的：
 * 用户会在这里比价、试连通性、来回改几遍。抽屉的窄条形态在这种场景下
 * 有两个具体代价：
 *   · 一长条表单只能上下滚，"模型"和"工作区"之间的跳转全靠滚动位置记忆；
 *   · 靠右贴边的形态让人下意识觉得"这是临时浮层"，不敢在里面做重操作。
 *
 * 所以改成居中窗口：左栏是分类（常驻可见，切换不丢失滚动位置），
 * 右侧是内容区，底部是**上下文相关**的操作区。
 *
 * ============================================================
 * 底部的按钮为什么是"看情况"的
 * ============================================================
 * 五个分类里只有两个（Agent / 工作区）需要"保存"这个动作：
 *   · 通用 —— 外观与偏好改了就生效，没有暂存态；
 *   · 模型 —— 每条都有自己明确的按钮（切换 / 添加 / 删除）；
 *   · 访问控制 —— 密钥存在本浏览器，有自己的"保存到本浏览器"。
 * 在一个不需要保存的页面上摆一个"保存并生效"，用户会点它，
 * 然后不知道到底存了什么 —— 那比没有按钮更让人不安。
 */

import { useEffect, useRef, useState } from 'react'
import type { ReactNode } from 'react'

import type { Preferences } from '../hooks/usePreferences'
import type { ThemePreference } from '../hooks/useTheme'
import { getAccessKey, setAccessKey } from '../lib/access'
import { parseTokenBudget } from '../lib/runtime'
import { parseApprovalTimeout } from '../lib/approvals'
import { parseTerminalTimeout } from '../lib/terminal'
import type { SettingsUpdatePayload } from '../lib/types'
import type { useSettings } from '../hooks/useSettings'
import { useDialogFocus } from '../hooks/useDialogFocus'
import { GeneralSection } from './settings/GeneralSection'
import { ModelsSection } from './settings/ModelsSection'
import { MCPSection } from './settings/MCPSection'
import { MemorySection } from './settings/MemorySection'
import { IconAlert, IconCheck, IconCoins, IconFolder, IconGear, IconLayers, IconX } from './Icons'

type SettingsApi = ReturnType<typeof useSettings>

export interface SettingsDialogProps {
  open: boolean
  onClose: () => void
  settings: SettingsApi
  themePreference: ThemePreference
  onThemeChange: (value: ThemePreference) => void
  prefs: Preferences
  onPrefChange: <K extends keyof Preferences>(key: K, value: Preferences[K]) => void
  onResetPrefs: () => void
  initialSection?: SettingsSection
  /** 保存或模型切换成功后刷新主界面的模型、权限与工具状态。 */
  onUpdated?: () => void
}

export type SettingsSection = 'general' | 'models' | 'agent' | 'workspace' | 'access' | 'mcp' | 'memory'

const NAV: { id: SettingsSection; label: string; icon: ReactNode; note: string }[] = [
  { id: 'memory', label: '记忆与存储', icon: <IconLayers size={16} />, note: '本机数据与已确认记忆' },
  { id: 'mcp', label: 'MCP', icon: <IconLayers size={16} />, note: '连接外部工具' },
  { id: 'general', label: '通用', icon: <IconGear size={16} />, note: '外观与交互' },
  { id: 'models', label: '模型', icon: <IconCoins size={16} />, note: '供应商与切换' },
  { id: 'agent', label: 'Agent', icon: <IconLayers size={16} />, note: '能力、预算与知识库' },
  { id: 'workspace', label: '工作区', icon: <IconFolder size={16} />, note: '文件与本机终端权限' },
  { id: 'access', label: '访问控制', icon: <IconAlert size={16} />, note: '本浏览器的访问密钥' },
]

/** 需要"保存"这个动作的分类 —— 见文件头关于底部按钮的说明。 */
const NEEDS_SAVE: SettingsSection[] = ['agent', 'workspace']

export function SettingsDialog({
  open,
  onClose,
  settings,
  themePreference,
  onThemeChange,
  prefs,
  onPrefChange,
  onResetPrefs,
  initialSection = 'general',
  onUpdated,
}: SettingsDialogProps) {
  const { saved, loading, saving, error, notice, load, save } = settings

  const [section, setSection] = useState<SettingsSection>(initialSection)

  // ---- 表单草稿（模型相关的字段已移入「模型」页，由那条链路自己管） ----
  const [profile, setProfile] = useState('general')
  const [workspaceRoot, setWorkspaceRoot] = useState('')
  const [corpusPaths, setCorpusPaths] = useState('')
  const [corpusIncludeSeed, setCorpusIncludeSeed] = useState(false)
  // 写权限（T23）与敏感文件权限。默认 false，打开要用户显式勾。
  const [fileWriteEnabled, setFileWriteEnabled] = useState(false)
  const [fileApprovalRequired, setFileApprovalRequired] = useState(true)
  const [fileApprovalTimeout, setFileApprovalTimeout] = useState('300')
  const [allowSecrets, setAllowSecrets] = useState(false)
  const [terminalEnabled, setTerminalEnabled] = useState(false)
  const [terminalTimeout, setTerminalTimeout] = useState('30')
  const [terminalApprovalTimeout, setTerminalApprovalTimeout] = useState('300')
  const [planBudget, setPlanBudget] = useState('60000')
  const [multiBudget, setMultiBudget] = useState('80000')
  const [runHistoryBackend, setRunHistoryBackend] = useState<'memory' | 'sql'>('memory')

  // 访问密钥是**客户端**状态（localStorage），不来自服务端配置
  const [accessKeyInput, setAccessKeyInput] = useState('')
  const [accessSaved, setAccessSaved] = useState(false)

  const closeRef = useRef<HTMLButtonElement>(null)
  const dialogRef = useRef<HTMLDivElement>(null)
  useDialogFocus({ open, containerRef: dialogRef, onClose, initialFocusRef: closeRef })

  useEffect(() => {
    if (!open) return
    setSection(initialSection)
    void load()
  }, [open, load, initialSection])

  // 每次打开重读访问密钥：它可能被另一个标签页改过
  useEffect(() => {
    if (!open) return
    setAccessKeyInput(getAccessKey())
    setAccessSaved(false)
  }, [open])

  // 已保存的值回来后灌进草稿
  useEffect(() => {
    if (!saved) return
    setProfile(saved.agent.profile)
    setWorkspaceRoot(saved.agent.workspace_root)
    setCorpusPaths(saved.agent.corpus_paths.join('\n'))
    setCorpusIncludeSeed(saved.agent.corpus_include_seed)
    setFileWriteEnabled(saved.agent.file_write_enabled)
    setFileApprovalRequired(saved.agent.file_approval_required ?? true)
    setFileApprovalTimeout(String(saved.agent.file_approval_timeout ?? 300))
    setAllowSecrets(saved.agent.file_allow_secrets)
    setTerminalEnabled(saved.agent.terminal_enabled === true)
    setTerminalTimeout(String(saved.agent.terminal_timeout ?? 30))
    setTerminalApprovalTimeout(String(saved.agent.terminal_approval_timeout ?? 300))
    setPlanBudget(String(saved.agent.plan_max_total_tokens ?? 60000))
    setMultiBudget(String(saved.agent.multi_max_total_tokens ?? 80000))
    setRunHistoryBackend(saved.run_history?.backend ?? 'memory')
  }, [saved, open])

  if (!open) return null

  const handleSave = async (): Promise<void> => {
    const plan = parseTokenBudget(planBudget)
    const multi = parseTokenBudget(multiBudget)
    if (section === 'agent' && (plan === null || multi === null)) return
    const approvalTimeout = parseApprovalTimeout(fileApprovalTimeout)
    const commandTimeout = parseTerminalTimeout(terminalTimeout, 600)
    const commandApprovalTimeout = parseTerminalTimeout(terminalApprovalTimeout, 3600)
    if (section === 'workspace' && (approvalTimeout === null || commandTimeout === null ||
      commandApprovalTimeout === null || (terminalEnabled && !workspaceRoot.trim()))) return
    // 键名是**后端的字段名**（snake_case）。写错会被后端 extra="forbid" 拒成 422，
    // 而那正是想要的：宁可当场报错，也不要静默地什么都没改。
    const payload: SettingsUpdatePayload = section === 'agent' ? {
      plan_max_total_tokens: plan ?? 60000,
      multi_max_total_tokens: multi ?? 80000,
      profile,
      run_history_backend: runHistoryBackend,
      corpus_paths: corpusPaths
        .split('\n')
        .map((line) => line.trim())
        .filter(Boolean),
      corpus_include_seed: corpusIncludeSeed,
    } : {
      workspace_root: workspaceRoot,
      file_write_enabled: fileWriteEnabled,
      file_approval_required: fileApprovalRequired,
      file_approval_timeout: approvalTimeout ?? 300,
      file_allow_secrets: allowSecrets,
      terminal_enabled: terminalEnabled,
      terminal_timeout: commandTimeout ?? 30,
      terminal_approval_timeout: commandApprovalTimeout ?? 300,
    }
    if (await save(payload)) onUpdated?.()
  }

  return (
    <div className="dialog-scrim" onClick={onClose} role="presentation">
      <div
        ref={dialogRef}
        className="dialog"
        role="dialog"
        aria-modal="true"
        aria-label="设置"
        tabIndex={-1}
        onClick={(event) => event.stopPropagation()}
      >
        <header className="dialog__head">
          <h2 className="dialog__title">设置</h2>
          <button
            ref={closeRef}
            type="button"
            className="btn btn--ghost btn--icon"
            onClick={onClose}
            title="关闭（Esc）"
            aria-label="关闭设置"
          >
            <IconX />
          </button>
        </header>

        <div className="dialog__main">
          <nav className="dialog__nav" aria-label="设置分类">
            {NAV.map((item) => (
              <button
                key={item.id}
                type="button"
                className={`navitem${section === item.id ? ' navitem--active' : ''}`}
                onClick={() => setSection(item.id)}
                aria-label={item.label}
                aria-current={section === item.id}
              >
                <span className="navitem__icon">{item.icon}</span>
                <span className="navitem__text">
                  <span className="navitem__label">{item.label}</span>
                  <span className="navitem__note">{item.note}</span>
                </span>
              </button>
            ))}
          </nav>

          <div className="dialog__content">
            {loading && <p className="drawer__note" role="status">正在读取最新配置…</p>}
            {error && (
              <div className="settings__alert settings__alert--bad" role="alert">
                <p>{error}</p>
                <button type="button" className="btn" disabled={loading} onClick={() => void load()}>
                  重新读取设置
                </button>
              </div>
            )}

            {section === 'general' && (
              <GeneralSection
                themePreference={themePreference}
                onThemeChange={onThemeChange}
                prefs={prefs}
                onPrefChange={onPrefChange}
                onResetPrefs={onResetPrefs}
              />
            )}

            {section === 'models' && <ModelsSection onChanged={onUpdated} />}
            {section === 'mcp' && <MCPSection onChanged={onUpdated} />}
            {section === 'memory' && <MemorySection onChanged={onUpdated} />}

            {section === 'agent' && (
              <>
                <section className="settings__group">
                  <h3 className="settings__legend">运行记录</h3>
                  <label className="settings__field settings__field--check">
                    <input type="checkbox" checked={runHistoryBackend === 'sql'}
                      onChange={(e) => setRunHistoryBackend(e.target.checked ? 'sql' : 'memory')} />
                    <span>持久保存运行摘要（SQLite）</span>
                  </label>
                  <p className="settings__hint settings__hint--block">
                    默认仅保存在内存，重启后清空。只记录状态、耗时、用量和工具执行摘要，
                    不记录问题、答案、工具参数及结果原文。存储切换需保存并重启服务；关闭不会删除已有数据库。
                  </p>
                  <p className="settings__hint">
                    当前存储：{!saved?.run_history ? '尚未读取' : saved.run_history.active_backend === 'sql' ? 'SQLite' : '内存'}
                    {saved?.run_history?.restart_required && ' · 已保存配置，等待重启'}
                    {saved?.run_history && ` · 最多保留 ${saved.run_history.max_records} 轮`}
                  </p>
                </section>
                <section className="settings__group">
                  <h3 className="settings__legend">Agent 身份</h3>
                  <label className="settings__field">
                    <span className="settings__label">能力集</span>
                    <select
                      className="settings__input"
                      value={profile}
                      onChange={(e) => setProfile(e.target.value)}
                    >
                      <option value="general">通用助手（默认）</option>
                      <option value="jobhunt">求职技能包</option>
                    </select>
                    <span className="settings__hint">
                      通用模式按配置使用计算、时间、知识库与文件工具。求职模式额外加载简历与岗位工具。
                    </span>
                  </label>
                </section>

                <section className="settings__group">
                  <h3 className="settings__legend">每轮模型用量预算</h3>
                  <p className="settings__hint settings__hint--block">
                    达到阈值后停止后续模型调用，保留已有结论。0 表示不限制。
                    在途调用可能超额；用量缺失时会显示「统计不完整」。
                  </p>
                  <label className="settings__field">
                    <span className="settings__label">Plan 累计 token 上限</span>
                    <input className="settings__input" type="number" min="0" step="1"
                      value={planBudget} onChange={(e) => setPlanBudget(e.target.value)} />
                  </label>
                  <label className="settings__field">
                    <span className="settings__label">Supervisor 累计 token 上限</span>
                    <input className="settings__input" type="number" min="0" step="1"
                      value={multiBudget} onChange={(e) => setMultiBudget(e.target.value)} />
                  </label>
                  {(parseTokenBudget(planBudget) === null || parseTokenBudget(multiBudget) === null) &&
                    <p role="alert" className="settings__alert">token 上限须为非负整数。</p>}
                </section>

                <section className="settings__group">
                  <h3 className="settings__legend">知识库</h3>
                  <p className="settings__hint settings__hint--block">
                    默认不加载文档。添加需要检索的文件或目录，一行一个；目录会递归扫描并跳过依赖与版本控制文件。
                  </p>

                  <label className="settings__field">
                    <span className="settings__label">文档路径</span>
                    <textarea
                      className="settings__input settings__textarea"
                      rows={3}
                      value={corpusPaths}
                      onChange={(e) => setCorpusPaths(e.target.value)}
                      placeholder={'data/notes\nD:/我的文档/handbook.md'}
                      spellCheck={false}
                    />
                  </label>

                  <label className="settings__field settings__field--check">
                    <input
                      type="checkbox"
                      checked={corpusIncludeSeed}
                      onChange={(e) => setCorpusIncludeSeed(e.target.checked)}
                    />
                    <span>加载公开示例语料</span>
                  </label>

                  {saved && (
                    <p className="settings__hint settings__hint--block">
                      {saved.agent.corpus_doc_count > 0 ? (
                        <>
                          <IconCheck size={14} /> 已加载 <strong>{saved.agent.corpus_doc_count}</strong> 篇文档
                        </>
                      ) : (
                        <>当前知识库为空，可添加文档或使用公开示例。</>
                      )}
                    </p>
                  )}
                </section>
              </>
            )}

            {section === 'workspace' && (
              <>
              <section className="settings__group">
                <h3 className="settings__legend">文件工作区</h3>
                <p className="settings__hint settings__hint--block">
                  文件工具只能访问所选目录<strong>以内</strong>的文件。留空关闭文件功能，默认仅允许读取。
                </p>
                <label className="settings__field">
                  <span className="settings__label">工作区根目录</span>
                  <input
                    className="settings__input"
                    value={workspaceRoot}
                    onChange={(e) => setWorkspaceRoot(e.target.value)}
                    placeholder="例如 D:/我的项目；留空关闭文件功能"
                    spellCheck={false}
                  />
                </label>

                {/* 写权限：默认关闭，必须显式打开。
                    它放在工作区这一页而不是"高级设置"里，因为它是
                    "Agent 能不能改我的文件"这个问题的直接答案。 */}
                <label className="settings__field settings__field--check">
                  <input
                    type="checkbox"
                    checked={fileWriteEnabled}
                    onChange={(e) => setFileWriteEnabled(e.target.checked)}
                  />
                  <span>
                    允许 Agent 写入文件
                    <span className="settings__hint">
                      可在工作区内新建与修改文件。默认关闭；开启后已完成的写入不会自动撤销，请先备份。
                    </span>
                  </span>
                </label>

                <label className="settings__field settings__field--check">
                  <input
                    type="checkbox"
                    checked={allowSecrets}
                    onChange={(e) => setAllowSecrets(e.target.checked)}
                  />
                  <span>
                    允许访问敏感文件（<code className="mono">.env</code> / 私钥）
                    <span className="settings__hint">
                      默认关闭。读取内容会发送给模型并显示在界面中；若同时开启写权限，凭据也可能被改动。
                    </span>
                  </span>
                </label>

                <label className="settings__field settings__field--check">
                  <input type="checkbox" checked={fileApprovalRequired} disabled={!fileWriteEnabled}
                    onChange={(event) => setFileApprovalRequired(event.target.checked)} />
                  <span>写入前预览确认（推荐）
                    <span className="settings__hint">
                      开启写权限后，先展示完整差异，由你逐次批准或拒绝；批准后再次核验文件与权限。
                      文件发生变化时，此次修改不写入，需要重新预览。
                    </span>
                  </span>
                </label>
                {fileWriteEnabled && !fileApprovalRequired && <p className="settings__alert" role="alert">
                  已选择直接写入：Agent 调用写工具时不再等待你的逐次确认，完成的修改不会自动撤销。
                </p>}
                <label className="settings__field">
                  <span className="settings__label">文件确认等待时限（秒）</span>
                  <input className="settings__input" type="number" min="0" step="any"
                    disabled={!fileWriteEnabled || !fileApprovalRequired} value={fileApprovalTimeout}
                    onChange={(event) => setFileApprovalTimeout(event.target.value)} />
                  <span className="settings__hint">默认 300 秒，0 表示不单独限时。等待仍消耗本轮时长预算。
                    确认方式与等待时限保存后从下一轮开始生效。</span>
                </label>
                {parseApprovalTimeout(fileApprovalTimeout) === null &&
                  <p role="alert" className="settings__alert">确认等待时限须为非负有限数字。</p>}

                <p className="settings__hint settings__hint--warn">
                  <IconAlert size={14} /> 只授权需要的目录。文件中的恶意指令可能影响模型行为，请留意工具操作。
                </p>
              </section>
              <section className="settings__group" aria-label="本机终端权限">
                <h3 className="settings__legend">本机终端</h3>
                <p className="settings__alert">
                  <strong>终端命令以当前服务账户权限执行，可访问任意文件、联网或启动程序。</strong>
                  工作区只指定起始目录，不构成沙箱。文件写权限与敏感文件开关不限制终端命令；
                  已产生的副作用不会自动撤销。
                </p>
                <label className="settings__field settings__field--check">
                  <input type="checkbox" checked={terminalEnabled}
                    onChange={(event) => setTerminalEnabled(event.target.checked)} />
                  <span>允许 Agent 执行本机终端命令
                    <span className="settings__hint">默认关闭，需显式设置工作区，并在支持的平台启用。
                      每条命令都先展示完整命令、目录和时限，由你批准后执行；此确认不能关闭。
                      仅支持 Web 流式对话，不提供交互式终端。</span>
                  </span>
                </label>
                {terminalEnabled && !workspaceRoot.trim() && <p className="settings__alert" role="alert">
                  请先填写工作区根目录，作为终端命令的默认起始目录。
                </p>}
                <label className="settings__field">
                  <span className="settings__label">终端执行时限（秒）</span>
                  <input className="settings__input" type="number" min="1" max="600" step="any"
                    disabled={!terminalEnabled} value={terminalTimeout}
                    onChange={(event) => setTerminalTimeout(event.target.value)} />
                  <span className="settings__hint">默认 30 秒，范围 1–600 秒。超时或停止时取消进程及子进程。</span>
                </label>
                {parseTerminalTimeout(terminalTimeout, 600) === null && <p className="settings__alert" role="alert">
                  终端执行时限须为 1–600 秒的有限数字。
                </p>}
                <label className="settings__field">
                  <span className="settings__label">终端确认等待时限（秒）</span>
                  <input className="settings__input" type="number" min="1" max="3600" step="any"
                    disabled={!terminalEnabled} value={terminalApprovalTimeout}
                    onChange={(event) => setTerminalApprovalTimeout(event.target.value)} />
                  <span className="settings__hint">默认 300 秒，范围 1–3600 秒。等待与执行都消耗本轮时长预算，
                    保存后从下一轮开始生效。</span>
                </label>
                {parseTerminalTimeout(terminalApprovalTimeout, 3600) === null && <p className="settings__alert" role="alert">
                  终端确认等待时限须为 1–3600 秒的有限数字。
                </p>}
              </section>
              </>
            )}

            {section === 'access' && (
              <section className="settings__group">
                <h3 className="settings__legend">访问控制</h3>
                <p className="settings__hint settings__hint--block">
                  连接启用鉴权的服务时，在此填写访问密钥。它只保存在<strong>这个浏览器</strong>中，
                  随 API 请求用于鉴权，不会修改服务端要求的密钥。
                </p>
                <label className="settings__field">
                  <span className="settings__label">访问密钥</span>
                  <input
                    className="settings__input"
                    type="password"
                    value={accessKeyInput}
                    placeholder="留空 = 不使用密钥"
                    autoComplete="off"
                    spellCheck={false}
                    onChange={(event) => {
                      setAccessKeyInput(event.target.value)
                      setAccessSaved(false)
                    }}
                  />
                </label>
                <div className="settings__actions">
                  <button
                    type="button"
                    className="btn btn--primary"
                    onClick={() => {
                      // 保存后不需要刷新页面：请求层每次现读密钥（见 lib/api.ts）
                      setAccessKey(accessKeyInput)
                      setAccessSaved(true)
                      onUpdated?.()
                    }}
                  >
                    保存到本浏览器
                  </button>
                  <button
                    type="button"
                    className="btn btn--ghost"
                    onClick={() => {
                      setAccessKey('')
                      setAccessKeyInput('')
                      setAccessSaved(true)
                      onUpdated?.()
                    }}
                  >
                    清除
                  </button>
                  {accessSaved && (
                    <span className="settings__result settings__result--ok">
                      <IconCheck size={14} /> 已保存（立即生效）
                    </span>
                  )}
                </div>
              </section>
            )}

            {notice && section !== 'mcp' && (
              <p className="settings__alert settings__alert--ok" role="status">
                <IconCheck size={14} /> {notice}
              </p>
            )}
          </div>
        </div>

        <footer className="dialog__foot">
          <span className="dialog__env">
            {section === 'agent' ? '能力与预算保存后生效；记录存储切换需重启' : NEEDS_SAVE.includes(section) ? '此页修改需保存后生效' : '界面偏好与服务配置'}
          </span>

          {NEEDS_SAVE.includes(section) ? (
            <span className="dialog__foot-actions">
              <button type="button" className="btn btn--ghost" onClick={onClose}>
                取消
              </button>
              <button
                type="button"
                className="btn btn--primary"
                onClick={() => void handleSave()}
                disabled={saving || loading || !saved ||
                  (section === 'workspace' && (parseApprovalTimeout(fileApprovalTimeout) === null ||
                    parseTerminalTimeout(terminalTimeout, 600) === null ||
                    parseTerminalTimeout(terminalApprovalTimeout, 3600) === null ||
                    (terminalEnabled && !workspaceRoot.trim()))) ||
                  (section === 'agent' && (parseTokenBudget(planBudget) === null || parseTokenBudget(multiBudget) === null))}
              >
                {saving ? '保存中…' : '保存此页'}
              </button>
            </span>
          ) : (
            <span className="dialog__foot-note">
              {section === 'models' && '保存模型后，点击「切换」启用'}
              {section === 'general' && '外观与交互偏好即时生效'}
              {section === 'access' && '保存或清除后立即用于请求'}
              {section === 'mcp' && '服务需保存；开关与工具授权即时生效'}
            </span>
          )}
        </footer>
      </div>
    </div>
  )
}
