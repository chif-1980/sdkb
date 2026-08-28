import { apiAdminGet, apiAdminPatch, apiAdminPost } from './base'

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
    'Review package is assigned to another reviewer': '该审核包已转交给其他审核人',
    'Review package is already completed or invalidated': '该审核包已经完成或失效',
    'Only open or newly received source-change requests can be cancelled':
      '该资料修改任务已结束，不能重复取消',
    'Only excluded knowledge units can be reopened': '只有已标记为不纳入的知识单元可以重新申请',
    'Knowledge unit is no longer excluded': '该知识单元当前已不是不纳入状态，请刷新后重试',
    'Assignee is not an active knowledge administrator': '请选择有效的知识管理员',
    'publish requires CREATE, UPDATE or SPLIT_BY_SCOPE action': '当前处理方式不能直接发布'
  }
  return messages[detail] || fallback
}

export const governanceApi = {
  listReviewers: () => apiAdminGet(`${BASE_URL}/reviewers`),

  listReviewPackages: (sourceId, params = {}) =>
    apiAdminGet(withQuery(`${BASE_URL}/review-packages`, { source_id: sourceId, ...params })),

  getReviewPackage: (packageId) => apiAdminGet(`${BASE_URL}/review-packages/${encoded(packageId)}`),

  listReviewPackageSegments: (packageId) =>
    apiAdminGet(`${BASE_URL}/review-packages/${encoded(packageId)}/segments`),

  getReviewPackagePresentation: (packageId) =>
    apiAdminGet(`${BASE_URL}/review-packages/${encoded(packageId)}/presentation`),

  getReviewPackageSlidePreview: (packageId, slideNumber) =>
    apiAdminGet(
      `${BASE_URL}/review-packages/${encoded(packageId)}/presentation/slides/${encoded(slideNumber)}`,
      {},
      'blob'
    ),

  getReviewPackageLayout: (packageId) =>
    apiAdminGet(`${BASE_URL}/review-packages/${encoded(packageId)}/layout`),

  getReviewPackageLayoutPage: (packageId, pageNumber) =>
    apiAdminGet(
      `${BASE_URL}/review-packages/${encoded(packageId)}/layout/pages/${encoded(pageNumber)}`,
      {},
      'blob'
    ),

  saveReviewPackageLayoutEdit: (packageId, payload) =>
    apiAdminPatch(`${BASE_URL}/review-packages/${encoded(packageId)}/layout/edits`, payload),

  saveReviewPackageDraft: (packageId, payload) =>
    apiAdminPatch(`${BASE_URL}/review-packages/${encoded(packageId)}/draft`, payload),

  resolveReviewPackage: (packageId, payload) =>
    apiAdminPost(`${BASE_URL}/review-packages/${encoded(packageId)}/resolve`, payload),

  bulkExcludeReviewPackage: (packageId, payload) =>
    apiAdminPost(`${BASE_URL}/review-packages/${encoded(packageId)}/bulk-exclude`, payload),

  reopenExcludedReviewItem: (reviewItemId) =>
    apiAdminPost(`${BASE_URL}/review-items/${encoded(reviewItemId)}/reopen-exclusion`, {}),

  transferReviewPackage: (packageId, payload) =>
    apiAdminPost(`${BASE_URL}/review-packages/${encoded(packageId)}/transfer`, payload),

  listSourceChangeRequests: (sourceId, params = {}) =>
    apiAdminGet(
      withQuery(`${BASE_URL}/source-change-requests`, { source_id: sourceId, ...params })
    ),

  getSourceChangeRequest: (changeRequestId) =>
    apiAdminGet(`${BASE_URL}/source-change-requests/${encoded(changeRequestId)}`),

  cancelSourceChangeRequest: (changeRequestId, reason) =>
    apiAdminPost(`${BASE_URL}/source-change-requests/${encoded(changeRequestId)}/cancel`, {
      reason
    }),

  listReviews: (sourceId, params = {}) =>
    apiAdminGet(withQuery(`${BASE_URL}/reviews`, { source_id: sourceId, ...params })),

  getReview: (reviewId) => apiAdminGet(`${BASE_URL}/reviews/${encoded(reviewId)}`),

  listReviewComparisons: (reviewId) =>
    apiAdminGet(`${BASE_URL}/reviews/${encoded(reviewId)}/comparisons`),

  resolveReview: (reviewId, payload) =>
    apiAdminPost(`${BASE_URL}/reviews/${encoded(reviewId)}/resolve`, payload),

  listRelations: (sourceId, params = {}) =>
    apiAdminGet(withQuery(`${BASE_URL}/relations`, { source_id: sourceId, ...params })),

  getDuplicateCandidates: (relationId) =>
    apiAdminGet(`${BASE_URL}/relations/${encoded(relationId)}/duplicate-candidates`),

  getRelationLayoutComparison: (relationId) =>
    apiAdminGet(`${BASE_URL}/relations/${encoded(relationId)}/layout-comparison`),

  getRelationLayoutComparisonPage: (relationId, side, pageNumber) =>
    apiAdminGet(
      `${BASE_URL}/relations/${encoded(relationId)}/layout-comparison/${encoded(side)}/pages/${encoded(pageNumber)}`,
      {},
      'blob'
    ),

  resolveDuplicateRelation: (relationId, payload) =>
    apiAdminPost(`${BASE_URL}/relations/${encoded(relationId)}/resolve-duplicate`, payload),

  getComparisonStatus: (sourceId) =>
    apiAdminGet(withQuery(`${BASE_URL}/comparisons/status`, { source_id: sourceId })),

  backfillComparisons: (sourceId) =>
    apiAdminPost(`${BASE_URL}/comparisons/backfill`, { source_id: sourceId }),

  listFormalKnowledge: (sourceId) =>
    apiAdminGet(withQuery(`${BASE_URL}/knowledge`, { source_id: sourceId })),

  listKnowledgeRelations: (knowledgeId) =>
    apiAdminGet(`${BASE_URL}/knowledge/${encoded(knowledgeId)}/relations`),

  listKnowledgeVersions: (knowledgeId) =>
    apiAdminGet(`${BASE_URL}/knowledge/${encoded(knowledgeId)}/versions`),

  getErrorMessage: errorMessage
}
