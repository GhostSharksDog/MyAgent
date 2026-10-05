/**
 * 顶部状态栏。
 *
 * 这里显示的每一项都来自后端接口，不是写死的文案：
 *
 *   模型名        → /api/meta   (model)
 *   会话 id       → 前端状态（会话是前端发起的）
 *   会话存储后端  → /api/meta   (session_backend)
 *   LLM 是否配置  → /healthz    (llm_configured)
 *
 * ------------------------------------------------------------
 * 为什么要把 session_backend 顶到这么显眼的位置
 * ------------------------------------------------------------
 * 后端默认用**进程内内存**存会话（`memory`）。这意味着：
 *   - 后端重启 → 会话全丢；
 *   - 多进程/多副本部署 → 请求落到另一个实例就读不到会话，
 *     表现为"历史莫名其妙消失了"。
 *
 * 这是运维最需要一眼看到的信息，也是本项目 P3 阶段要做 Redis 的原因。
 * 把它做成一个琥珀色的徽标，等于把一条技术债显式地摆在界面上 ——
 * 这比藏在文档里诚实得多，也正好是可以在面试里展开讲的一个点。
 */

import type { ApiMeta, HealthStatus } from '../lib/types'
import { shortId } from '../lib/format'
import { IconFolder, IconGear, IconGrid, IconMonitor, IconMoon, IconPanelLeft, IconRefresh, IconSun } from './Icons'
import type { ThemePreference } from '../hooks/useTheme'

export interface TopBarProps {
  meta: ApiMeta | null
  health: HealthStatus | null
  sessionId: string | null
  streaming: boolean
  loading: boolean
  sidebarOpen: boolean
  themePreference: ThemePreference
  onToggleSidebar: () => void
  onOpenTools: () => void
  onOpenSettings: () => void
  onOpenFiles: () => void
  /** 文件功能是否可用（未配置工作区时按钮置灰并说明差什么） */
  filesAvailable: boolean
  /** 右侧文件栏是否展开 —— 按钮要能显示激活态，否则用户不知道点了有没有生效 */
  filesOpen: boolean
  onRefresh: () => void
  onCycleTheme: () => void
}

function ThemeIcon({ preference }: { preference: ThemePreference }) {
  if (preference === 'light') return <IconSun size={14} />
  if (preference === 'dark') return <IconMoon size={14} />
  return <IconMonitor size={14} />
}

const THEME_LABEL: Record<ThemePreference, string> = {
  light: '明亮主题（点击切换为暗色）',
  dark: '暗色主题（点击切换为跟随系统）',
  system: '跟随系统（点击切换为明亮）',
}

export function TopBar({
  meta,
  health,
  sessionId,
  streaming,
  loading,
  sidebarOpen,
  themePreference,
  onToggleSidebar,
  onOpenTools,
  onOpenSettings,
  onOpenFiles,
  filesAvailable,
  filesOpen,
  onRefresh,
  onCycleTheme,
}: TopBarProps) {
  const model = meta?.model ?? health?.model ?? '未知模型'
  const backend = meta?.session_backend ?? health?.session_backend ?? ''
  const llmConfigured = health?.llm_configured ?? true

  return (
    <header className="topbar">
      <div className="topbar__brand">
        <button
          type="button"
          className="btn btn--ghost btn--icon"
          onClick={onToggleSidebar}
          title={sidebarOpen ? '收起会话列表' : '展开会话列表'}
          aria-label="切换会话列表"
          aria-expanded={sidebarOpen}
        >
          <IconPanelLeft size={15} />
        </button>
        <span className="topbar__mark">JP</span>
        <span className="topbar__title">Legacy</span>
        <span className="topbar__subtitle">手写 ReAct 内核 · Agent 控制台</span>
      </div>

      <div className="topbar__spacer" />

      <div className="topbar__meta">
        <span className="pill" title="当前使用的模型（来自 /api/meta）">
          <span className={streaming ? 'dot dot--live' : 'dot dot--ok'} />
          <span className="pill__value">{model}</span>
        </span>

        <span className="pill hide-sm" title="当前会话 id（会话模式下由服务端恢复历史）">
          <span className="pill__label">session</span>
          <span className="pill__value">{sessionId ? shortId(sessionId) : '无状态'}</span>
        </span>

        {backend ? (
          <span
            className={backend === 'memory' ? 'pill pill--warn' : 'pill pill--ok'}
            title={
              backend === 'memory'
                ? '会话存在进程内存里：后端重启即丢失，多进程/多副本部署时也不共享'
                : '会话存在 Redis 中，可跨进程与实例共享'
            }
          >
            <span className="pill__label">sessions</span>
            <span className="pill__value">
              {backend === 'memory' ? 'memory（多进程不共享）' : backend}
            </span>
          </span>
        ) : null}

        {!llmConfigured ? (
          <span className="pill pill--danger" title="后端没有检测到 LLM_API_KEY">
            <span className="pill__value">未配置密钥</span>
          </span>
        ) : null}
      </div>

      <div className="topbar__actions">
        <button
          type="button"
          className="btn btn--ghost btn--icon"
          onClick={onRefresh}
          title="刷新会话列表与服务状态"
          aria-label="刷新"
          disabled={loading}
        >
          <IconRefresh size={14} className={loading ? 'spin' : undefined} />
        </button>

        <button type="button" className="btn" onClick={onOpenTools} title="查看已注册的工具">
          <IconGrid size={13} />
          工具
          {meta ? <span className="muted mono">{meta.tool_count}</span> : null}
        </button>

        {/* 文件入口。未配置工作区时**置灰而不是隐藏** ——
            隐藏会让用户以为"这个产品没有文件功能"，
            而置灰 + title 说明了"差什么才能用"，那是可操作的。 */}
        <button
          type="button"
          className={filesOpen ? 'btn btn--primary' : 'btn'}
          onClick={onOpenFiles}
          aria-pressed={filesOpen}
          title={
            filesAvailable
              ? '浏览工作区文件并预览'
              : '尚未配置工作区根目录 —— 点右侧「设置」配置后启用'
          }
          aria-disabled={!filesAvailable}
        >
          <IconFolder size={13} />
          文件
        </button>

        <button
          type="button"
          className="btn"
          onClick={onOpenSettings}
          title="模型接入、知识库数据源、文件工作区"
        >
          <IconGear size={13} />
          设置
        </button>

        <button
          type="button"
          className="btn btn--ghost btn--icon"
          onClick={onCycleTheme}
          title={THEME_LABEL[themePreference]}
          aria-label="切换主题"
        >
          <ThemeIcon preference={themePreference} />
        </button>
      </div>
    </header>
  )
}
