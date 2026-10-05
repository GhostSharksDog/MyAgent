/**
 * 模型管理的**纯逻辑**：预设、草稿、校验、展示文案。
 *
 * ============================================================
 * 为什么与 models-api.ts 分成两个文件
 * ============================================================
 * 前端的测试跑在 `node --test` 里（没有 jsdom），它直接加载 `.ts` 源码，
 * 而 Node 的 ESM 解析器**不认无扩展名的相对导入**（`./access`）。
 * 所以"能被测试直接加载"的模块必须不依赖其它运行时模块。
 *
 * 这不是为了迁就测试才拆的 —— 纯逻辑与出网本来就该分开：
 * 这个文件里的函数全是"输入 → 输出"，可以逐条断言；
 * 而 models-api.ts 只做 HTTP。混在一起时，测一个校验函数要先拉起整个请求层。
 *
 * 组件里剩下的就是"把状态摆到界面上"，没有判断逻辑 —— 这正是让
 * 组件测试可以省掉的前提。
 */

import type { ModelSavePayload } from './types'

/** 供应商预设：点一下就填好地址与模型名。 */
export interface ProviderPreset {
  id: string
  label: string
  baseUrl: string
  model: string
  /** 密钥输入框的提示语（本地模型不需要真密钥，但协议要求非空） */
  keyHint: string
  note: string
}

export const PROVIDERS: ProviderPreset[] = [
  {
    id: 'deepseek',
    label: 'DeepSeek',
    baseUrl: 'https://api.deepseek.com/v1',
    model: 'deepseek-chat',
    keyHint: 'sk-…（platform.deepseek.com 申请）',
    note: '国内直连、价格低，默认推荐',
  },
  {
    id: 'openai',
    label: 'OpenAI',
    baseUrl: 'https://api.openai.com/v1',
    model: 'gpt-4o-mini',
    keyHint: 'sk-…',
    note: '需要能访问 openai.com',
  },
  {
    id: 'moonshot',
    label: '月之暗面 Kimi',
    baseUrl: 'https://api.moonshot.cn/v1',
    model: 'moonshot-v1-8k',
    keyHint: 'sk-…',
    note: '长上下文',
  },
  {
    id: 'dashscope',
    label: '阿里通义',
    baseUrl: 'https://dashscope.aliyuncs.com/compatible-mode/v1',
    model: 'qwen-plus',
    keyHint: 'sk-…（DashScope 控制台）',
    note: 'OpenAI 兼容模式',
  },
  {
    id: 'ollama',
    label: 'Ollama（本地）',
    baseUrl: 'http://127.0.0.1:11434/v1',
    model: 'qwen2.5:7b',
    keyHint: '随便填一个非空值即可（本地不校验）',
    note: '完全离线，不产生费用',
  },
  {
    id: 'custom',
    label: '自定义',
    baseUrl: '',
    model: '',
    keyHint: '按你的网关要求填写',
    note: '任何 OpenAI 兼容端点',
  },
]

export function presetFor(id: string): ProviderPreset {
  // storage 里可能留着旧版本的 provider 值，认不出就退回"自定义"，
  // 而不是抛异常让整页崩掉
  return PROVIDERS.find((p) => p.id === id) ?? PROVIDERS[PROVIDERS.length - 1]!
}

/**
 * 反查"这个地址像哪家的"。
 *
 * 用途是**显示**：给清单里的每条模型一个来源标签。判断"是不是同一个模型"
 * 从来不靠这个（那靠地址+模型名+密钥的比对），所以这里只做前缀匹配即可 ——
 * 猜错了最多是标签不好看，不会影响任何行为。
 */
export function presetIdForUrl(url: string): string {
  const normalized = url.trim().toLowerCase().replace(/\/+$/, '')
  if (!normalized) return 'custom'
  for (const p of PROVIDERS) {
    if (p.id === 'custom' || !p.baseUrl) continue
    if (normalized === p.baseUrl.toLowerCase().replace(/\/+$/, '')) return p.id
  }
  // 认不出具体预设时，按域名特征给一个更贴切的兜底标签
  if (normalized.includes('localhost') || normalized.includes('127.0.0.1')) return 'ollama'
  return 'custom'
}

