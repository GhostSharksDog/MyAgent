/// <reference types="vite/client" />

/**
 * 环境变量的类型声明。
 *
 * 为什么要显式声明而不是用 `(import.meta.env as any)`：
 * 部署时前端要能指向另一个后端地址，这个开关必须在类型层面可见，
 * 否则"忘了配 VITE_API_BASE"只会在运行时表现为奇怪的 404。
 */
interface ImportMetaEnv {
  /** 后端基础地址。空字符串（默认）表示走 vite 代理 / 同源部署。 */
  readonly VITE_API_BASE?: string
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}
