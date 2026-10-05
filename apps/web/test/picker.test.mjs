/**
 * 目录选择交互的决策测试。
 *
 * ============================================================
 * 这三条规则为什么必须被测住
 * ============================================================
 * "打开文件夹"有两种界面（宿主弹系统对话框 / 应用内浏览），选错哪一种
 * 不会报错、也不会崩，只会让用户**点了没反应**：
 *
 *   1. 能力还没问到就先渲染按钮 → 用户点下去时接口还没回来，像是坏了
 *   2. 问不到能力时"乐观地"按 native 渲染 → 用户会一直点一个永远不会成功的按钮
 *   3. 不认识的能力值走到渲染分支里 → 服务端将来加了第三种交互，
 *      或前端回滚到旧版本时，用户看到的是一个点了就报错的按钮
 *
 * 这类错误的共同点是**没有失败信号**，只能靠断言挡住。而这些规则本身
 * 是纯函数（不需要浏览器、不需要渲染），所以测起来很便宜。
 */

import assert from 'node:assert/strict'
import test from 'node:test'

import { resolvePickerView } from '../src/lib/picker.ts'

test('能力还没问到时不渲染任何入口，也不假装已经可用', () => {
  const view = resolvePickerView(null, null)
  assert.equal(view.status, 'loading')
  assert.equal(view.interaction, null)
  assert.ok(view.reason.includes('正在确认'))
})

test('native：宿主能弹系统对话框，界面就是一个按钮', () => {
  const view = resolvePickerView({ kind: 'native', detail: '服务只监听本机地址' }, null)
  assert.equal(view.status, 'ready')
  assert.equal(view.interaction, 'native')
  assert.equal(view.reason, '服务只监听本机地址')
})

test('browse：宿主弹不出对话框，界面换成应用内浏览，并带上原因', () => {
  const view = resolvePickerView({ kind: 'browse', detail: '检测到 SSH 会话' }, null)
  assert.equal(view.status, 'ready')
  assert.equal(view.interaction, 'browse')
  assert.equal(view.reason, '检测到 SSH 会话')
})

test('问不到能力时**不能**退化成 native', () => {
  // 乐观降级在这里是最糟的选择：用户会反复点一个永远不会成功的按钮。
  // 正确行为是明说"没问到"，并把失败原因显示出来。
  const view = resolvePickerView(null, '无法连接后端服务')
  assert.equal(view.status, 'unavailable')
  assert.equal(view.interaction, null)
  assert.equal(view.reason, '无法连接后端服务')
})

test('不认识的能力值：隐藏入口并如实说明，而不是渲染成按钮', () => {
  // 这条对应 DSH 的 directory-picker seam 规则：
  // 遇到未知的 kind，消费方要**隐藏**目录选择入口，而不是让它失败。
  // 服务端多一种交互、或前端回滚到旧版本时，用户看到的是"这里没有这个功能"。
  const view = resolvePickerView({ kind: 'native-v2', detail: '' }, null)
  assert.equal(view.status, 'unavailable')
  assert.equal(view.interaction, null)
  assert.ok(view.reason.includes('native-v2'))
})

test('大小写不同不算已知能力（不做模糊匹配）', () => {
  // 线上格式是后端给的字符串，前端一旦开始"宽容匹配"，
  // 就可能把另一个语义的交互认成 native —— 宁可隐藏，也不要猜。
  const view = resolvePickerView({ kind: 'Native' }, null)
  assert.equal(view.interaction, null)
})

test('错误优先于能力：同时拿到两者时以错误为准', () => {
  // 现实里会出现"上一次问到的能力还在 state 里，这一次请求失败了"，
  // 那时不能拿旧能力当现状用。
  const view = resolvePickerView({ kind: 'native', detail: '' }, '连接被重置')
  assert.equal(view.status, 'unavailable')
  assert.equal(view.interaction, null)
  assert.equal(view.reason, '连接被重置')
})
