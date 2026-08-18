import { apiAdminGet, apiAdminPost } from './base'

const BASE_URL = '/api/feishu-knowledge'
const MAX_BATCH_SIZE = 100

const ERROR_MESSAGES = {
  'A scan is already running for this source': '该数据源已有扫描任务正在执行',
  'Feishu source not found': '未找到飞书数据源',
  'Feishu sync run not found': '未找到扫描批次',
  'Feishu material not found': '未找到该素材',
  'Only pending parsed material can be approved': '仅待审核且已解析的素材可以审核通过',
  'Reject reason is required': '驳回时必须填写原因',
  'Only pending material can be rejected': '仅待审核的素材可以驳回',
  'Only failed material can be retried': '仅处理失败的素材可以重试',
  'Material is not queued for publishing': '素材当前不在等待发布状态',
  'Material is not queued for processing': '素材当前不在等待加工状态',
  'Material is not processing': '素材当前不在加工中',
  'Material is not publishing': '素材当前不在发布中',
  'Material is not the active published version': '素材不是当前生效的已发布版本',
  'Material removal is no longer pending': '素材下架任务已不再等待处理',
  'Material source must be invalid before removal': '仅来源失效的素材可以确认下架',
  'reason is required for reject': '驳回时必须填写原因',
  'updated_from must not be later than updated_to': '更新时间起点不能晚于终点',
  FEISHU_SPACE_PERMISSION_DENIED: '当前应用没有读取整个知识空间的权限，请在飞书开放平台开通后重试',
  FEISHU_USER_AUTHORIZATION_REQUIRED: '请先授权一名有权限的飞书用户',
  FEISHU_USER_REAUTHORIZATION_REQUIRED: '飞书用户授权已失效，请重新授权',
  FEISHU_USER_SOURCE_PERMISSION_DENIED: '当前飞书用户没有读取该知识空间的权限',
  FEISHU_USER_OAUTH_NOT_CONFIGURED: '飞书用户授权回调尚未配置',
  FEISHU_USER_QR_OAUTH_NOT_CONFIGURED: '飞书扫码授权回调尚未配置',
  FEISHU_OAUTH_ENCRYPTION_NOT_CONFIGURED: '飞书用户授权令牌加密尚未配置',
  FEISHU_OAUTH_STATE_INVALID: '飞书授权请求已过期，请重新发起',
  FEISHU_USER_OAUTH_FAILED: '飞书用户授权失败，请重新尝试',
  FEISHU_USER_TOKEN_REFRESH_FAILED: '飞书用户授权已失效，请重新授权'
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
  if (detail && typeof detail === 'object')
    return detail.code || detail.message || detail.error || ''
  return error?.message || ''
}

export const feishuKnowledgeApi = {
  listSources: () => apiAdminGet(`${BASE_URL}/sources`),

  checkSource: (sourceId) => apiAdminPost(`${BASE_URL}/sources/${encoded(sourceId)}/check`, {}),

  listTree: (sourceId) => apiAdminGet(`${BASE_URL}/sources/${encoded(sourceId)}/tree`),

  getOAuthStatus: (sourceId) =>
    apiAdminGet(`${BASE_URL}/sources/${encoded(sourceId)}/oauth/status`),

  startOAuth: (sourceId, mode = 'redirect') =>
    apiAdminPost(`${BASE_URL}/sources/${encoded(sourceId)}/oauth/authorize`, { mode }),

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
    if (ERROR_MESSAGES[detail]) return ERROR_MESSAGES[detail]
    return /[\u3400-\u9fff]/.test(detail) ? `${fallback}：${detail}` : fallback
  }
}

export { MAX_BATCH_SIZE }
