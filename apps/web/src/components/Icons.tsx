/**
 * 手写内联图标集。
 *
 * 为什么不用图标库（lucide-react 等）：本项目一共用到十来个图标，
 * 引入一个库要多装一个包、多一层构建产物，而且为了 tree-shaking 生效
 * 还得小心 import 方式。直接写 SVG 的代价是每个图标 3~5 行，
 * 收益是**零依赖 + 完全可控的描边粗细与对齐**。
 *
 * 所有图标统一在 16×16 网格上、`stroke-width: 1.6`、`currentColor` 描边 ——
 * 这样它们在任何字号/颜色下都能和小字文本对齐，不会出现"某个图标偏胖"的问题。
 */

import type { ReactNode } from 'react'

export interface IconProps {
  size?: number
  className?: string
}

interface BaseProps extends IconProps {
  children: ReactNode
}

function Base({ size = 14, className, children }: BaseProps) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 16 16"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.6}
      strokeLinecap="round"
      strokeLinejoin="round"
      className={className}
      aria-hidden="true"
      focusable="false"
    >
      {children}
    </svg>
  )
}

export function IconPlus(props: IconProps) {
  return (
    <Base {...props}>
      <path d="M8 3.5v9M3.5 8h9" />
    </Base>
  )
}

export function IconTrash(props: IconProps) {
  return (
    <Base {...props}>
      <path d="M2.8 4.5h10.4M6.3 4.5V3.2h3.4v1.3M4.3 4.5l.6 8.3h6.2l.6-8.3" />
    </Base>
  )
}

export function IconRefresh(props: IconProps) {
  return (
    <Base {...props}>
      <path d="M13 8a5 5 0 1 1-1.6-3.7" />
      <path d="M13.2 2.8v3.4h-3.4" />
    </Base>
  )
}

export function IconCheck(props: IconProps) {
  return (
    <Base {...props}>
      <path d="M3.2 8.4 6.4 11.6 12.8 4.8" />
    </Base>
  )
}

export function IconX(props: IconProps) {
  return (
    <Base {...props}>
      <path d="M4 4l8 8M12 4l-8 8" />
    </Base>
  )
}

export function IconChevronRight(props: IconProps) {
  return (
    <Base {...props}>
      <path d="M6.2 3.6 10.6 8l-4.4 4.4" />
    </Base>
  )
}

export function IconLayers(props: IconProps) {
  return (
    <Base {...props}>
      <path d="M8 2.2 14 5.4 8 8.6 2 5.4z" />
      <path d="M2.8 8.8 8 11.6l5.2-2.8" />
    </Base>
  )
}

export function IconCoins(props: IconProps) {
  return (
    <Base {...props}>
      <circle cx="8" cy="8" r="5.6" />
      <path d="M8 5.4v5.2M6.4 6.9h3.2M6.4 9.1h3.2" />
    </Base>
  )
}

export function IconSun(props: IconProps) {
  return (
    <Base {...props}>
      <circle cx="8" cy="8" r="3" />
      <path d="M8 1.6v1.6M8 12.8v1.6M1.6 8h1.6M12.8 8h1.6M3.5 3.5l1.1 1.1M11.4 11.4l1.1 1.1M12.5 3.5l-1.1 1.1M4.6 11.4l-1.1 1.1" />
    </Base>
  )
}

export function IconMoon(props: IconProps) {
  return (
    <Base {...props}>
      <path d="M13.4 9.6A5.6 5.6 0 0 1 6.4 2.6a5.7 5.7 0 1 0 7 7z" />
    </Base>
  )
}

export function IconMonitor(props: IconProps) {
  return (
    <Base {...props}>
      <rect x="2" y="3" width="12" height="8.2" rx="1.4" />
      <path d="M6 13.4h4M8 11.2v2.2" />
    </Base>
  )
}

