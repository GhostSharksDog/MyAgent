import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { stripTypeScriptTypes } from 'node:module'
import test from 'node:test'

// 执行实际 Hook，替身只提供 React 状态槽和可控制的网络，不复制业务实现。
const source = stripTypeScriptTypes(
  readFileSync(new URL('../src/hooks/useSessions.ts', import.meta.url), 'utf8'),
).replace(/^import[\s\S]*?from\s+['"][^'"]+['"]\s*;?\s*$/gm, '')
  .replace(/^export\s+/gm, '')

class ApiError extends Error {
  constructor(detail) { super(detail); this.detail = detail }
}

const makeHook = new Function(
  'useState', 'useRef', 'useCallback', 'useEffect',
  'ApiError', 'createSession', 'deleteSession', 'getSession', 'listSessions',
  `${source}\nreturn useSessions`,
)

function deferred() {
  let resolve
  let reject
  const promise = new Promise((yes, no) => { resolve = yes; reject = no })
  return { promise, resolve, reject }
}

function session(id, turns = [{ role: 'user', content: `${id} 的历史` }]) {
  return { id, title: id, created_at: 1, updated_at: 1, total_tokens: 3, turns }
}

function harness(overrides = {}) {
  const slots = []
  let cursor = 0
  let pendingEffects = []
  let refreshes = 0
  const useState = (initial) => {
    const index = cursor++
    if (!(index in slots)) slots[index] = typeof initial === 'function' ? initial() : initial
    return [slots[index], (next) => {
      slots[index] = typeof next === 'function' ? next(slots[index]) : next
    }]
  }
  const useRef = (initial) => {
    const index = cursor++
    if (!(index in slots)) slots[index] = { current: initial }
    return slots[index]
  }
  const equalDeps = (previous, next) => previous && next && previous.length === next.length &&
    previous.every((value, index) => Object.is(value, next[index]))
  const useCallback = (callback, deps) => {
    const index = cursor++
    if (!equalDeps(slots[index]?.deps, deps)) slots[index] = { deps, callback }
    return slots[index].callback
  }
  const useEffect = (effect, deps) => {
    const index = cursor++
    if (!equalDeps(slots[index]?.deps, deps)) {
      const previous = slots[index]
      slots[index] = { deps, cleanup: undefined }
      pendingEffects.push(() => {
        previous?.cleanup?.()
        slots[index].cleanup = effect()
      })
    }
  }
  const hook = makeHook(useState, useRef, useCallback, useEffect, ApiError,
    overrides.createSession ?? (async () => session('new', [])),
    overrides.deleteSession ?? (async () => ({ deleted: true })),
    overrides.getSession ?? (async (id) => session(id)),
    async () => {
      refreshes += 1
      return overrides.listSessions ? overrides.listSessions() : { sessions: [], backend: 'memory' }
    },
  )
  const render = () => {
    cursor = 0
    const result = hook()
    const effects = pendingEffects
    pendingEffects = []
    effects.forEach((run) => run())
    return result
  }
  return { render, get refreshes() { return refreshes } }
}

test('加载新会话期间旧 ID 与详情保持一致，成功后一起切换', async () => {
  const pending = deferred()
  const h = harness({ getSession: (id) => id === 'next' ? pending.promise : Promise.resolve(session(id)) })
  await h.render().select('old')
  const selecting = h.render().select('next')
  let state = h.render()
  assert.equal(state.transitioning, true)
  assert.equal(state.activeId, 'old')
  assert.equal(state.detail.id, 'old')
  pending.resolve(session('next'))
  await selecting
  state = h.render()
  assert.equal(state.transitioning, false)
  assert.equal(state.activeId, 'next')
  assert.equal(state.detail.id, 'next')
})

for (const first of ['A', 'B']) {
  test(`快速选择 A/B，${first} 先返回时只接受 B`, async () => {
    const a = deferred()
    const b = deferred()
    const h = harness({ getSession: (id) => id === 'A' ? a.promise : id === 'B' ? b.promise : Promise.resolve(session(id)) })
    await h.render().select('old')
    const requestA = h.render().select('A')
    const requestB = h.render().select('B')
    if (first === 'A') {
      a.resolve(session('A'))
      await requestA
      assert.equal(h.render().activeId, 'old')
      assert.equal(h.render().transitioning, true, '旧请求 finally 不能解除新请求的等待状态')
      b.resolve(session('B'))
      await requestB
    } else {
      b.resolve(session('B'))
      await requestB
      a.resolve(session('A'))
      await requestA
    }
    const state = h.render()
    assert.equal(state.activeId, 'B')
    assert.equal(state.detail.id, 'B')
    assert.equal(state.transitioning, false)
  })
}

test('新建在发请求时就抢占旧 select，迟到历史不能落入新会话', async () => {
  const selecting = deferred()
  const creating = deferred()
  const h = harness({ getSession: () => selecting.promise, createSession: () => creating.promise })
  const oldRequest = h.render().select('old')
  const createRequest = h.render().create()
  selecting.resolve(session('old'))
  await oldRequest
  assert.equal(h.render().activeId, null)
  assert.equal(h.render().transitioning, true)
  creating.resolve(session('new'))
  assert.equal(await createRequest, true)
  const state = h.render()
  assert.equal(state.activeId, 'new')
  assert.deepEqual(state.detail.turns, [])
  assert.equal(state.transitioning, false)
})