/** 表单草稿。与后端字段一一对应（`id` 为空表示新增）。 */
export interface ModelDraft {
  id: string
  label: string
  provider: string
  baseUrl: string
  model: string
  apiKey: string
  /** 温度。**按模型存**：本地小模型要 0.7 才好用，而工具调用场景要 ≤0.3 */
  temperature: number
  /** 编辑已有条目且用户没动密钥栏 —— 此时密钥应保持原样 */
  keyUnchanged: boolean
}

export function emptyDraft(provider = 'deepseek'): ModelDraft {
  const preset = presetFor(provider)
  return {
    id: '',
    label: preset.label,
    provider: preset.id,
    baseUrl: preset.baseUrl,
    model: preset.model,
    apiKey: '',
    temperature: 0.3,
    keyUnchanged: false,
  }
}

export function draftFromModel(model: {
  id: string
  label: string
  provider: string
  base_url: string
  model: string
  temperature?: number | null
}): ModelDraft {
  return {
    id: model.id,
    label: model.label,
    provider: model.provider,
    baseUrl: model.base_url,
    model: model.model,
    apiKey: '',
    // 没存过温度的条目给一个保守默认值（与 LLM_TEMPERATURE 的默认一致）
    temperature: model.temperature ?? 0.3,
    keyUnchanged: true,
  }
}

export type DraftErrors = Partial<Record<'label' | 'baseUrl' | 'model' | 'apiKey', string>>

/**
 * 校验草稿。**每种错误给一句能照做的话**，而不是"格式不正确"。
 *
 * 【为什么密钥在"编辑且未改动"时允许为空】
 * 界面拿到的密钥只有掩码。用户只改模型名时密钥栏是空的，此时判"必填"
 * 会逼他重新找回密钥（可能当初是别人配的）。后端对"留空"的语义是"不改动"，
 * 前端校验必须与之一致，否则会出现"改个名字要先找回密钥"这种荒唐要求。
 */
export function validateModelDraft(draft: ModelDraft): DraftErrors {
  const errors: DraftErrors = {}
  const label = draft.label.trim()
  const baseUrl = draft.baseUrl.trim()
  const model = draft.model.trim()

  if (!label) errors.label = '给它起个名字，列表里靠它区分'
  if (!baseUrl) {
    errors.baseUrl = '填服务地址，例如 https://api.deepseek.com/v1'
  } else if (!/^https?:\/\//i.test(baseUrl)) {
    errors.baseUrl = '地址要以 http:// 或 https:// 开头'
  }
  if (!model) errors.model = '填模型名，例如 deepseek-chat'
  if (!draft.apiKey.trim() && !draft.keyUnchanged) {
    errors.apiKey = '填密钥；本地模型随便填一个非空值'
  }
  return errors
}

export function canSubmitDraft(draft: ModelDraft): boolean {
  return Object.keys(validateModelDraft(draft)).length === 0
}

/**
 * 草稿 → 请求体。
 *
 * `apiKey` 为空时**不放进请求体**（而不是传空字符串）：两者后端都当作
 * "不改动"，但不传更准确地表达"我没碰这个字段" —— 万一后端哪天的语义变了，
 * 传空字符串就会变成"改个名字把密钥清掉"。
 */
export function draftToPayload(draft: ModelDraft): ModelSavePayload {
  const payload: ModelSavePayload = {
    label: draft.label.trim(),
    provider: draft.provider,
    base_url: draft.baseUrl.trim(),
    model: draft.model.trim(),
    temperature: draft.temperature,
  }
  if (draft.id) payload.id = draft.id
  const key = draft.apiKey.trim()
  if (key) payload.api_key = key
  return payload
}

/** 列表里显示的一行文案：`deepseek-chat · api.deepseek.com`。 */
export function describeModel(model: { model: string; base_url: string }): string {
  let host = model.base_url
  try {
    host = new URL(model.base_url).host
  } catch {
    /* 地址不合法（data/models.json 是用户可以手改的）就原样显示 */
  }
  return `${model.model} · ${host}`
}
