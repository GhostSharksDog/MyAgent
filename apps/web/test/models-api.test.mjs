/**
 * 模型管理的纯函数测试。
 *
 * 【为什么测这些而不是渲染组件】
 * 项目的前端测试用 `node --test`，没有 jsdom —— 这是刻意的（见 styles.test.mjs
 * 的说明）。所以凡是"值得被钉住的判断"都被刻意写成纯函数放在 lib 里，
 * 组件只负责把状态摆到界面上。这个文件测的就是那些判断。
 *
 * 其中最有价值的两条：
 *   · **编辑时密钥留空必须合法** —— 界面拿到的只有掩码。若把密钥判成"编辑时
 *     也必填"，用户改个模型名就得重新找回密钥，而密钥他手上未必有。
 *     后端对"留空"的语义是"不改动"，前端校验必须与之一致。
 *   · **draftToPayload 不能把空密钥发出去** —— 发空字符串会被后端当成
 *     "不改动"（侥幸没事），但只要后端哪天的语义变了，这里就成了"改个名字
 *     把密钥清掉"。不发，才是明确表达"我没碰这个字段"。
 */

import assert from 'node:assert/strict'
import { test } from 'node:test'

import {
  PROVIDERS,
  canSubmitDraft,
  describeModel,
  draftFromModel,
  draftToPayload,
  emptyDraft,
  presetFor,
  presetIdForUrl,
  validateModelDraft,
} from '../src/lib/models-view.ts'

test('预设：每个都有地址与模型名，除了自定义', () => {
  for (const p of PROVIDERS) {
    assert.ok(p.label, '预设必须有名字')
    assert.ok(p.note, `${p.label} 缺少说明（用户靠它决定选哪个）`)
    if (p.id !== 'custom') {
      assert.match(p.baseUrl, /^https?:\/\//, `${p.label} 的地址不合法`)
      assert.ok(p.model, `${p.label} 缺少默认模型名`)
    }
  }
})

test('emptyDraft 用预设填好地址与模型名', () => {
  const draft = emptyDraft('deepseek')
  assert.equal(draft.baseUrl, 'https://api.deepseek.com/v1')
  assert.equal(draft.model, 'deepseek-chat')
  assert.equal(draft.id, '', '新草稿没有 id（后端据此判断是新增）')
})

test('presetFor 对未知 id 退到自定义，而不是抛异常', () => {
  // storage 里可能留着旧版本的 provider 值，不该让界面崩掉
  assert.equal(presetFor('这个供应商已经下线了').id, 'custom')
})

test('presetIdForUrl 认得出预设，认不出就给合理的兜底', () => {
  assert.equal(presetIdForUrl('https://api.deepseek.com/v1'), 'deepseek')
  // 末尾斜杠不该影响判断
  assert.equal(presetIdForUrl('https://api.deepseek.com/v1/'), 'deepseek')
  // 本地地址兜底成 ollama（标签更贴切），而不是"自定义"
  assert.equal(presetIdForUrl('http://127.0.0.1:11434/v1'), 'ollama')
  assert.equal(presetIdForUrl('http://localhost:8080/v1'), 'ollama')
  assert.equal(presetIdForUrl('https://内部网关.example.com/v1'), 'custom')
  assert.equal(presetIdForUrl(''), 'custom')
})

test('校验：地址必须是 http(s)，其它字段不能为空', () => {
  const base = emptyDraft('deepseek')
  base.apiKey = 'sk-test'

  assert.deepEqual(validateModelDraft(base), {}, '填好的草稿不该有错误')

  const noLabel = { ...base, label: '   ' }
  assert.ok(validateModelDraft(noLabel).label, '空白名字要被判错（列表靠它区分）')

  const badUrl = { ...base, baseUrl: 'api.deepseek.com/v1' }
  assert.match(validateModelDraft(badUrl).baseUrl ?? '', /http/, '缺协议头要给出可照做的提示')

  const noModel = { ...base, model: '' }
  assert.ok(validateModelDraft(noModel).model)
})

test('校验：编辑已有条目时，密钥留空是合法的', () => {
  const editing = draftFromModel({
    id: 'abc',
    label: '公司网关',
    provider: 'custom',
    base_url: 'http://gw.internal/v1',
    model: 'qwen-max',
  })
  assert.equal(editing.apiKey, '', '编辑态不回填密钥（界面只有掩码）')
  assert.deepEqual(validateModelDraft(editing), {}, '编辑态留空密钥必须合法')

  // 但新增时密钥是必填的 —— 否则会存下一条永远 401 的配置。
  // 注意判据是 `keyUnchanged`，不是"有没有 id"：一个从编辑态改回新增态的
  // 草稿，两者的区别正在这个标志上，而规则也必须跟着它走。
  const creating = emptyDraft('deepseek')
  assert.equal(creating.keyUnchanged, false, '新草稿没有"已存过密钥"这回事')
  assert.ok(validateModelDraft(creating).apiKey, '新增时缺密钥要判错')
})

test('draftToPayload 不把空密钥发出去', () => {
  const editing = draftFromModel({
    id: 'abc',
    label: 'A',
    provider: 'custom',
    base_url: 'http://x/v1',
    model: 'm',
  })
  const payload = draftToPayload(editing)
  assert.ok(!('api_key' in payload), '空密钥不该出现在请求体里（不发 = 明确表达"没碰"）')
  assert.equal(payload.id, 'abc')

  const creating = { ...emptyDraft('deepseek'), apiKey: '  sk-new  ' }
  const created = draftToPayload(creating)
  assert.equal(created.api_key, 'sk-new', '密钥要去掉首尾空白')
  assert.ok(!('id' in created), '新增不该带 id')
})

test('draftToPayload 会带上温度（按模型存）', () => {
  const draft = { ...emptyDraft('ollama'), apiKey: 'x', temperature: 0.7 }
  assert.equal(draftToPayload(draft).temperature, 0.7)
})

test('draftFromModel 对没有存过温度的条目给保守默认值', () => {
  const draft = draftFromModel({
    id: 'x',
    label: 'A',
    provider: 'custom',
    base_url: 'http://x/v1',
    model: 'm',
    temperature: null,
  })
  assert.equal(draft.temperature, 0.3, '与 LLM_TEMPERATURE 的默认保持一致')
})

test('canSubmitDraft 与 validateModelDraft 永远一致', () => {
  // 两者不一致会导致"按钮能点但点了没反应"这类最难解释的交互
  const cases = [
    emptyDraft('deepseek'),
    { ...emptyDraft('deepseek'), apiKey: 'k' },
    { ...emptyDraft('deepseek'), apiKey: 'k', baseUrl: '不是地址' },
    draftFromModel({
      id: '1',
      label: 'A',
      provider: 'custom',
      base_url: 'http://x/v1',
      model: 'm',
    }),
  ]
  for (const draft of cases) {
    const noErrors = Object.keys(validateModelDraft(draft)).length === 0
    assert.equal(canSubmitDraft(draft), noErrors, `不一致：${JSON.stringify(draft)}`)
  }
})

test('describeModel 显示模型名与主机名', () => {
  assert.equal(describeModel({ model: 'deepseek-chat', base_url: 'https://api.deepseek.com/v1' }), 'deepseek-chat · api.deepseek.com')
})

test('describeModel 对非法地址原样显示，不抛异常', () => {
  // 用户可以手改 data/models.json —— 一个坏地址不该让整页渲染失败
  const text = describeModel({ model: 'm', base_url: '这不是地址' })
  assert.equal(text, 'm · 这不是地址')
})