test('取消选中使挂起 select 失效，不会恢复刚取消的上下文', async () => {
  const pending = deferred()
  const h = harness({ getSession: () => pending.promise })
  const request = h.render().select('late')
  h.render().deselect()
  pending.resolve(session('late'))
  await request
  const state = h.render()
  assert.equal(state.activeId, null)
  assert.equal(state.detail, null)
  assert.equal(state.transitioning, false)
})

test('删除当前会话发起时取消 pending select，删除后不被迟到结果复活', async () => {
  const pending = deferred()
  const deleting = deferred()
  const h = harness({
    getSession: (id) => id === 'late' ? pending.promise : Promise.resolve(session(id)),
    deleteSession: () => deleting.promise,
  })
  await h.render().select('current')
  const selecting = h.render().select('late')
  const removing = h.render().remove('current')
  pending.resolve(session('late'))
  await selecting
  assert.equal(h.render().activeId, 'current')
  assert.equal(h.render().transitioning, true)
  deleting.resolve({ deleted: true })
  await removing
  const state = h.render()
  assert.equal(state.activeId, null)
  assert.equal(state.detail, null)
  assert.equal(state.transitioning, false)
})

test('选择失败保留旧上下文，刷新列表后仍给出具体错误', async () => {
  const h = harness({ getSession: (id) => id === 'missing'
    ? Promise.reject(new ApiError('会话不存在，请刷新列表')) : Promise.resolve(session(id)) })
  await h.render().select('old')
  const previousRefreshes = h.refreshes
  await h.render().select('missing')
  const state = h.render()
  assert.equal(state.activeId, 'old')
  assert.equal(state.detail.id, 'old')
  assert.match(state.error, /会话不存在/)
  assert.equal(state.transitioning, false)
  assert.equal(h.refreshes, previousRefreshes + 1)
})

test('取消选中后迟到的新建返回 false，调用方不能据此清理草稿', async () => {
  const pending = deferred()
  const h = harness({ createSession: () => pending.promise })
  const request = h.render().create()
  h.render().deselect()
  pending.resolve(session('late'))
  assert.equal(await request, false)
  assert.equal(h.render().activeId, null)
  assert.equal(h.render().detail, null)
})

test('新建失败返回 false 并保留已加载上下文', async () => {
  const h = harness({ createSession: async () => { throw new ApiError('新建失败，请重试') } })
  await h.render().select('old')
  assert.equal(await h.render().create(), false)
  const state = h.render()
  assert.equal(state.activeId, 'old')
  assert.equal(state.detail.id, 'old')
  assert.match(state.error, /新建失败/)
  assert.equal(state.transitioning, false)
})

for (const detailReturnsFirst of [true, false]) {
  test('删除正在加载的目标后保留旧上下文，详情先返回：' + detailReturnsFirst, async () => {
    const pending = deferred()
    const deleting = deferred()
    const h = harness({
      getSession: (id) => id === 'B' ? pending.promise : Promise.resolve(session(id)),
      deleteSession: () => deleting.promise,
    })
    await h.render().select('A')
    const selecting = h.render().select('B')
    const removing = h.render().remove('B')
    if (detailReturnsFirst) {
      pending.resolve(session('B'))
      await selecting
      assert.equal(h.render().activeId, 'A')
      assert.equal(h.render().transitioning, true, '详情旧请求不能结束删除等待')
      deleting.resolve({ deleted: true })
      await removing
    } else {
      deleting.resolve({ deleted: true })
      await removing
      pending.resolve(session('B'))
      await selecting
    }
    const state = h.render()
    assert.equal(state.activeId, 'A', '被删除的 B 不能成为当前会话')
    assert.equal(state.detail.id, 'A')
    assert.equal(state.transitioning, false)
  })
}

test('删除期间再次加载同一个目标，删除成功仍作废迟到详情', async () => {
  const pending = deferred()
  const deleting = deferred()
  const h = harness({
    getSession: (id) => id === 'B' ? pending.promise : Promise.resolve(session(id)),
    deleteSession: () => deleting.promise,
  })
  await h.render().select('A')
  const removing = h.render().remove('B')
  const selecting = h.render().select('B')
  deleting.resolve({ deleted: true })
  await removing
  assert.equal(h.render().transitioning, false)
  pending.resolve(session('B'))
  await selecting
  assert.equal(h.render().activeId, 'A')
  assert.equal(h.render().detail.id, 'A')
})

test('删除其它会话不能作废正在加载的新目标', async () => {
  const pending = deferred()
  const h = harness({
    getSession: (id) => id === 'C' ? pending.promise : Promise.resolve(session(id)),
  })
  await h.render().select('A')
  const selecting = h.render().select('C')
  await h.render().remove('B')
  assert.equal(h.render().transitioning, true)
  pending.resolve(session('C'))
  await selecting
  assert.equal(h.render().activeId, 'C')
  assert.equal(h.render().detail.id, 'C')
  assert.equal(h.render().transitioning, false)
})

test('删除正在加载的目标失败时，保留原有效上下文并显示删除错误', async () => {
  const pending = deferred()
  const h = harness({
    getSession: (id) => id === 'B' ? pending.promise : Promise.resolve(session(id)),
    deleteSession: async () => { throw new ApiError('删除失败，请重试') },
  })
  await h.render().select('A')
  const selecting = h.render().select('B')
  await h.render().remove('B')
  pending.resolve(session('B'))
  await selecting
  const state = h.render()
  assert.equal(state.activeId, 'A')
  assert.equal(state.detail.id, 'A')
  assert.equal(state.transitioning, false)
  assert.match(state.error, /删除失败/)
})
