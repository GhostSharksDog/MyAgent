/**
 * 访问密钥（客户端那一半）的测试。
 *
 * ============================================================
 * 这里测的是"用户能不能看懂为什么它失败了"
 * ============================================================
 * 服务端加鉴权只需要几行中间件，而**用户侧真正会出问题的地方**是：
 *
 *   1. 密钥放错位置（URL 参数、Cookie）→ 自己泄漏自己
 *   2. 服务端要密钥、本地没有 → 每个接口各报一条 401，
 *      用户看到的是"什么都坏了"，而该做的是"去填一个密钥"
 *   3. 没启鉴权却提示"请填密钥" → 本地开发时凭空多出一件要理解的事
 *
 * 所以这些断言都对着"界面上显示什么、请求头里带什么"，
 * 而不是"函数返回值对不对"。
 */

import assert from 'node:assert/strict'
import test from 'node:test'

import { accessHeaders, resolveAccessNotice } from '../src/lib/access.ts'

test('没有密钥时不带任何认证头（默认形态必须完全干净）', () => {
  assert.deepEqual(accessHeaders(''), {})
})

test('有密钥时放在 X-API-Key 头里', () => {
  assert.deepEqual(accessHeaders('abc123'), { 'X-API-Key': 'abc123' })
})

test('密钥只走请求头 —— 不放 URL、不放 Cookie', async () => {
  // 这条是**纪律的机器化表达**：以后有人为了图方便把密钥塞进 query string，
  // 那它会进浏览器历史、Referer 和服务端访问日志 —— 而"图方便"正是这么发生的。
  const source = await import('node:fs/promises').then((fs) =>
    fs.readFile(new URL('../src/lib/access.ts', import.meta.url), 'utf8'),
  )
  assert.ok(!/URLSearchParams|document\.cookie/.test(source), 'access.ts 里出现了 URL/Cookie 路径')
})

test('三态：服务端不要密钥 → 界面什么都不提', () => {
  // 本地开发的常态。任何多余的提示都是在教用户忽略提示。
  assert.equal(resolveAccessNotice(false, false), 'none')
  assert.equal(resolveAccessNotice(false, true), 'none')
})

test('三态：服务端要、本地没有 → 必须提示', () => {
  assert.equal(resolveAccessNotice(true, false), 'required')
})

test('三态：服务端要、本地有 → 安静工作', () => {
  assert.equal(resolveAccessNotice(true, true), 'configured')
})

test('缺密钥提示只由服务端声明驱动，不靠"撞到 401 才猜"', () => {
  // 撞到 401 时用户看到的只是"请求失败"，而该做的是去填密钥 ——
  // 这两个信息完全不同，所以判定必须来自 /healthz 的 auth_required。
  assert.equal(resolveAccessNotice(true, false), 'required')
  assert.equal(resolveAccessNotice(false, false), 'none')
})
