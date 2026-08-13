import { apiAdminGet, apiAdminPost } from './base'

const BASE_URL = '/api/feishu-knowledge'
const MAX_BATCH_SIZE = 100

const ERROR_MESSAGES = {
  'A scan is already running for this source': '该数据源已有扫描任务正在执行',
  'Feishu source not found': '未找到飞书数据源',
  'Feishu sync run not found': '未找到扫描批次',
  'Feishu material not found': '未找到该素材',
  'reason is required for reject': '驳回时必须填写原因',
  'updated_from must not be later than updated_to': '更新时间起点不能晚于终点'
}

function encoded(value) {
  return encodeURIComponent(value)
}

function withQuery(url, params = {}) {
  const query = new URLSearchParams()
  Object.entries(params).forEach(([key, value]) => {
    if (value !== '' && value !== null && value !== undefined) {
      query.set(key, value)
    }
  })
  const queryString = query.toString()
  return queryString ? `${url}?${queryString}` : url
}

function getErrorDetail(error) {
  const detail = error?.response?.data?.detail
  if (typeof detail === 'string') return detail
  if (detail && typeof detail === 'object') return detail.message || detail.error || ''
  return error?.message || ''
}

export const feishuKnowledgeApi = {
  listSources: () => apiAdminGet(`${BASE_URL}/sources`),

  checkSource: (sourceId) => apiAdminPost(`${BASE_URL}/sources/${encoded(sourceId)}/check`, {}),

  scanSource: (sourceId, mode) =>
    apiAdminPost(`${BASE_URL}/sources/${encoded(sourceId)}/scan`, { mode }),

  listRuns: (sourceId) => apiAdminGet(`${BASE_URL}/sources/${encoded(sourceId)}/runs`),

  getRun: (runId) => apiAdminGet(`${BASE_URL}/runs/${encoded(runId)}`),

  listMaterials: (sourceId, params = {}) =>
    apiAdminGet(withQuery(`${BASE_URL}/sources/${encoded(sourceId)}/materials`, params)),

  getMaterial: (versionId) => apiAdminGet(`${BASE_URL}/materials/${encoded(versionId)}`),

  listMaterialEvents: (versionId) =>
    apiAdminGet(`${BASE_URL}/materials/${encoded(versionId)}/events`),

  approveMaterial: (versionId) =>
    apiAdminPost(`${BASE_URL}/materials/${encoded(versionId)}/approve`, {}),

  rejectMaterial: (versionId, reason) =>
    apiAdminPost(`${BASE_URL}/materials/${encoded(versionId)}/reject`, { reason }),

  retryMaterial: (versionId) =>
    apiAdminPost(`${BASE_URL}/materials/${encoded(versionId)}/retry`, {}),

  confirmRemoval: (versionId) =>
    apiAdminPost(`${BASE_URL}/materials/${encoded(versionId)}/confirm-removal`, {}),

  batchAction: (action, versionIds, reason) => {
    if (versionIds.length > MAX_BATCH_SIZE) {
      return Promise.reject(new Error(`单次批量操作最多选择 ${MAX_BATCH_SIZE} 条素材`))
    }
    return apiAdminPost(`${BASE_URL}/materials/batch-action`, {
      action,
      version_ids: versionIds,
      ...(reason ? { reason } : {})
    })
  },

  getErrorMessage: (error, fallback = '操作失败') => {
    const detail = getErrorDetail(error)
    if (!detail) return fallback
    return ERROR_MESSAGES[detail] || `${fallback}：${detail}`
  }
}

export { MAX_BATCH_SIZE }
