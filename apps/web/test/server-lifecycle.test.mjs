import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { stripTypeScriptTypes } from 'node:module'
import test from 'node:test'

const source = stripTypeScriptTypes(
  readFileSync(new URL('../src/hooks/useServerInfo.ts', import.meta.url), 'utf8'),
).replace(/^import[\s\S]*?from\s+['"][^'"]+['"]\s*;?\s*$/gm, '')
  .replace(/^export\s+/gm, '')
const makeHook = new Function(
  'useState', 'useRef', 'useCallback', 'useEffect',
  'ApiError', 'fetchHealth', 'fetchMeta', 'fetchTools',
  source + '\nreturn useServerInfo',
)

class ApiError extends Error {
  constructor(detail) { super(detail); this.detail = detail }
}

function deferred() {
  let resolve, reject
  const promise = new Promise((yes, no) => { resolve = yes; reject = no })
  return { promise, resolve, reject }
}

const tick = () => new Promise((resolve) => setImmediate(resolve))
const health = (model) => ({ status: 'ok', model, llm_configured: true })
const meta = (model) => ({ model, tool_count: 1 })
const tools = (name) => [{ name, description: name, parameters: {} }]

function fulfill(request, model) {
  request.health.resolve(health(model))
  request.meta.resolve(meta(model))
  request.tools.resolve(tools(model))
}

function harness() {
  const slots = []
  const requests = []
  let cursor = 0
  let updates = 0
  let pendingEffects = []
  const equalDeps = (before, after) => before && after && before.length === after.length &&
    before.every((value, index) => Object.is(value, after[index]))
  const useState = (initial) => {
    const index = cursor++
    if (!(index in slots)) slots[index] = typeof initial === 'function' ? initial() : initial
    return [slots[index], (value) => {
      updates++
      slots[index] = typeof value === 'function' ? value(slots[index]) : value
    }]
  }
  const useRef = (initial) => {
    const index = cursor++
    if (!(index in slots)) slots[index] = { current: initial }
    return slots[index]
  }
  const useCallback = (callback, deps) => {
    const index = cursor++
    if (!equalDeps(slots[index]?.deps, deps)) slots[index] = { callback, deps }
    return slots[index].callback
  }
  const useEffect = (effect, deps) => {
    const index = cursor++
    if (!equalDeps(slots[index]?.deps, deps)) {
      const previous = slots[index]
      slots[index] = { deps }
      pendingEffects.push(() => {
        previous?.cleanup?.()
        slots[index].cleanup = effect()
      })
    }
  }
  const hook = makeHook(useState, useRef, useCallback, useEffect, ApiError,
    () => {
      const request = { health: deferred(), meta: deferred(), tools: deferred() }
      requests.push(request)
      return request.health.promise
    },
    () => requests.at(-1).meta.promise,
    () => requests.at(-1).tools.promise,
  )
  return {
    requests,
    render() {
      cursor = 0
      const result = hook()
      const effects = pendingEffects
      pendingEffects = []
      effects.forEach((run) => run())
      return result
    },
    unmount() { slots.forEach((slot) => slot?.cleanup?.()) },
    get updates() { return updates },
  }
}

for (const first of ['old', 'current']) {
  test(`服务刷新乱序：${first} 先返回，旧模型与工具不覆盖当前刷新`, async () => {
    const h = harness()
    h.render() // Hook 的首次自动读取。
    const current = h.render().reload()
    assert.equal(h.requests.length, 2)
    if (first === 'old') {
      fulfill(h.requests[0], 'old')
      await tick()
      assert.equal(h.render().meta, null)
      assert.equal(h.render().loading, true, '旧请求不能提前解除最新请求的等待状态')
      fulfill(h.requests[1], 'new')
      await current
    } else {
      fulfill(h.requests[1], 'new')
      await current
      fulfill(h.requests[0], 'old')
      await tick()
    }
    const state = h.render()
    assert.equal(state.health.model, 'new')
    assert.equal(state.meta.model, 'new')
    assert.equal(state.tools[0].name, 'new')
    assert.equal(state.loading, false)
    assert.equal(state.error, null)
    assert.equal(state.offline, false)
  })
}

test('旧健康检查失败迟到不能把新配置标记成离线', async () => {
  const h = harness()
  h.render()
  const current = h.render().reload()
  fulfill(h.requests[1], 'new')
  await current
  h.requests[0].health.reject(new ApiError('旧服务不可用'))
  h.requests[0].meta.reject(new Error('旧元信息失败'))
  h.requests[0].tools.reject(new Error('旧工具失败'))
  await tick()
  const state = h.render()
  assert.equal(state.offline, false)
  assert.equal(state.error, null)
  assert.equal(state.meta.model, 'new')
})

test('最新工具读取失败保留已知工具和可用服务，明确展示部分失败', async () => {
  const h = harness()
  h.render()
  fulfill(h.requests[0], 'known')
  await tick()
  const current = h.render().reload()
  h.requests[1].health.resolve(health('new'))
  h.requests[1].meta.resolve(meta('new'))
  h.requests[1].tools.reject(new ApiError('需要访问密钥'))
  await current
  const state = h.render()
  assert.equal(state.offline, false)
  assert.equal(state.health.model, 'new')
  assert.equal(state.meta.model, 'new')
  assert.equal(state.tools[0].name, 'known')
  assert.equal(state.error, '工具列表加载失败：需要访问密钥')
  assert.equal(state.loading, false)
})

test('元信息失败使用后端具体原因，健康与工具仍独立可用', async () => {
  const h = harness()
  h.render()
  h.requests[0].health.resolve(health('available'))
  h.requests[0].meta.reject(new ApiError('缺少访问密钥'))
  h.requests[0].tools.resolve(tools('calculator'))
  await tick()
  const state = h.render()
  assert.equal(state.offline, false)
  assert.equal(state.meta, null)
  assert.equal(state.tools[0].name, 'calculator')
  assert.equal(state.error, '服务元信息加载失败：缺少访问密钥')
})

test('最新健康检查失败才标记离线，独立成功结果仍可读取', async () => {
  const h = harness()
  h.render()
  h.requests[0].health.reject(new ApiError('服务没有启动'))
  h.requests[0].meta.resolve(meta('cached-endpoint'))
  h.requests[0].tools.resolve(tools('calculator'))
  await tick()
  const state = h.render()
  assert.equal(state.offline, true)
  assert.equal(state.error, '服务没有启动')
  assert.equal(state.meta.model, 'cached-endpoint')
  assert.equal(state.tools[0].name, 'calculator')
  assert.equal(state.loading, false)
})

test('卸载后在途刷新不更新状态，reload 不再启动请求', async () => {
  const h = harness()
  h.render()
  const current = h.render().reload()
  h.unmount()
  const updates = h.updates
  fulfill(h.requests[0], 'old')
  fulfill(h.requests[1], 'new')
  await current
  await tick()
  assert.equal(h.updates, updates)
  await h.render().reload()
  assert.equal(h.requests.length, 2)
})
