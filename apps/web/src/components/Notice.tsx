/**
 * 通用提示条。
 *
 * 存在的唯一理由是**让错误可操作**：本项目里最可能出现的失败是
 * "后端没启动"，而浏览器在这种情况下只会给一句 `Failed to fetch`。
 * 一个新人看到这句话完全不知道该干什么，所以这里必须明确告诉他：
 * 运行哪个命令、在哪个目录、然后点哪里重试。
 *
 * 三种语气对应三类信息（不是三种颜色装饰）：
 *   error —— 功能不可用，必须处理（后端连不上、接口报错）
 *   warn  —— 功能可用但有风险（会话存在内存里、输出被截断）
 *   info  —— 中性补充说明
 */

import type { ReactNode } from 'react'

import { IconAlert, IconInfo } from './Icons'

export interface NoticeProps {
  tone: 'error' | 'warn' | 'info'
  title: string
  text?: string
  /** 原样展示的命令或技术细节（等宽字体）。 */
  hint?: string
  action?: ReactNode
  role?: 'alert' | 'status'
}

export function Notice({ tone, title, text, hint, action, role = 'status' }: NoticeProps) {
  return (
    <div className={`notice notice--${tone}`} role={role}>
      <span className="notice__icon">
        {tone === 'info' ? <IconInfo size={15} /> : <IconAlert size={15} />}
      </span>
      <span className="notice__body">
        <span className="notice__title">{title}</span>
        {text ? <span className="notice__text">{text}</span> : null}
        {hint ? <code className="notice__hint">{hint}</code> : null}
      </span>
      {action ? <span className="notice__action">{action}</span> : null}
    </div>
  )
}
