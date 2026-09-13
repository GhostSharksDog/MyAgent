/**
 * 对话区消息列表。
 *
 * 只做一件容易做错的事：**把消息列表和滚动容器分开**。
 * 滚动容器（.chat__scroll）在 ChatPanel 里，这里只负责渲染行；
 * 这样"切换会话时清空滚动位置"和"流式时黏底"两件事都发生在容器上，
 * 不会和消息渲染纠缠在一起。
 *
 * 用 React.memo 包一层：流式过程中 items 数组每次都会换新引用，
 * 但已完成的那些消息对象是稳定的 —— memo 让它们跳过重渲染。
 * 一轮对话里如果有 20 条历史消息，这个优化是能感觉到的。
 */

import { memo } from 'react'

import type { ChatItem } from '../lib/types'
import { AssistantTurn } from './AssistantTurn'

const UserMessage = memo(function UserMessage({ content }: { content: string }) {
  return (
    <div className="msg-user">
      <div className="msg-user__bubble">{content}</div>
    </div>
  )
})

const MessageRow = memo(function MessageRow({ item }: { item: ChatItem }) {
  if (item.kind === 'user') return <UserMessage content={item.content} />
  return <AssistantTurn item={item} />
})

export interface MessageListProps {
  items: ChatItem[]
}

export function MessageList({ items }: MessageListProps) {
  return (
    <>
      {items.map((item) => (
        <MessageRow key={item.id} item={item} />
      ))}
    </>
  )
}
