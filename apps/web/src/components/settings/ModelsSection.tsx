/**
 * 模型：供应商清单 + 添加/切换/删除。
 *
 * ============================================================
 * 这一页要回答的两个问题
 * ============================================================
 *  1. **现在在用哪一个？** —— 每条卡片上有"当前使用"徽标。它不是本地记住的
 *     一个 id，而是后端**算出来**的（比对地址+模型名+密钥）。所以手改过 `.env`
 *     之后这里的显示依然是对的。
 *  2. **怎么再添一个？** —— 六个预设（含"自定义"）+ 一张表单。
 *     预设的意义是"用户不用去查文档"，而不是限制他能填什么。
 *
 * ============================================================
 * 两个刻意的交互决定
 * ============================================================
 * · **切换是单击，删除要确认。** 两者的代价不对称：切错了再点回去就行
 *   （而且切换是立即生效、可逆的）；删掉一条要重填地址与密钥，而密钥
 *   在界面上只有掩码 —— 用户未必找得回来。
 * · **编辑时密钥栏留空 = 不改动**，与后端语义一致。占位符显示掩码，
 *   让用户知道"这里已经有一把了"。
 */

import { useEffect, useRef, useState } from 'react'

import { useModels } from '../../hooks/useModels'
import {
  PROVIDERS,
  type ModelDraft,
  canSubmitDraft,
  describeModel,
  draftFromModel,
  draftToPayload,
  emptyDraft,
  presetFor,
  presetIdForUrl,
  validateModelDraft,
} from '../../lib/models-view'
import { IconCheck, IconPlus, IconTrash } from '../Icons'

export interface ModelsSectionProps {
  onChanged?: () => void
}

