/**
 * Vite 配置。
 *
 * 三个决定，每个都有具体理由：
 *
 * 1. **代理而不是让浏览器直连 8000**。
 *    后端 CORS 只放行 `http://(localhost|127.0.0.1):*`（见 services/api/app/main.py），
 *    开发期直连虽然能跑，但会让前端代码里写死一个绝对地址：
 *    换端口、换机器、走 https 都要改代码。走代理后前端只认相对路径 `/api/*`，
 *    部署时由网关（或 VITE_API_BASE）决定真实后端在哪。
 *
 * 2. **SSE 必须关掉代理层的缓冲**。
 *    这是个真实的坑：任何中间层只要"攒够一批再转发"，打字机效果就会退化成
 *    "等 20 秒然后一次性吐出全文"——看起来像后端没做流式，实际是代理干的。
 *    Vite 的 http-proxy 默认不缓冲，但这里显式声明，避免以后换成 nginx 时忘掉。
 *
 * 3. **不引入路径别名（@/xxx）**。
 *    别名需要额外配置 tsconfig + vite 两处保持一致，收益只是少写几个 `../`。
 *    在依赖越少越好的前提下，这笔交易不划算。
 */

import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

/** 后端地址：开发时用 `.\scripts\dev.ps1 serve` 起在 8000。 */
const BACKEND = process.env.JOBPILOT_BACKEND ?? 'http://127.0.0.1:8000'

/** 共享的代理选项。
 *
 *  这里刻意保持最小配置：不写 `selfHandleResponse`、不手动读写 `proxyRes`。
 *  http-proxy 默认以流的方式转发响应体，而任何"读完整包再回写"的自定义逻辑
 *  都会把 SSE 变成一次性响应。想加日志可以监听 `proxyRes`，但不要缓冲 body。 */
const proxyOptions = {
  target: BACKEND,
  changeOrigin: true,
  ws: false,
}

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    strictPort: false,
    proxy: {
      // 对话、会话、元信息都挂在 /api 下
      '/api': proxyOptions,
      // 健康检查刻意在根路径（不在 /api 下），所以必须单独代理
      '/healthz': proxyOptions,
    },
  },
  build: {
    target: 'es2022',
    outDir: 'dist',
    sourcemap: false,
    // 产物不大，直接给一个明确的警告阈值，避免默认 500kB 的噪声
    chunkSizeWarningLimit: 800,
  },
})