export function IconGrid(props: IconProps) {
  return (
    <Base {...props}>
      <rect x="2.2" y="2.2" width="5" height="5" rx="1.2" />
      <rect x="8.8" y="2.2" width="5" height="5" rx="1.2" />
      <rect x="2.2" y="8.8" width="5" height="5" rx="1.2" />
      <rect x="8.8" y="8.8" width="5" height="5" rx="1.2" />
    </Base>
  )
}

export function IconPanelLeft(props: IconProps) {
  return (
    <Base {...props}>
      <rect x="2" y="2.8" width="12" height="10.4" rx="1.6" />
      <path d="M6.4 2.8v10.4" />
    </Base>
  )
}

export function IconArrowUp(props: IconProps) {
  return (
    <Base {...props}>
      <path d="M8 13V3.4M4.2 7.2 8 3.4l3.8 3.8" />
    </Base>
  )
}

export function IconStop(props: IconProps) {
  return (
    <Base {...props}>
      <rect x="4.4" y="4.4" width="7.2" height="7.2" rx="1.4" fill="currentColor" stroke="none" />
    </Base>
  )
}

export function IconAlert(props: IconProps) {
  return (
    <Base {...props}>
      <path d="M8 2.4 14.6 13.6H1.4z" />
      <path d="M8 6.4v3.2M8 11.8v.1" />
    </Base>
  )
}

export function IconInfo(props: IconProps) {
  return (
    <Base {...props}>
      <circle cx="8" cy="8" r="5.8" />
      <path d="M8 7.4v3.4M8 5.2v.1" />
    </Base>
  )
}

export function IconClock(props: IconProps) {
  return (
    <Base {...props}>
      <circle cx="8" cy="8" r="5.8" />
      <path d="M8 4.8V8l2.2 1.4" />
    </Base>
  )
}

/** 加载中的转圈。用 currentColor 描边 + CSS 旋转（见 ui.css 的 .spin）。 */
export function IconSpinner({ size = 14, className }: IconProps) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 16 16"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.8}
      strokeLinecap="round"
      className={className ? `spin ${className}` : 'spin'}
      aria-hidden="true"
      focusable="false"
    >
      <circle cx="8" cy="8" r="5.6" opacity="0.25" />
      <path d="M13.6 8A5.6 5.6 0 0 0 8 2.4" />
    </svg>
  )
}

/** 未截断 / 已截断的标记用的小箭头（工具卡片上的"截断"提示）。 */
export function IconScissors(props: IconProps) {
  return (
    <Base {...props}>
      <circle cx="4" cy="4" r="1.8" />
      <circle cx="4" cy="12" r="1.8" />
      <path d="M5.4 5.2 13 12M5.4 10.8 13 4" />
    </Base>
  )
}

/** 设置入口（齿轮）。 */
export function IconGear(props: IconProps) {
  return (
    <Base {...props}>
      <circle cx="8" cy="8" r="2.2" />
      <path d="M8 1.6v1.8M8 12.6v1.8M14.4 8h-1.8M3.4 8H1.6M12.5 3.5l-1.3 1.3M4.8 11.2l-1.3 1.3M12.5 12.5l-1.3-1.3M4.8 4.8 3.5 3.5" />
    </Base>
  )
}

/** 文件夹（文件树里的目录节点）。 */
export function IconFolder(props: IconProps) {
  return (
    <Base {...props}>
      <path d="M1.8 4.2A1 1 0 0 1 2.8 3.2h3.1l1.4 1.6h5A1 1 0 0 1 13.3 5.8v6A1 1 0 0 1 12.3 12.8H2.8a1 1 0 0 1-1-1z" />
    </Base>
  )
}

/** 文件（文件树里的叶子节点）。 */
export function IconFile(props: IconProps) {
  return (
    <Base {...props}>
      <path d="M3.6 2.4h5l3.8 3.8v7.4a.8.8 0 0 1-.8.8H3.6a.8.8 0 0 1-.8-.8V3.2a.8.8 0 0 1 .8-.8z" />
      <path d="M8.6 2.4v3.8h3.8" />
    </Base>
  )
}
