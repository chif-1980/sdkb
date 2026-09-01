import { beforeEach, describe, expect, it, vi } from 'vitest'

import { apiAdminGet, apiAdminPatch, apiAdminPost } from './base'
import { governanceApi } from './governance_api'

vi.mock('./base', () => ({
  apiAdminGet: vi.fn(),
  apiAdminPatch: vi.fn(),
  apiAdminPost: vi.fn()
}))

describe('governanceApi', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('为审核包列表保留来源筛选并忽略空查询参数', () => {
    governanceApi.listReviewPackages('source/1', { view: 'mine', status: '' })

    expect(apiAdminGet).toHaveBeenCalledWith(
      '/api/governance/review-packages?source_id=source%2F1&view=mine'
    )
  })

  it('对审核包和资料修改任务 id 进行路径编码', () => {
    governanceApi.getReviewPackage('package/1')
    governanceApi.getSourceChangeRequest('change/1')

    expect(apiAdminGet).toHaveBeenNthCalledWith(1, '/api/governance/review-packages/package%2F1')
    expect(apiAdminGet).toHaveBeenNthCalledWith(
      2,
      '/api/governance/source-change-requests/change%2F1'
    )
  })

  it('读取 PPT 整页版式和带认证的页面预览', () => {
    governanceApi.getReviewPackagePresentation('package/1')
    governanceApi.getReviewPackageSlidePreview('package/1', 3)

    expect(apiAdminGet).toHaveBeenNthCalledWith(
      1,
      '/api/governance/review-packages/package%2F1/presentation'
    )
    expect(apiAdminGet).toHaveBeenNthCalledWith(
      2,
      '/api/governance/review-packages/package%2F1/presentation/slides/3',
      {},
      'blob'
    )
  })

  it('读取通用资料版式、页面预览并保存内容块草稿', () => {
    governanceApi.getReviewPackageLayout('package/1')
    governanceApi.getReviewPackageLayoutPage('package/1', 2)
    governanceApi.saveReviewPackageLayoutEdit('package/1', {
      lock_version: 3,
      block_id: 'page-1-block-1',
      page_number: 1,
      content: '修订后的正文'
    })

    expect(apiAdminGet).toHaveBeenNthCalledWith(
      1,
      '/api/governance/review-packages/package%2F1/layout'
    )
    expect(apiAdminGet).toHaveBeenNthCalledWith(
      2,
      '/api/governance/review-packages/package%2F1/layout/pages/2',
      {},
      'blob'
    )
    expect(apiAdminPatch).toHaveBeenCalledWith(
      '/api/governance/review-packages/package%2F1/layout/edits',
      expect.objectContaining({ block_id: 'page-1-block-1' })
    )
  })

  it('审核包草稿、裁决、批量不纳入、重新申请和转交使用对应方法及请求体', () => {
    const draft = { outcome: 'REQUEST_SOURCE_CHANGE', comment: '请补充版本信息' }
    const decision = { decision: 'REQUEST_CHANGES', action: 'MARK_INSUFFICIENT' }
    const bulkExclude = { review_item_ids: ['item-1'], decision_comment: '不纳入' }
    const transfer = { assignee_id: 'admin-2', comment: '转交产品负责人' }

    governanceApi.saveReviewPackageDraft('package-1', draft)
    governanceApi.resolveReviewPackage('package-1', decision)
    governanceApi.bulkExcludeReviewPackage('package-1', bulkExclude)
    governanceApi.reopenExcludedReviewItem('item/1')
    governanceApi.transferReviewPackage('package-1', transfer)

    expect(apiAdminPatch).toHaveBeenCalledWith(
      '/api/governance/review-packages/package-1/draft',
      draft
    )
    expect(apiAdminPost).toHaveBeenNthCalledWith(
      1,
      '/api/governance/review-packages/package-1/resolve',
      decision
    )
    expect(apiAdminPost).toHaveBeenNthCalledWith(
      2,
      '/api/governance/review-packages/package-1/bulk-exclude',
      bulkExclude
    )
    expect(apiAdminPost).toHaveBeenNthCalledWith(
      3,
      '/api/governance/review-items/item%2F1/reopen-exclusion',
      {}
    )
    expect(apiAdminPost).toHaveBeenNthCalledWith(
      4,
      '/api/governance/review-packages/package-1/transfer',
      transfer
    )
  })

  it('取消资料修改任务时发送原因', () => {
    governanceApi.cancelSourceChangeRequest('change-1', '源文档已由业务负责人修正')

    expect(apiAdminPost).toHaveBeenCalledWith(
      '/api/governance/source-change-requests/change-1/cancel',
      { reason: '源文档已由业务负责人修正' }
    )
  })

  it('读取并裁决片段级重复关系', () => {
    const payload = { request_id: 'request-1', strategy: 'USE_SOURCE' }

    governanceApi.getDuplicateCandidates('relation/1')
    governanceApi.resolveDuplicateRelation('relation/1', payload)

    expect(apiAdminGet).toHaveBeenCalledWith(
      '/api/governance/relations/relation%2F1/duplicate-candidates'
    )
    expect(apiAdminPost).toHaveBeenCalledWith(
      '/api/governance/relations/relation%2F1/resolve-duplicate',
      payload
    )
  })

  it('读取跨文档版式对比和页面预览', () => {
    governanceApi.getRelationLayoutComparison('relation/1')
    governanceApi.getRelationLayoutComparisonPage('relation/1', 'source', 2)
    governanceApi.getRelationLayoutComparisonPage('relation/1', 'target', 3, 2)

    expect(apiAdminGet).toHaveBeenNthCalledWith(
      1,
      '/api/governance/relations/relation%2F1/layout-comparison'
    )
    expect(apiAdminGet).toHaveBeenNthCalledWith(
      2,
      '/api/governance/relations/relation%2F1/layout-comparison/source/pages/2',
      {},
      'blob'
    )
    expect(apiAdminGet).toHaveBeenNthCalledWith(
      3,
      '/api/governance/relations/relation%2F1/layout-comparison/target/pages/3?density=2',
      {},
      'blob'
    )
  })

  it('正式知识治理接口编码标识并保留操作原因', () => {
    const metadata = {
      owner_id: 'admin-1',
      owner_name: '知识负责人',
      valid_from: '2026-08-01T00:00:00Z',
      valid_until: null,
      review_due_at: '2026-12-01T00:00:00Z'
    }

    governanceApi.updateKnowledgeUnitMetadata('unit/1', metadata)
    governanceApi.offlineKnowledgeUnit('unit/1', '内容已过时')
    governanceApi.restoreKnowledgeUnit('unit/1', '内容已确认有效')
    governanceApi.createKnowledgeRevision('unit/1', '原文需要修正')
    governanceApi.offlineKnowledgeSource('item/1', '整篇内容过时')
    governanceApi.restoreKnowledgeSource('item/1', '整篇重新启用')
    governanceApi.rollbackKnowledgeSource('item/1', 'version/2', '恢复稳定版本')

    expect(apiAdminPatch).toHaveBeenCalledWith(
      '/api/governance/knowledge-units/unit%2F1/metadata',
      metadata
    )
    expect(apiAdminPost).toHaveBeenNthCalledWith(
      1,
      '/api/governance/knowledge-units/unit%2F1/offline',
      { reason: '内容已过时' }
    )
    expect(apiAdminPost).toHaveBeenNthCalledWith(
      2,
      '/api/governance/knowledge-units/unit%2F1/restore',
      { reason: '内容已确认有效' }
    )
    expect(apiAdminPost).toHaveBeenNthCalledWith(
      3,
      '/api/governance/knowledge-units/unit%2F1/revision',
      { reason: '原文需要修正' }
    )
    expect(apiAdminPost).toHaveBeenNthCalledWith(4, '/api/governance/knowledge/item%2F1/offline', {
      reason: '整篇内容过时'
    })
    expect(apiAdminPost).toHaveBeenNthCalledWith(5, '/api/governance/knowledge/item%2F1/restore', {
      reason: '整篇重新启用'
    })
    expect(apiAdminPost).toHaveBeenNthCalledWith(
      6,
      '/api/governance/knowledge/item%2F1/versions/version%2F2/rollback',
      { reason: '恢复稳定版本' }
    )
  })
})
