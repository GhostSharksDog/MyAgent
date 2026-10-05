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

import { useState } from 'react'

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

export function ModelsSection() {
  const models = useModels()
  const [draft, setDraft] = useState<ModelDraft | null>(null)
  const [confirmDelete, setConfirmDelete] = useState<string | null>(null)
  const [touched, setTouched] = useState(false)
  const [importName, setImportName] = useState('')
  const [showImport, setShowImport] = useState(false)

  // 首次展开这一页时拉一次清单（不放在对话框打开时拉：用户可能只想改主题）
  if (models.data === null && !models.loading && models.error === null) {
    void models.load()
  }

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
    if (await models.save(draftToPayload(draft))) setDraft(null)
  }

  return (
    <>
      {models.error && <div className="settings__alert settings__alert--bad">{models.error}</div>}
      {models.notice && <div className="settings__alert settings__alert--ok">{models.notice}</div>}

      {/* ---------- 当前生效的配置 ---------- */}
      {models.data && (
        <section className="settings__group">
          <h3 className="settings__legend">正在使用</h3>
          <div className="model-current">
            <div>
              <div className="model-current__name">{models.data.current.model}</div>
              <div className="model-current__url">{models.data.current.base_url}</div>
            </div>
            <span className={`badge${models.data.current.api_key_set ? ' badge--ok' : ' badge--warn'}`}>
              {models.data.current.api_key_set ? '密钥已配置' : '没有密钥'}
            </span>
          </div>
          {models.data.current_unsaved && (
            <div className="settings__alert settings__hint--warn">
              这份配置还没存进清单。存起来之后就能一键切回它，也能存别的供应商。
              <div className="settings__actions">
                {!showImport ? (
                  <button type="button" className="btn" onClick={() => setShowImport(true)}>
                    存为模型
                  </button>
                ) : (
                  <>
                    <input
                      className="settings__input"
                      placeholder="给它起个名字，例如 我一直在用的"
                      value={importName}
                      onChange={(e) => setImportName(e.target.value)}
                      onKeyDown={(e) => {
                        if (e.key === 'Enter') void models.importCurrent(importName)
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
          <p className="settings__hint settings__hint--block">
            还没有保存任何模型。下面挑一个供应商加进来 —— 之后换模型就是一次点击的事。
          </p>
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
                      disabled={models.busyId === m.id}
                      onClick={() => void models.activate(m.id)}
                    >
                      {models.busyId === m.id ? '切换中…' : '切换'}
                    </button>
                  )}
                  <button
                    type="button"
                    className="btn btn--ghost"
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
                        disabled={models.busyId === m.id}
                        onClick={async () => {
                          await models.remove(m.id)
                          setConfirmDelete(null)
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
              <span className="settings__label">名称（列表里靠它区分）</span>
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
                按模型存：本地小模型常要 0.7，而工具调用场景建议 ≤ 0.3
              </span>
            </label>

            <div className="settings__actions">
              <button
                type="button"
                className="btn btn--primary"
                disabled={touched && !ready}
                onClick={() => void submit()}
              >
                {draft.id ? '保存修改' : '添加'}
              </button>
              <button type="button" className="btn btn--ghost" onClick={() => setDraft(null)}>
                取消
              </button>
              <span className="settings__hint">
                添加后还要点一次「切换」才会真正用它 —— 免得填到一半就把正在用的换掉了。
              </span>
            </div>
          </div>
        )}
      </section>
    </>
  )
}