export function ModelsSection({ onChanged }: ModelsSectionProps) {
  const models = useModels()
  const [draft, setDraft] = useState<ModelDraft | null>(null)
  const [confirmDelete, setConfirmDelete] = useState<string | null>(null)
  const [touched, setTouched] = useState(false)
  const [importName, setImportName] = useState('')
  const [showImport, setShowImport] = useState(false)

  // 挂载时拉一次清单。
  //
  // 【为什么用 ref 而不是 `if (data === null) load()` 直接写在渲染体里】
  // 那样写看着更短，但它是**在渲染期间发副作用**：`load()` 会同步 setLoading，
  // 于是 React 在渲染一个组件的过程中更新另一个组件的状态，
  // 轻则警告、重则"渲染 → 状态变化 → 再渲染"地打转。
  // 一次性的副作用就该放在 effect 里，并用 ref 保证只跑一次。
  //
  // 只在**这一页**挂载时拉，而不是对话框一打开就拉：用户可能只想改主题。
  const loaded = useRef(false)
  useEffect(() => {
    if (loaded.current) return
    loaded.current = true
    void models.load()
  }, [models.load])

  const errors = draft ? validateModelDraft(draft) : {}
  const ready = draft ? canSubmitDraft(draft) : false

  const startAdd = (providerId: string): void => {
    setTouched(false)
    setDraft(emptyDraft(providerId))
  }

  const submit = async (): Promise<void> => {
    if (!draft) return
    setTouched(true)
    if (!canSubmitDraft(draft)) return
    if (await models.save(draftToPayload(draft))) {
      setDraft(null)
      onChanged?.()
    }
  }

  const activate = async (id: string): Promise<void> => {
    if (await models.activate(id)) onChanged?.()
  }

  return (
    <>
      {models.error && (
        <div className="settings__alert settings__alert--bad" role="alert">
          <p>{models.error}</p>
          <button type="button" className="btn" disabled={models.loading} onClick={() => void models.load()}>
            重新读取模型
          </button>
        </div>
      )}
      {models.notice && <div className="settings__alert settings__alert--ok" role="status">{models.notice}</div>}

      {/* ---------- 当前生效的配置 ---------- */}
      {models.data && (
        <section className="settings__group">
          <h3 className="settings__legend">正在使用</h3>
          <div className="model-current">
            <div>
              <div className="model-current__name">{models.data.current.model || '尚未配置模型'}</div>
              <div className="model-current__url">{models.data.current.base_url}</div>
            </div>
            <span className={`badge${models.data.current.api_key_set ? ' badge--ok' : ' badge--warn'}`}>
              {models.data.current.api_key_set ? '密钥已配置' : '密钥未配置'}
            </span>
          </div>
          {models.data.current_unsaved && (
            <div className="settings__alert settings__hint--warn">
              当前配置尚未保存到模型清单。保存后可随时切回。
              <div className="settings__actions">
                {!showImport ? (
                  <button type="button" className="btn" onClick={() => setShowImport(true)}>
                    存为模型
                  </button>
                ) : (
                  <>
                    <input
                      className="settings__input"
                      aria-label="当前模型名称"
                      placeholder="例如 日常使用"
                      value={importName}
                      onChange={(e) => setImportName(e.target.value)}
                      onKeyDown={(e) => {
                        if (e.key === 'Enter' && !e.nativeEvent.isComposing && models.busyId === null) {
                          void models.importCurrent(importName)
                        }
                      }}
                    />
                    <button
                      type="button"
                      className="btn btn--primary"
                      disabled={models.busyId === 'import'}
                      onClick={() => void models.importCurrent(importName)}
                    >
                      {models.busyId === 'import' ? '保存中…' : '保存'}
                    </button>
                    <button type="button" className="btn btn--ghost" onClick={() => setShowImport(false)}>
                      取消
                    </button>
                  </>
                )}
              </div>
            </div>
          )}
        </section>
      )}

      {/* ---------- 清单 ---------- */}
      <section className="settings__group">
        <h3 className="settings__legend">
          已保存的模型{models.data ? `（${models.data.models.length}）` : ''}
        </h3>

        {models.loading && !models.data && <p className="settings__hint">读取中…</p>}
        {models.data?.models.length === 0 && (
          <p className="settings__hint settings__hint--block">还没有保存模型，选择下方供应商添加。</p>
        )}

        <ul className="model-list">
          {models.data?.models.map((m) => {
            const preset = presetFor(presetIdForUrl(m.base_url))
            return (
              <li key={m.id} className={`model-card${m.active ? ' model-card--active' : ''}`}>
                <div className="model-card__main">
                  <div className="model-card__title">
                    <span className="model-card__label">{m.label}</span>
                    <span className="model-card__provider">{preset.label}</span>
                    {m.active && (
                      <span className="badge badge--ok">
                        <IconCheck size={12} /> 当前使用
                      </span>
                    )}
                    {!m.api_key_set && <span className="badge badge--warn">缺密钥</span>}
                  </div>
                  <div className="model-card__desc">{describeModel(m)}</div>
                </div>

                <div className="model-card__actions">
                  {!m.active && (
                    <button
                      type="button"
                      className="btn"
                      disabled={models.busyId !== null}
                      onClick={() => void activate(m.id)}
                    >
                      {models.busyId === m.id ? '切换中…' : '切换'}
                    </button>
                  )}
                  <button
                    type="button"
                    className="btn btn--ghost"
                    disabled={models.busyId !== null}
                    onClick={() => {
                      setTouched(false)
                      setDraft(draftFromModel(m))
                    }}
                  >
                    编辑
                  </button>
                  {confirmDelete === m.id ? (
                    <>
                      <button
                        type="button"
                        className="btn btn--danger"
                        disabled={models.busyId !== null}
                        onClick={async () => {
                          if (await models.remove(m.id)) {
                            setConfirmDelete(null)
                            onChanged?.()
                          }
                        }}
                      >
                        确认删除
                      </button>
                      <button type="button" className="btn btn--ghost" onClick={() => setConfirmDelete(null)}>
                        取消
                      </button>
                    </>
                  ) : (
                    <button
                      type="button"
                      className="iconbtn"
                      disabled={models.busyId !== null}
                      title="删除"
                      aria-label={`删除 ${m.label}`}
                      onClick={() => setConfirmDelete(m.id)}
                    >
                      <IconTrash size={16} />
                    </button>
                  )}
                </div>
              </li>
            )
          })}
        </ul>
      </section>

      {/* ---------- 添加/编辑表单 ---------- */}
      <section className="settings__group">
        <h3 className="settings__legend">{draft?.id ? '编辑模型' : '添加供应商'}</h3>

        {!draft && (
          <div className="provider-grid">
            {PROVIDERS.map((p) => (
              <button
                key={p.id}
                type="button"
                className="provider-card"
                onClick={() => startAdd(p.id)}
                title={p.note}
              >
                <span className="provider-card__label">
                  {p.id === 'custom' ? <IconPlus size={14} /> : null}
                  {p.label}
                </span>
                <span className="provider-card__note">{p.note}</span>
              </button>
            ))}
          </div>
        )}

        {draft && (
          <div className="model-form">
            <label className="settings__field">
              <span className="settings__label">显示名称</span>
              <input
                className="settings__input"
                value={draft.label}
                onChange={(e) => setDraft({ ...draft, label: e.target.value })}
                placeholder="例如 日常 / 难题 / 公司网关"
              />
              {touched && errors.label && <span className="settings__hint settings__hint--warn">{errors.label}</span>}
            </label>

            <label className="settings__field">
              <span className="settings__label">服务地址</span>
              <input
                className="settings__input"
                value={draft.baseUrl}
                onChange={(e) => setDraft({ ...draft, baseUrl: e.target.value })}
                placeholder="https://api.deepseek.com/v1"
                spellCheck={false}
              />
              {touched && errors.baseUrl && (
                <span className="settings__hint settings__hint--warn">{errors.baseUrl}</span>
              )}
            </label>

            <label className="settings__field">
              <span className="settings__label">模型名</span>
              <input
                className="settings__input"
                value={draft.model}
                onChange={(e) => setDraft({ ...draft, model: e.target.value })}
                placeholder="deepseek-chat"
                spellCheck={false}
              />
              {touched && errors.model && <span className="settings__hint settings__hint--warn">{errors.model}</span>}
            </label>

            <label className="settings__field">
              <span className="settings__label">API Key</span>
              <input
                className="settings__input"
                type="password"
                value={draft.apiKey}
                onChange={(e) => setDraft({ ...draft, apiKey: e.target.value })}
                placeholder={draft.keyUnchanged ? '留空 = 不改动已保存的密钥' : presetFor(draft.provider).keyHint}
                autoComplete="off"
              />
              {touched && errors.apiKey && (
                <span className="settings__hint settings__hint--warn">{errors.apiKey}</span>
              )}
            </label>

            <label className="settings__field settings__field--inline">
              <span className="settings__label">温度（{draft.temperature}）</span>
              <input
                type="range"
                min={0}
                max={2}
                step={0.1}
                value={draft.temperature}
                onChange={(e) => setDraft({ ...draft, temperature: Number(e.target.value) })}
              />
              <span className="settings__hint">
                较低值让输出更稳定，较高值增加表达变化。保存后切换模型才会生效。
              </span>
            </label>

            <div className="settings__actions">
              <button
                type="button"
                className="btn btn--primary"
                disabled={models.busyId !== null || (touched && !ready)}
                onClick={() => void submit()}
              >
                {models.busyId !== null ? '保存中…' : draft.id ? '保存修改' : '添加模型'}
              </button>
              <button type="button" className="btn btn--ghost" onClick={() => setDraft(null)}>
                取消
              </button>
              <span className="settings__hint">
                保存到清单后，点击「切换」启用；不会自动替换当前模型。
              </span>
            </div>
          </div>
        )}
      </section>
    </>
  )
}
