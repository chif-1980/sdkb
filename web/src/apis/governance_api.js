import { apiAdminGet, apiAdminPost } from './base'

const BASE_URL = '/api/governance'

function encoded(value) {
  return encodeURIComponent(value)
}

function withQuery(url, params = {}) {
  const query = new URLSearchParams()
  Object.entries(params).forEach(([key, value]) => {
    if (value !== '' && value !== null && value !== undefined) query.set(key, value)
  })
  const queryString = query.toString()
  return queryString ? `${url}?${queryString}` : url
}

function errorMessage(error, fallback) {
  const detail = error?.response?.data?.detail
  if (typeof detail === 'string' && /[\u3400-\u9fff]/.test(detail)) return detail
  const messages = {
    'Review task is already completed': '该审核任务已经完成',
    'Review task is assigned to another reviewer': '该任务已转交给其他审核人',
    'Assignee is not an active knowledge administrator': '请选择有效的知识管理员',
    'publish requires CREATE, UPDATE or SPLIT_BY_SCOPE action': '当前处理方式不能直接发布'
  }
  return messages[detail] || fallback
}

export const governanceApi = {
  listReviewers: () => apiAdminGet(`${BASE_URL}/reviewers`),

  listReviews: (sourceId, params = {}) =>
    apiAdminGet(withQuery(`${BASE_URL}/reviews`, { source_id: sourceId, ...params })),

  getReview: (reviewId) => apiAdminGet(`${BASE_URL}/reviews/${encoded(reviewId)}`),

  listReviewComparisons: (reviewId) =>
    apiAdminGet(`${BASE_URL}/reviews/${encoded(reviewId)}/comparisons`),

  resolveReview: (reviewId, payload) =>
    apiAdminPost(`${BASE_URL}/reviews/${encoded(reviewId)}/resolve`, payload),

  listRelations: (sourceId, params = {}) =>
    apiAdminGet(withQuery(`${BASE_URL}/relations`, { source_id: sourceId, ...params })),

  listFormalKnowledge: (sourceId) =>
    apiAdminGet(withQuery(`${BASE_URL}/knowledge`, { source_id: sourceId })),

  listKnowledgeRelations: (knowledgeId) =>
    apiAdminGet(`${BASE_URL}/knowledge/${encoded(knowledgeId)}/relations`),

  listKnowledgeVersions: (knowledgeId) =>
    apiAdminGet(`${BASE_URL}/knowledge/${encoded(knowledgeId)}/versions`),

  getErrorMessage: errorMessage
}
