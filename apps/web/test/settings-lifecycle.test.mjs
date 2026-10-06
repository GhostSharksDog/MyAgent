import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { stripTypeScriptTypes } from 'node:module'
import test from 'node:test'

// 执行真实 Hook 源码；只替换 React 容器与网络，保留实际并发控制。
const source = stripTypeScriptTypes(
  readFileSync(new URL('../src/hooks/useSettings.ts', import.meta.url), 'utf8'),
).replace(/^import[\s\S]*?from\s+['"][^'"]+['"]\s*;?\s*$/gm, '')
  .replace(/^export\s+/gm, '')
const makeHook = new Function(
  'useState', 'useRef', 'useCallback', 'useEffect',
  'fetchSettings', 'updateSettings', 'testLLMConnection',
  source + '\nreturn useSettings',
)

function deferred() {
  let resolve, reject
  const promise = new Promise((yes, no) => { resolve = yes; reject = no })
  return { promise, resolve, reject }
}

const settings = (root) => ({ agent: { workspace_root: root }, llm: { model: root } })
const tick = () => new Promise((resolve) => setImmediate(resolve))

function harness(overrides = {}) {
  const slots = []
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
  const reads = []
  const writes = []
  const hook = makeHook(useState, useRef, useCallback, useEffect,
    () => {
      const request = deferred()
      reads.push(request)
      return request.promise
    },
    (payload) => {
      const request = deferred()
      writes.push({ payload, ...request })
      return request.promise
    },
    overrides.testLLMConnection ?? (async () => ({ ok: true })),
  )
  return {
    reads, writes,
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
  test(`两个设置读取乱序：${first} 先返回，只接受最新配置`, async () => {
    const h = harness()
    const old = h.render().load()
    const current = h.render().load()
    if (first === 'old') {
      h.reads[0].resolve(settings('old'))
      await old
      assert.equal(h.render().saved, null)
      assert.equal(h.render().loading, true, '旧 finally 不能关闭最新读取的等待状态')
      h.reads[1].resolve(settings('new'))
      await current
    } else {
      h.reads[1].resolve(settings('new'))
      await current
      h.reads[0].resolve(settings('old'))
      await old
    }
    const state = h.render()
    assert.equal(state.saved.agent.workspace_root, 'new')
    assert.equal(state.loading, false)
    assert.equal(state.error, null)
  })
}

test('旧设置读取的异常不能污染新读取，也不能结束其等待状态', async () => {
  const h = harness()
  const old = h.render().load()
  const current = h.render().load()
  h.reads[0].reject(new Error('旧连接失败'))
  await old
  assert.equal(h.render().error, null)
  assert.equal(h.render().loading, true)
  h.reads[1].resolve(settings('current'))
  await current
  assert.equal(h.render().saved.agent.workspace_root, 'current')
})

for (const oldFirst of [true, false]) {
  test(`保存使初始读取失效：旧读取${oldFirst ? '先' : '后'}返回也不能回滚工作区`, async () => {
    const h = harness()
    const old = h.render().load()
    const saving = h.render().save({ workspace_root: 'new' })
    assert.equal(h.render().loading, false)
    assert.equal(h.render().saving, true)
    if (oldFirst) {
      h.reads[0].resolve(settings('old'))
      await old
      assert.equal(h.render().saved, null)
      assert.equal(h.render().saving, true)
    }
    h.writes[0].resolve({})
    await tick()
    assert.equal(h.reads.length, 2, '成功更新后仍重新读取派生配置')
    h.reads[1].resolve(settings('new'))
    assert.equal(await saving, true)
    if (!oldFirst) {
      h.reads[0].resolve(settings('old'))
      await old
    }
    const state = h.render()
    assert.equal(state.saved.agent.workspace_root, 'new')
    assert.equal(state.notice, '已保存并立即生效，无需重启')
    assert.equal(state.error, null)
    assert.equal(state.loading, false)
    assert.equal(state.saving, false)
  })
}

test('保存更新与回读期间的 load 不发起竞争读取，保存完成后可再次刷新', async () => {
  const h = harness()
  const saving = h.render().save({ workspace_root: 'new' })
  await h.render().load()
  assert.equal(h.reads.length, 0)
  assert.equal(h.render().saving, true)
  h.writes[0].resolve({})
  await tick()
  await h.render().load()
  assert.equal(h.reads.length, 1, '仅保存自己的回读在途')
  h.reads[0].resolve(settings('new'))
  assert.equal(await saving, true)
  const refresh = h.render().load()
  assert.equal(h.reads.length, 2)
  assert.equal(h.render().loading, true)
  h.reads[1].resolve(settings('refreshed'))
  await refresh
  assert.equal(h.render().saved.agent.workspace_root, 'refreshed')
})

test('同一帧内重复保存被同步锁挡住，失败后锁释放并可重试', async () => {
  const h = harness()
  const first = h.render().save({ workspace_root: 'first' })
  assert.equal(await h.render().save({ workspace_root: 'duplicate' }), false)
  assert.equal(h.writes.length, 1)
  assert.equal(h.render().saving, true)
  assert.equal(h.render().error, null)
  h.writes[0].reject(new Error('目录不存在'))
  assert.equal(await first, false)
  assert.equal(h.render().saving, false)
  assert.equal(h.render().error, '目录不存在')
  const retry = h.render().save({ workspace_root: 'retry' })
  assert.equal(h.writes.length, 2)
  assert.equal(h.render().error, null)
  h.writes[1].resolve({})
  await tick()
  h.reads[0].resolve(settings('retry'))
  assert.equal(await retry, true)
  assert.equal(h.render().saved.agent.workspace_root, 'retry')
})

test('保存后的回读失败保留已知配置，不显示虚假的成功反馈', async () => {
  const h = harness()
  const initial = h.render().load()
  h.reads[0].resolve(settings('known'))
  await initial
  const saving = h.render().save({ workspace_root: 'new' })
  h.writes[0].resolve({})
  await tick()
  h.reads[1].reject(new Error('配置回读失败'))
  assert.equal(await saving, false)
  assert.equal(h.render().saved.agent.workspace_root, 'known')
  assert.equal(h.render().notice, null)
  assert.equal(h.render().error, '配置回读失败')
  assert.equal(h.render().saving, false)
})

test('保存失败后迟到的初始读取仍失效，不抹掉当前失败原因', async () => {
  const h = harness()
  const old = h.render().load()
  const saving = h.render().save({ workspace_root: 'new' })
  h.writes[0].reject(new Error('没有保存权限'))
  assert.equal(await saving, false)
  h.reads[0].reject(new Error('过期读取失败'))
  await old
  assert.equal(h.render().error, '没有保存权限')
  assert.equal(h.render().saved, null)
  assert.equal(h.render().loading, false)
})

test('卸载使读取和保存失效，迟到结果不更新状态也不继续回读', async () => {
  const h = harness()
  const old = h.render().load()
  const saving = h.render().save({ workspace_root: 'new' })
  h.unmount()
  const updates = h.updates
  h.reads[0].resolve(settings('old'))
  h.writes[0].resolve({})
  await old
  assert.equal(await saving, false)
  assert.equal(h.reads.length, 1, '卸载后的更新完成不再启动回读')
  assert.equal(h.updates, updates)
  await h.render().load()
  assert.equal(await h.render().save({ workspace_root: 'ignored' }), false)
  assert.equal(h.reads.length, 1)
  assert.equal(h.writes.length, 1)
})
