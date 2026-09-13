/**
 * 前端入口。
 *
 * 只做三件事：挂载样式、开启 StrictMode、渲染 App。
 *
 * StrictMode 在开发模式下会**故意双调用** effect 与渲染函数 ——
 * 这正是我们想要的：它专门暴露"没有清理干净"的副作用
 * （例如 SSE 连接没断、事件监听没移除）。本项目里最相关的就是
 * useChat 在卸载时是否会 abort 掉进行中的流 —— 双调用会让问题当场暴露，
 * 而不是上线后以"偶发的重复请求"形式出现。
 */

import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'

import App from './App'
import './styles/index.css'

const container = document.getElementById('root')
if (!container) {
  throw new Error('找不到 #root 挂载点：请检查 index.html')
}

createRoot(container).render(
  <StrictMode>
    <App />
  </StrictMode>,
)
