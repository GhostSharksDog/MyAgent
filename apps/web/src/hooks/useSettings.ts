/**
 * 设置面板的状态管理。
 *
 * ============================================================
 * 这里有两个不那么显然的设计决定
 * ============================================================
 *
 * 1. **密钥字段的初值是空字符串，不是掩码。**
 *    后端 GET 返回的是掩码（`sk-e******35e7`）。如果直接把它塞进输入框，
 *    用户不改密钥、只改模型名，提交时就会把掩码原样发回去 ——
 *    后端虽然做了防御（认出掩码等于"不改动"），但**界面不该依赖后端
 *    兜住自己的错误**。所以输入框留空，placeholder 显示掩码，
 *    含义很直白："留空 = 不改动"。
 *
 *    这一条值得单独写出来，因为它踩过一次：最初的版本直接填掩码，
 *    用户一保存密钥就变成 `sk-e******35e7`，之后所有请求 401，
 *    而设置页面显示"密钥已配置" —— **配置看着正常，功能全挂**。
 *
 * 2. **保存成功后要重新拉一次，而不是拿响应更新本地状态。**
 *    因为"保存"的副作用不止是配置值：后端会**重建检索索引**、
 *    清设置缓存，语料文档数也会变。前端只看响应体的话，
 *    看不到"知识库现在有几篇文档"这类派生信息 ——
 *    而那恰恰是用户最想确认的"我的改动生效了吗"。
 */

import { useCallback, useState } from 'react'

import { fetchSettings, testLLMConnection, updateSettings } from '../lib/settings-api'
import type { SettingsUpdatePayload, SettingsView, TestConnectionResult } from '../lib/types'

export interface SettingsState {
  /** 当前已保存的配置（来自后端）。null 表示还没加载。 */
  saved: SettingsView | null
  loading: boolean
  saving: boolean
  error: string | null
  /** 保存成功的一次性提示（下次操作前自动清掉） */
  notice: string | null
  testing: boolean
  testResult: TestConnectionResult | null
}

export function useSettings() {
  const [saved, setSaved] = useState<SettingsView | null>(null)
  const [loading, setLoading] = useState(false)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [testing, setTesting] = useState(false)
  const [testResult, setTestResult] = useState<TestConnectionResult | null>(null)

  const load = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      setSaved(await fetchSettings())
    } catch (err) {
      // 把后端的中文原因原样带出来 —— 设置接口为每种失败都写了
      // 具体提示（"工作区目录不存在"），前端自己编一句就丢掉了
      setError(err instanceof Error ? err.message : '读取设置失败')
    } finally {
      setLoading(false)
    }
  }, [])

  const save = useCallback(async (payload: SettingsUpdatePayload) => {
    setSaving(true)
    setError(null)
    setNotice(null)
    try {
      await updateSettings(payload)
      // 重新拉而不是用响应 —— 见文件头的第 2 条
      setSaved(await fetchSettings())
      setNotice('已保存并立即生效，无需重启')
      return true
    } catch (err) {
      setError(err instanceof Error ? err.message : '保存失败')
      return false
    } finally {
      setSaving(false)
    }
  }, [])

  const test = useCallback(async () => {
    setTesting(true)
    setTestResult(null)
    try {
      setTestResult(await testLLMConnection())
    } catch (err) {
      setTestResult({
        ok: false,
        model: '',
        latency_ms: 0,
        error: err instanceof Error ? err.message : '测试请求失败',
        hint: '检查后端是否在运行。',
      })
    } finally {
      setTesting(false)
    }
  }, [])

  const clearMessages = useCallback(() => {
    setError(null)
    setNotice(null)
    setTestResult(null)
  }, [])

  return { saved, loading, saving, error, notice, testing, testResult, load, save, test, clearMessages }
}
