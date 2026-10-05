/**
 * 目录选择交互的决策：把服务端声明的能力翻译成"界面渲染哪一种交互"。
 *
 * ============================================================
 * 为什么要单独抽成一个纯函数
 * ============================================================
 * "打开文件夹"有两种完全不同的界面：
 *
 *   native —— 一个按钮，点完由宿主进程弹出**系统**对话框，直接拿到路径；
 *   browse —— 一整个应用内浏览面板（因为宿主弹不出对话框）。
 *
 * 这段决策有三个容易写错的地方，而它们都不需要浏览器就能测：
 *
 *   1. **能力还没问到之前不能先渲染按钮**。否则用户点下去时接口还没回来，
 *      表现为"点了没反应"——而真正的原因是界面根本没进入可用状态。
 *   2. **问不到能力（服务不可达）时不能退化成 native**：那样用户会持续点一个
 *      永远不会成功的按钮。要明说"没问到能力"，并把原因显示出来。
 *   3. **不认识的能力值要隐藏入口，而不是让它失败**。这是 DSH 的
 *      directory-picker seam 定下的规则（"For an unknown kind, consumers hide
 *      directory picking rather than fail"）：服务端将来加了第三种交互、
 *      或前端回滚到旧版本时，用户看到的是"这里没有这个功能"，
 *      而不是一个点了就报错的按钮。
 *
 * 这三条都是"配置/兼容"类的逻辑，一旦写进组件里就会和渲染搅在一起，
 * 只能靠手点来验证。抽出来之后，它们变成一张可以断言的真值表。
 */

import type { PickerInfo } from './types'

export type PickerInteraction = 'native' | 'browse'

/** 界面加载能力时的三个状态之一，不是一个"可能与实际不符"的默认值。 */
export type PickerStatus = 'loading' | 'ready' | 'unavailable'

export interface PickerView {
  status: PickerStatus
  /** 该渲染哪种交互；`null` 表示**不显示**目录选择入口 */
  interaction: PickerInteraction | null
  /** 给用户看的一句话：为什么是这一种（或为什么没有） */
  reason: string
}

/**
 * 从"能力（或错误）"推导出界面状态。
 *
 * @param info   服务端声明的能力；还没拿到时为 null
 * @param error  询问能力时失败的原因；成功时为 null
 */
export function resolvePickerView(
  // 线上格式是后端给的字符串，这里刻意**不**收窄成联合类型：
  // 服务端将来多一种 kind 时，前端必须能识别出"不认识"并隐藏入口，
  // 而不是让一个不在类型里的值悄悄走到渲染分支里。
  info: { kind: string; detail?: string } | PickerInfo | null,
  error: string | null,
): PickerView {
  if (error) {
    return { status: 'unavailable', interaction: null, reason: error }
  }
  if (!info) {
    return { status: 'loading', interaction: null, reason: '正在确认宿主能否弹出系统对话框…' }
  }

  const detail = info.detail ?? ''
  if (info.kind === 'native') {
    return { status: 'ready', interaction: 'native', reason: detail }
  }
  if (info.kind === 'browse') {
    return { status: 'ready', interaction: 'browse', reason: detail }
  }
  // 不认识的能力：隐藏入口，并如实说明 —— 不失败、也不假装支持
  return {
    status: 'unavailable',
    interaction: null,
    reason: detail || `这台服务提供的目录选择方式（${info.kind}）这个界面版本还不认识。`,
  }
}

// ============================================================
// "打开文件夹"这一步到底做什么
// ============================================================
export type StartPickResult =
  /** 用户选好了：直接拿到绝对路径 */
  | { kind: 'picked'; path: string }
  /** 用户取消了：什么都不该发生 */
  | { kind: 'cancelled' }
  /** 宿主弹不出对话框（远程部署等）：交给应用内浏览面板 */
  | { kind: 'browse'; reason: string }
  /** 出错了（服务不可达、对话框失败）：也该交给面板兜底，但要说明原因 */
  | { kind: 'error'; message: string }

export interface StartPickDeps {
  fetchPickerCapability: () => Promise<{ kind: string; detail?: string }>
  pickDirectory: () => Promise<{ path: string | null; cancelled: boolean; hint: string }>
}

/**
 * 点"打开文件夹"时**直接**弹出系统对话框。
 *
 * ============================================================
 * 为什么不先开一个面板、让用户在里面再点一次
 * ============================================================
 * 中间那一层面板是有代价的：用户点"打开文件夹"的意图是"我要选一个文件夹"，
 * 而这个意图在能用系统对话框时**一步就能完成**。多出来的那一屏只是把
 * 同一个动作拆成两次，还顺带把"系统对话框"包装成了一个需要理解的概念。
 *
 * 所以这里的默认路径是：点一下 → 系统对话框 → 选完 → 工作区就位、文件栏展开。
 * 面板只在**宿主真的弹不出**（browse），或者弹的过程中出错时才出现 ——
 * 那时它是唯一可行的兜底，而不是一道多余的门槛。
 *
 * 【依赖为什么是参数而不是 import】
 * 这样这段决策就是纯的：不需要浏览器、不需要后端，就能把四条分支都断言一遍
 * （见 test/picker.test.mjs）。它恰好是最容易写错的一段 ——
 * 把"取消"当成"错误"弹红条、把"没有能力"当成"用户没选"，都是让人以为程序坏了
 * 的那类错误。
 */
export async function startFolderPick(deps: StartPickDeps): Promise<StartPickResult> {
  let capability: { kind: string; detail?: string } | null = null
  try {
    capability = await deps.fetchPickerCapability()
  } catch (err) {
    return {
      kind: 'error',
      message: err instanceof Error ? err.message : '无法确认目录选择能力',
    }
  }

  const view = resolvePickerView(capability, null)
  if (view.interaction !== 'native') {
    // 没有系统对话框（或能力不认识）→ 交给面板；面板会显示原因
    return { kind: 'browse', reason: view.reason || '当前部署没有系统文件夹对话框。' }
  }

  try {
    const result = await deps.pickDirectory()
    if (result.path) return { kind: 'picked', path: result.path }
    // 取消是正常操作：不弹错、不提示，什么都不做
    return { kind: 'cancelled' }
  } catch (err) {
    return { kind: 'error', message: err instanceof Error ? err.message : '打开系统对话框失败' }
  }
}
