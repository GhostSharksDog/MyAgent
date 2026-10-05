/**
 * 模型清单的状态管理。
 *
 * ============================================================
 * 为什么每个动作都用**响应**更新状态，而不是重新拉一次
 * ============================================================
 * `useSettings` 的注释里写了相反的做法（保存后重新 GET），理由是"保存的副作用
 * 不止是配置值"（后端会重建索引，派生信息会变）。
 *
 * 模型接口不一样：每个写操作**都返回完整清单**（`ModelListResponse`），
 * 包括"哪一条是当前激活的"这个算出来的字段。再 GET 一次只会多一个来回，
 * 而且中间那一瞬的状态是旧的 —— 用户会看到"点了切换，徽标闪了一下才动"。
 *
 * 两处的做法不同，但依据是同一条：**响应对不对得上"用户的下一步动作"**。
 * 设置页的下一个动作是"确认生效了吗"（要看派生数据），
 * 模型列表的下一个动作是"看到它多出来/换过去了"（清单本身就是答案）。
 */

import { useCallback, useState } from 'react'

import {
  activateModel,
  deleteModel,
  fetchModels,
  importCurrentModel,
  saveModel,
} from '../lib/models-api'
import type { ModelListResponse, ModelSavePayload } from '../lib/types'

export function useModels() {
  const [data, setData] = useState<ModelListResponse | null>(null)
  const [loading, setLoading] = useState(false)
  const [busyId, setBusyId] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)

  const load = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      setData(await fetchModels())
    } catch (err) {
      // 后端的中文原因原样带出来（"没有 id 为 x 的模型"这类提示比"操作失败"有用）
      setError(err instanceof Error ? err.message : '读取模型清单失败')
    } finally {
      setLoading(false)
    }
  }, [])

  /** 所有写操作的公共外壳：清提示 → 执行 → 用响应替换清单 → 出错时记原因。 */
  const mutate = useCallback(
    async (
      action: () => Promise<ModelListResponse>,
      id: string | null,
      successNotice?: (result: ModelListResponse) => string,
    ): Promise<boolean> => {
      setBusyId(id)
      setError(null)
      setNotice(null)
      try {
        const result = await action()
        setData(result)
        if (successNotice) setNotice(successNotice(result))
        return true
      } catch (err) {
        setError(err instanceof Error ? err.message : '操作失败')
        return false
      } finally {
        setBusyId(null)
      }
    },
    [],
  )

  const save = useCallback(
    (payload: ModelSavePayload) =>
      mutate(() => saveModel(payload), payload.id ?? 'new', () => '已保存'),
    [mutate],
  )

  const remove = useCallback(
    (id: string) => mutate(() => deleteModel(id), id, () => '已删除'),
    [mutate],
  )

  const activate = useCallback(
    (id: string) =>
      mutate(
        () => activateModel(id),
        id,
        // 说清"立即生效"：这是本功能最容易让人怀疑的地方
        // （切换前它确实要重启才生效，见后端 app/api/models.py 的说明）
        (result) => {
          const current = result.models.find((m) => m.active)
          return current ? `已切换到 ${current.label}，立即生效` : '已切换，立即生效'
        },
      ),
    [mutate],
  )

  const importCurrent = useCallback(
    (label: string) =>
      mutate(() => importCurrentModel(label), 'import', () => '已保存为模型'),
    [mutate],
  )

  const clearMessages = useCallback(() => {
    setError(null)
    setNotice(null)
  }, [])

  return { data, loading, busyId, error, notice, load, save, remove, activate, importCurrent, clearMessages }
}
