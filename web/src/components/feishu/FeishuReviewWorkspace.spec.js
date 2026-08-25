// @vitest-environment jsdom

import { flushPromises, mount } from '@vue/test-utils'
import { message, Modal } from 'ant-design-vue'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { apiAdminGet, apiAdminPatch, apiAdminPost } from '@/apis/base'
import FeishuReviewWorkspace from './FeishuReviewWorkspace.vue'

vi.mock('@/apis/base', () => ({
  apiAdminGet: vi.fn(),
  apiAdminPatch: vi.fn(),
  apiAdminPost: vi.fn()
}))

vi.mock('ant-design-vue', async (importOriginal) => {
  const actual = await importOriginal()
  return {
    ...actual,
    message: { error: vi.fn(), success: vi.fn(), warning: vi.fn() },
    Modal: { confirm: vi.fn() }
  }
})

const packages = [
  {
    package_id: 'package-first',
    source_version_id: 'version-first',
    title: '第一份资料',
    wiki_path: '产品资料 / 第一份资料',
    workflow_status: 'OPEN',
    risk_level: 'MEDIUM',
    item_count: 1,
    review_type_counts: { NEW: 1 },
    updated_at: '2026-08-24T08:00:00Z'
  },
  {
    package_id: 'package-target',
    source_version_id: 'version-target',
    title: '目标资料',
    wiki_path: '产品资料 / 目标资料',
    workflow_status: 'OPEN',
    risk_level: 'HIGH',
    item_count: 1,
    review_type_counts: { CONFLICT: 1 },
    updated_at: '2026-08-24T09:00:00Z'
  }
]

function packageDetail(packageId = 'package-first') {
  const target = packageId === 'package-target'
  return {
    ...packages.find((item) => item.package_id === packageId),
    source_item_id: target ? 'item-target' : 'item-first',
    source_url: `https://quickdone.feishu.cn/wiki/${packageId}`,
    target_kb_id: 'kb-1',
    item_type: target ? 'pdf' : 'page',
    revision: target ? '2' : '1',
    yuxi_file_id: target ? 'file-target' : 'file-first',
    chunk_count: 2,
    token_count: 100,
    content_quality: target ? { checked: true, has_body: true } : {},
    previous_version: target
      ? {
          version_id: 'version-target-old',
          revision: '1',
          yuxi_file_id: 'file-target-old',
          chunk_count: 2,
          token_count: 80,
          published_at: '2026-08-20T08:00:00Z'
        }
      : null,
    lock_version: 3,
    draft: {},
    items: [
      {
        review_item_id: target ? 'item-review-target' : 'item-review-first',
        review_type: target ? 'UPDATE' : 'NEW',
        subject_id: target ? 'version-target' : 'version-first',
        title: target ? '目标资料冲突' : '第一份资料',
        summary: '请核对这份资料',
        relation_ids: target ? ['relation-1'] : [],
        problem_tags: target ? ['CONFLICT'] : [],
        applicability_scope: target ? { industry: '制造业', product: '知识助手' } : {},
        item_status: 'PENDING',
        allowed_outcomes: target
          ? ['KEEP_CURRENT', 'ADOPT_NEW_VERSION', 'SPLIT_SCOPE', 'WAIT_BUSINESS_CONFIRMATION']
          : ['PUBLISH', 'REQUEST_SOURCE_CHANGE', 'EXCLUDE'],
        reopened_from_item_id: target ? 'old-review-item' : null
      }
    ],
    relations: target
      ? [
          {
            relation_id: 'relation-1',
            relation_type: 'CONFLICT',
            source_title: '目标资料',
            target_title: '旧版部署手册',
            source_path: '产品资料 / 目标资料',
            target_path: '产品资料 / 旧版部署手册',
            source_revision: '2',
            target_revision: '1',
            confidence: 0.92,
            same_content: ['均适用于 Q900'],
            different_content: [{ field: '端口', current: '8080', candidate: '9090' }],
            reasoning: '相同条件下端口结论不同'
          }
        ]
      : [],
    change_requests: target
      ? [
          {
            change_request_id: 'change-1',
            status: 'OPEN',
            round_number: 1,
            request_text: '请补充适用版本',
            responsible_user_name: '资料负责人',
            updated_at: '2026-08-24T08:30:00Z'
          }
        ]
      : [],
    events: []
  }
}

function duplicatePackageDetail() {
  return {
    ...packageDetail('package-first'),
    package_id: 'package-duplicate',
    source_version_id: 'version-duplicate',
    title: '产品介绍 A',
    yuxi_file_id: 'file-duplicate',
    items: [
      {
        ...packageDetail('package-first').items[0],
        review_item_id: 'item-review-duplicate',
        subject_id: 'version-duplicate',
        title: '产品介绍 A',
        relation_ids: ['relation-duplicate'],
        problem_tags: ['DUPLICATE']
      }
    ],
    relations: [
      {
        relation_id: 'relation-duplicate',
        relation_type: 'EXACT_DUPLICATE',
        source_title: '产品介绍 A',
        target_title: '产品介绍 B',
        source_path: '产品资料 / 产品介绍 A',
        target_path: '产品资料 / 产品介绍 B',
        source_revision: '2',
        target_revision: '1',
        confidence: 0.98,
        same_content: ['均包含相同的公司简介'],
        different_content: [],
        reasoning: '公司简介片段完全一致'
      }
    ]
  }
}

function duplicateCandidates(decision = null) {
  return {
    relation_id: 'relation-duplicate',
    relation_type: 'EXACT_DUPLICATE',
    status: decision ? 'resolved' : 'open',
    source: {
      version_id: 'version-duplicate',
      revision: '2',
      title: '产品介绍 A',
      path: '产品资料 / 产品介绍 A'
    },
    target: {
      version_id: 'version-other',
      revision: '1',
      title: '产品介绍 B',
      path: '产品资料 / 产品介绍 B'
    },
    fragment_matches: [
      {
        match_id: 'match-1',
        source_chunk_id: 'chunk-a',
        source_chunk_index: 2,
        source_excerpt: '公司简介：狗狗你是公司专注企业数字化服务。',
        source_overlap_excerpt: '狗狗你是公司专注企业数字化服务。',
        target_chunk_id: 'chunk-b',
        target_chunk_index: 1,
        target_excerpt: '公司简介：狗狗你是公司专注企业数字化服务。',
        target_overlap_excerpt: '狗狗你是公司专注企业数字化服务。',
        similarity: 1
      }
    ],
    decision
  }
}

function mountWorkspace(props = {}) {
  return mount(FeishuReviewWorkspace, {
    props: { sourceId: 'source-1', ...props },
    global: {
      stubs: {
        'a-spin': { template: '<div><slot /></div>' },
        'a-empty': { props: ['description'], template: '<div>{{ description }}</div>' },
        'a-tag': { template: '<span><slot /></span>' },
        'a-button': {
          props: ['disabled'],
          emits: ['click'],
          template: '<button :disabled="disabled" @click="$emit(\'click\')"><slot /></button>'
        },
        'a-select': {
          props: ['value', 'options', 'placeholder'],
          emits: ['update:value'],
          template:
            '<select :value="value" :aria-label="$attrs[\'aria-label\'] || placeholder" @change="$emit(\'update:value\', $event.target.value)"><option v-for="option in options" :key="option.value" :value="option.value">{{ option.label }}</option></select>'
        },
        'a-input': {
          props: ['value', 'placeholder'],
          emits: ['update:value'],
          template:
            '<input :value="value" :placeholder="placeholder" @input="$emit(\'update:value\', $event.target.value)" />'
        },
        'a-textarea': {
          props: ['value', 'placeholder'],
          emits: ['update:value'],
          template:
            '<textarea :value="value" :placeholder="placeholder" @input="$emit(\'update:value\', $event.target.value)" />'
        },
        MarkdownPreview: {
          props: ['content'],
          template: '<div class="markdown-stub">{{ content }}</div>'
        }
      }
    }
  })
}

describe('FeishuReviewWorkspace', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    apiAdminGet.mockImplementation((url) => {
      if (url.startsWith('/api/governance/review-packages?')) {
        return Promise.resolve({
          items: packages,
          total: 2,
          counts: { mine: 2, waiting_source_change: 1, completed: 3 }
        })
      }
      if (url === '/api/governance/review-packages/package-first') {
        return Promise.resolve(packageDetail('package-first'))
      }
      if (url === '/api/governance/review-packages/package-target') {
        return Promise.resolve(packageDetail('package-target'))
      }
      if (url === '/api/governance/review-packages/package-first/segments') {
        return Promise.resolve({
          count: 2,
          token_count: 42,
          items: [
            {
              segment_id: 'seg-1',
              segment_index: 0,
              segment_type: 'paragraph',
              title_path: ['第一份资料', '部署要求'],
              locator: { page: 2, block: 1 },
              locator_label: '第2页',
              content: '部署环境至少需要八核处理器。',
              token_count: 18,
              publication_state: 'PENDING'
            },
            {
              segment_id: 'seg-2',
              segment_index: 1,
              segment_type: 'table',
              title_path: ['第一份资料', '参数表'],
              locator: { page: 3, block: 2 },
              locator_label: '第3页',
              content: '| 参数 | 值 |',
              token_count: 24,
              publication_state: 'PENDING'
            }
          ]
        })
      }
      if (url === '/api/governance/reviewers') {
        return Promise.resolve({ items: [{ user_id: 'admin-b', name: '管理员乙', role: 'admin' }] })
      }
      if (url === '/api/knowledge/databases/kb-1/documents/file-first/content') {
        return Promise.resolve({ content: '# 第一份资料正文', lines: [] })
      }
      if (url === '/api/knowledge/databases/kb-1/documents/file-target/content') {
        return Promise.resolve({ content: '# 目标资料\n端口：9090\n新增说明', lines: [] })
      }
      if (url === '/api/knowledge/databases/kb-1/documents/file-target-old/content') {
        return Promise.resolve({ content: '# 目标资料\n端口：8080\n保留内容', lines: [] })
      }
      return Promise.resolve({})
    })
    apiAdminPatch.mockResolvedValue({ lock_version: 4, draft: { outcome: 'PUBLISH' } })
    apiAdminPost.mockResolvedValue({})
  })

  it('初次进入立即加载审核包、真实数量和场景化操作', async () => {
    const wrapper = mountWorkspace()
    await flushPromises()

    expect(wrapper.get('.record-heading h2').text()).toBe('第一份资料')
    expect(wrapper.get('.markdown-stub').text()).toContain('第一份资料正文')
    expect(wrapper.text()).toContain('发布')
    expect(wrapper.text()).toContain('退回飞书修改')
    expect(wrapper.text()).toContain('新增知识 · 1项')
    expect(wrapper.text()).toContain('确认现有知识')
    expect(wrapper.text()).toContain('待审核')
    expect(wrapper.text()).toContain('更新于')
    expect(wrapper.text()).toContain('审核意见（选填）')
    const submitButton = wrapper.findAll('.decision-footer button')[1]
    expect(submitButton.text()).toBe('提交审核结果')
    expect(wrapper.text()).not.toContain('处理方式')
    expect(wrapper.find('.content-quality-alert').exists()).toBe(false)
    expect(wrapper.text()).not.toContain('适用范围')
    expect(wrapper.emitted('count-change')[0]).toEqual([2])
    expect(apiAdminGet).toHaveBeenCalledWith(
      '/api/governance/review-packages?source_id=source-1&view=mine'
    )
  })

  it('当前正文可按稳定来源片段定位并返回全文', async () => {
    const wrapper = mountWorkspace()
    await flushPromises()

    expect(wrapper.get('.source-segment-navigation').text()).toContain('2 个来源片段')
    const firstSegment = wrapper.findAll('.source-segment-list button')[0]
    expect(firstSegment.text()).toContain('第2页')

    await firstSegment.trigger('click')
    expect(wrapper.get('.source-segment-focus').text()).toContain('部署环境至少需要八核处理器')
    expect(wrapper.get('.source-segment-focus').text()).toContain('第一份资料 > 部署要求 · 第2页')

    await wrapper
      .findAll('.source-segment-heading button')
      .find((button) => button.text().includes('返回全文'))
      .trigger('click')
    expect(wrapper.find('.source-segment-focus').exists()).toBe(false)
    expect(wrapper.get('.markdown-stub').text()).toContain('第一份资料正文')
  })

  it('PPT 按真实页面呈现并支持点选页内片段', async () => {
    const pptSummary = {
      ...packages[0],
      package_id: 'package-ppt',
      source_version_id: 'version-ppt',
      title: '公司介绍.pptx'
    }
    const pptDetail = {
      ...packageDetail('package-first'),
      ...pptSummary,
      item_type: 'pptx',
      yuxi_file_id: 'file-ppt'
    }
    const createObjectUrl = vi.fn(() => 'blob:ppt-slide')
    const revokeObjectUrl = vi.fn()
    Object.defineProperty(URL, 'createObjectURL', { configurable: true, value: createObjectUrl })
    Object.defineProperty(URL, 'revokeObjectURL', { configurable: true, value: revokeObjectUrl })
    apiAdminGet.mockImplementation((url) => {
      if (url.startsWith('/api/governance/review-packages?')) {
        return Promise.resolve({ items: [pptSummary], total: 1, counts: { mine: 1 } })
      }
      if (url === '/api/governance/review-packages/package-ppt') return Promise.resolve(pptDetail)
      if (url === '/api/governance/review-packages/package-ppt/segments') {
        return Promise.resolve({ items: [], count: 0, token_count: 0 })
      }
      if (url === '/api/governance/review-packages/package-ppt/presentation') {
        return Promise.resolve({
          supported: true,
          slide_count: 2,
          aspect_ratio: 1.777778,
          slides: [
            {
              slide_number: 1,
              fragment_count: 2,
              fragments: [
                {
                  fragment_id: 'slide-1-shape-1',
                  fragment_number: 1,
                  content: '公司简介',
                  left: 10,
                  top: 10,
                  width: 30,
                  height: 10,
                  source_segment_ids: []
                },
                {
                  fragment_id: 'slide-1-shape-2',
                  fragment_number: 2,
                  content: '提供咨询、实施、交付和持续运营服务。',
                  left: 10,
                  top: 30,
                  width: 70,
                  height: 20,
                  source_segment_ids: ['seg-company']
                }
              ]
            },
            { slide_number: 2, fragment_count: 0, fragments: [] }
          ]
        })
      }
      if (url.startsWith('/api/governance/review-packages/package-ppt/presentation/slides/')) {
        return Promise.resolve({ blob: () => Promise.resolve(new Blob(['image'])) })
      }
      if (url === '/api/knowledge/databases/kb-1/documents/file-ppt/content') {
        return Promise.resolve({ content: '# 公司介绍', lines: [] })
      }
      if (url === '/api/governance/reviewers') return Promise.resolve({ items: [] })
      return Promise.resolve({})
    })

    const wrapper = mountWorkspace()
    await flushPromises()

    expect(wrapper.get('.presentation-review').text()).toContain('第 1 / 2 页')
    expect(wrapper.findAll('.presentation-fragment-hotspot')).toHaveLength(2)
    await wrapper.findAll('.presentation-fragment-hotspot')[1].trigger('click')
    expect(wrapper.get('.presentation-fragment-focus').text()).toContain(
      '提供咨询、实施、交付和持续运营服务。'
    )

    await wrapper.get('button[aria-label="下一页幻灯片"]').trigger('click')
    await flushPromises()
    expect(wrapper.get('.presentation-toolbar').text()).toContain('第 2 / 2 页')
    expect(createObjectUrl).toHaveBeenCalledTimes(2)
    expect(revokeObjectUrl).toHaveBeenCalled()
  })

  it('用直观文案说明无内容变化的现有知识确认任务', async () => {
    const stalePackage = {
      ...packages[0],
      package_id: 'package-stale',
      source_version_id: 'version-stale',
      title: '现有正式知识',
      review_type_counts: { STALE: 1 }
    }
    const staleDetail = {
      ...packageDetail('package-first'),
      ...stalePackage,
      yuxi_file_id: 'file-stale',
      items: [
        {
          ...packageDetail('package-first').items[0],
          review_item_id: 'item-review-stale',
          review_type: 'STALE',
          subject_id: 'version-stale',
          title: '现有正式知识',
          allowed_outcomes: ['CONFIRM_VALID', 'ARCHIVE', 'DISMISS']
        }
      ]
    }
    apiAdminGet.mockImplementation((url) => {
      if (url.startsWith('/api/governance/review-packages?')) {
        return Promise.resolve({ items: [stalePackage], total: 1, counts: { mine: 1 } })
      }
      if (url === '/api/governance/review-packages/package-stale') {
        return Promise.resolve(staleDetail)
      }
      if (url === '/api/governance/reviewers') return Promise.resolve({ items: [] })
      if (url === '/api/knowledge/databases/kb-1/documents/file-stale/content') {
        return Promise.resolve({ content: '# 现有正式知识', lines: [] })
      }
      return Promise.resolve({})
    })

    const wrapper = mountWorkspace()
    await flushPromises()

    expect(wrapper.text()).toContain('确认现有知识')
    expect(wrapper.text()).toContain('内容未变化，请确认是否继续有效。')
    expect(wrapper.text()).not.toContain('有效性复核')
  })

  it('不纳入知识库时允许不填写审核意见', async () => {
    const wrapper = mountWorkspace()
    await flushPromises()

    await wrapper.findAll('.outcome-list button')[2].trigger('click')
    const submitButton = wrapper.findAll('.decision-footer button')[1]
    expect(submitButton.element.disabled).toBe(false)
    await submitButton.trigger('click')
    await flushPromises()

    expect(message.warning).not.toHaveBeenCalled()
    expect(apiAdminPost).toHaveBeenCalledWith(
      '/api/governance/review-packages/package-first/resolve',
      expect.objectContaining({
        decisions: [
          expect.objectContaining({
            outcome: 'EXCLUDE',
            decision_comment: undefined
          })
        ]
      })
    )
  })

  it('变更任务默认展示旧版与新版的具体差异', async () => {
    const wrapper = mountWorkspace({ targetReviewId: 'version-target' })
    await flushPromises()

    expect(wrapper.get('.record-heading h2').text()).toBe('目标资料')
    expect(wrapper.text()).toContain('资料修改后重新审核')
    expect(wrapper.text()).toContain('更新已有知识')
    expect(wrapper.get('.evidence-tabs').text()).toContain('具体变更')
    expect(wrapper.get('.version-change-review').text()).toContain('版本 1')
    expect(wrapper.get('.version-change-review').text()).toContain('版本 2')
    expect(wrapper.get('.version-change-review').text()).toContain('端口：8080')
    expect(wrapper.get('.version-change-review').text()).toContain('端口：9090')
    expect(wrapper.text()).not.toContain('按范围拆分')
    expect(wrapper.find('input[placeholder="行业"]').exists()).toBe(false)

    const comparisonTab = wrapper
      .findAll('.evidence-tabs button')
      .find((button) => button.text().includes('跨文档证据'))
    await comparisonTab.trigger('click')
    expect(wrapper.text()).toContain('目标资料 ↔ 旧版部署手册')
    expect(wrapper.text()).toContain('相同条件下端口结论不同')
    expect(wrapper.emitted('target-consumed')).toHaveLength(1)
  })

  it('从跨文档关系列表进入时定位对应资料和证据页', async () => {
    const wrapper = mountWorkspace({
      targetReviewId: {
        relationId: 'relation-1',
        sourceVersionId: 'version-not-in-review',
        targetVersionId: 'version-target'
      }
    })
    await flushPromises()

    expect(wrapper.get('.record-heading h2').text()).toBe('目标资料')
    expect(wrapper.get('.evidence-tabs [role="tab"][aria-selected="true"]').text()).toContain(
      '跨文档证据'
    )
    expect(wrapper.text()).toContain('目标资料 ↔ 旧版部署手册')
    expect(wrapper.emitted('target-consumed')).toHaveLength(1)
  })

  it('默认把空间留给证据，并可按需收起任务列表和打开审核处理', async () => {
    const wrapper = mountWorkspace()
    await flushPromises()

    expect(wrapper.get('.decision-panel').classes()).not.toContain('open')
    expect(wrapper.get('.review-workspace').classes()).not.toContain('queue-collapsed')

    await wrapper.get('button[aria-label="收起审核任务"]').trigger('click')
    expect(wrapper.get('.review-workspace').classes()).toContain('queue-collapsed')
    expect(wrapper.get('.review-queue').attributes('style')).toContain('display: none')

    await wrapper
      .findAll('.record-actions button')
      .find((button) => button.text().includes('审核处理'))
      .trigger('click')
    expect(wrapper.get('.decision-panel').classes()).toContain('open')

    await wrapper.get('button[aria-label="关闭审核处理"]').trigger('click')
    expect(wrapper.get('.decision-panel').classes()).not.toContain('open')
  })

  it('跨文档证据一次聚焦一条并支持前后切换', async () => {
    const detail = packageDetail('package-target')
    detail.items[0].relation_ids = ['relation-1', 'relation-2']
    detail.relations.push({
      ...detail.relations[0],
      relation_id: 'relation-2',
      relation_type: 'CONDITIONAL_VARIANT',
      target_title: '第二份相关资料',
      target_path: '产品资料 / 第二份相关资料'
    })
    apiAdminGet.mockImplementation((url) => {
      if (url.startsWith('/api/governance/review-packages?')) {
        return Promise.resolve({ items: packages, total: 2, counts: { mine: 2 } })
      }
      if (url === '/api/governance/review-packages/package-target') {
        return Promise.resolve(detail)
      }
      if (url === '/api/governance/reviewers') return Promise.resolve({ items: [] })
      if (url === '/api/knowledge/databases/kb-1/documents/file-target/content') {
        return Promise.resolve({ content: '# 目标资料', lines: [] })
      }
      if (url === '/api/knowledge/databases/kb-1/documents/file-target-old/content') {
        return Promise.resolve({ content: '# 旧版资料', lines: [] })
      }
      return Promise.resolve({})
    })

    const wrapper = mountWorkspace({ targetReviewId: 'version-target' })
    await flushPromises()
    const comparisonTab = wrapper
      .findAll('.evidence-tabs button')
      .find((button) => button.text().includes('跨文档证据'))
    await comparisonTab.trigger('click')
    await flushPromises()

    expect(wrapper.findAll('.comparison-card')).toHaveLength(1)
    expect(wrapper.get('.comparison-position').text()).toContain('关系 1 / 2')
    expect(wrapper.get('.comparison-card').text()).toContain('旧版部署手册')

    await wrapper.get('button[aria-label="下一条关系"]').trigger('click')
    await flushPromises()
    expect(wrapper.get('.comparison-position').text()).toContain('关系 2 / 2')
    expect(wrapper.get('.comparison-card').text()).toContain('第二份相关资料')
    expect(wrapper.get('.comparison-card').text()).not.toContain('旧版部署手册')
  })

  it('关系对应资料不在审核队列第一页时扩大查询并只插入目标资料', async () => {
    apiAdminGet.mockImplementation((url) => {
      if (url === '/api/governance/review-packages?source_id=source-1&view=mine') {
        return Promise.resolve({ items: [packages[0]], total: 98, counts: { mine: 98 } })
      }
      if (url === '/api/governance/review-packages?source_id=source-1&view=mine&page_size=100') {
        return Promise.resolve({ items: packages, total: 98, counts: { mine: 98 } })
      }
      if (url === '/api/governance/review-packages/package-target') {
        return Promise.resolve(packageDetail('package-target'))
      }
      if (url === '/api/governance/reviewers') return Promise.resolve({ items: [] })
      if (url === '/api/knowledge/databases/kb-1/documents/file-target/content') {
        return Promise.resolve({ content: '# 目标资料\n端口：9090\n新增说明', lines: [] })
      }
      if (url === '/api/knowledge/databases/kb-1/documents/file-target-old/content') {
        return Promise.resolve({ content: '# 目标资料\n端口：8080\n保留内容', lines: [] })
      }
      return Promise.resolve({})
    })

    const wrapper = mountWorkspace({
      targetReviewId: {
        relationId: 'relation-1',
        sourceVersionId: 'version-not-in-review',
        targetVersionId: 'version-target'
      }
    })
    await flushPromises()

    expect(apiAdminGet).toHaveBeenCalledWith(
      '/api/governance/review-packages?source_id=source-1&view=mine&page_size=100'
    )
    expect(apiAdminGet).not.toHaveBeenCalledWith('/api/governance/review-packages/package-first')
    expect(wrapper.get('.record-heading h2').text()).toBe('目标资料')
    expect(wrapper.findAll('.queue-item')).toHaveLength(2)
    expect(wrapper.get('.evidence-tabs [role="tab"][aria-selected="true"]').text()).toContain(
      '跨文档证据'
    )
    expect(wrapper.emitted('target-consumed')).toHaveLength(1)
  })

  it('保存草稿和退回修改均携带审核项及乐观锁', async () => {
    const wrapper = mountWorkspace()
    await flushPromises()

    await wrapper.findAll('.outcome-list button')[1].trigger('click')
    await wrapper.get('input[placeholder="填写飞书原文修改负责人"]').setValue('资料负责人')
    await wrapper
      .get('textarea[placeholder="具体说明飞书原文需要修改或补充什么"]')
      .setValue('请补充适用产品版本')
    const footerButtons = wrapper.findAll('.decision-footer button')
    await footerButtons[0].trigger('click')
    await flushPromises()

    expect(apiAdminPatch).toHaveBeenCalledWith(
      '/api/governance/review-packages/package-first/draft',
      expect.objectContaining({
        lock_version: 3,
        draft: expect.objectContaining({
          review_item_id: 'item-review-first',
          outcome: 'REQUEST_SOURCE_CHANGE',
          decision_comment: '请补充适用产品版本'
        })
      })
    )

    await footerButtons[1].trigger('click')
    await flushPromises()
    expect(apiAdminPost).toHaveBeenCalledWith(
      '/api/governance/review-packages/package-first/resolve',
      expect.objectContaining({
        lock_version: 4,
        decisions: [
          expect.objectContaining({
            review_item_id: 'item-review-first',
            outcome: 'REQUEST_SOURCE_CHANGE',
            responsible_user_name: '资料负责人'
          })
        ]
      })
    )
  })

  it('处理记录中可以取消仍在进行的资料修改任务', async () => {
    const wrapper = mountWorkspace({ targetReviewId: 'version-target' })
    await flushPromises()
    const historyTab = wrapper
      .findAll('.evidence-tabs button')
      .find((button) => button.text().includes('处理记录'))
    await historyTab.trigger('click')
    await wrapper.get('.cancel-change-request').trigger('click')

    expect(Modal.confirm).toHaveBeenCalledOnce()
    await Modal.confirm.mock.calls[0][0].onOk()
    expect(apiAdminPost).toHaveBeenCalledWith(
      '/api/governance/source-change-requests/change-1/cancel',
      { reason: '人工取消资料修改任务' }
    )
    expect(message.success).toHaveBeenCalledWith('资料修改任务已取消')
  })

  it('在跨文档证据中加载并展示两边的重复片段', async () => {
    const detail = duplicatePackageDetail()
    apiAdminGet.mockImplementation((url) => {
      if (url.startsWith('/api/governance/review-packages?')) {
        return Promise.resolve({
          items: [
            {
              ...packages[0],
              package_id: detail.package_id,
              source_version_id: detail.source_version_id,
              title: detail.title
            }
          ],
          total: 1,
          counts: { mine: 1 }
        })
      }
      if (url === '/api/governance/review-packages/package-duplicate') {
        return Promise.resolve(detail)
      }
      if (url === '/api/governance/relations/relation-duplicate/duplicate-candidates') {
        return Promise.resolve(duplicateCandidates())
      }
      if (url === '/api/governance/reviewers') return Promise.resolve({ items: [] })
      if (url === '/api/knowledge/databases/kb-1/documents/file-duplicate/content') {
        return Promise.resolve({ content: '# 产品介绍 A', lines: [] })
      }
      return Promise.resolve({})
    })

    const wrapper = mountWorkspace()
    await flushPromises()
    const comparisonTab = wrapper
      .findAll('.evidence-tabs button')
      .find((button) => button.text().includes('跨文档证据'))
    await comparisonTab.trigger('click')
    await flushPromises()

    expect(apiAdminGet).toHaveBeenCalledWith(
      '/api/governance/relations/relation-duplicate/duplicate-candidates'
    )
    expect(wrapper.get('.duplicate-match').text()).toContain('来源一 · 重叠部分')
    expect(wrapper.get('.duplicate-match').text()).toContain('来源二 · 重叠部分')
    expect(wrapper.get('.duplicate-match').text()).toContain('狗狗你是公司专注企业数字化服务')
    expect(wrapper.get('.duplicate-actions').text()).toContain('独有内容')
  })

  it('可以选择规范内容并将另一边记录为重复来源', async () => {
    const detail = duplicatePackageDetail()
    apiAdminGet.mockImplementation((url) => {
      if (url.startsWith('/api/governance/review-packages?')) {
        return Promise.resolve({
          items: [
            {
              ...packages[0],
              package_id: detail.package_id,
              source_version_id: detail.source_version_id,
              title: detail.title
            }
          ],
          total: 1,
          counts: { mine: 1 }
        })
      }
      if (url === '/api/governance/review-packages/package-duplicate') {
        return Promise.resolve(detail)
      }
      if (url === '/api/governance/relations/relation-duplicate/duplicate-candidates') {
        return Promise.resolve(duplicateCandidates())
      }
      if (url === '/api/governance/reviewers') return Promise.resolve({ items: [] })
      if (url === '/api/knowledge/databases/kb-1/documents/file-duplicate/content') {
        return Promise.resolve({ content: '# 产品介绍 A', lines: [] })
      }
      return Promise.resolve({})
    })
    apiAdminPost.mockImplementation((url, payload) => {
      if (url === '/api/governance/relations/relation-duplicate/resolve-duplicate') {
        return Promise.resolve(
          duplicateCandidates({
            strategy: payload.strategy,
            primary_version_id: 'version-duplicate',
            logical_knowledge_ids: ['logical-1'],
            fragment_match_ids: ['match-1']
          })
        )
      }
      return Promise.resolve({})
    })

    const wrapper = mountWorkspace()
    await flushPromises()
    const comparisonTab = wrapper
      .findAll('.evidence-tabs button')
      .find((button) => button.text().includes('跨文档证据'))
    await comparisonTab.trigger('click')
    await flushPromises()
    const sourceButton = wrapper
      .findAll('.duplicate-actions button')
      .find((button) => button.text().includes('保留来源一'))
    await sourceButton.trigger('click')

    expect(Modal.confirm).toHaveBeenCalledOnce()
    expect(Modal.confirm.mock.calls[0][0].content).toContain('独有内容不受影响')
    await Modal.confirm.mock.calls[0][0].onOk()
    await flushPromises()

    expect(apiAdminPost).toHaveBeenCalledWith(
      '/api/governance/relations/relation-duplicate/resolve-duplicate',
      expect.objectContaining({ strategy: 'USE_SOURCE' })
    )
    expect(wrapper.get('.duplicate-decision-result').text()).toContain(
      '已将“产品介绍 A”设为规范内容'
    )
    expect(wrapper.get('.duplicate-decision-result').text()).toContain('1 组重复片段')
  })

  it('可以明确决定两边内容分别保留', async () => {
    const detail = duplicatePackageDetail()
    apiAdminGet.mockImplementation((url) => {
      if (url.startsWith('/api/governance/review-packages?')) {
        return Promise.resolve({
          items: [
            {
              ...packages[0],
              package_id: detail.package_id,
              source_version_id: detail.source_version_id,
              title: detail.title
            }
          ],
          total: 1,
          counts: { mine: 1 }
        })
      }
      if (url === '/api/governance/review-packages/package-duplicate') {
        return Promise.resolve(detail)
      }
      if (url === '/api/governance/relations/relation-duplicate/duplicate-candidates') {
        return Promise.resolve(duplicateCandidates())
      }
      if (url === '/api/governance/reviewers') return Promise.resolve({ items: [] })
      if (url === '/api/knowledge/databases/kb-1/documents/file-duplicate/content') {
        return Promise.resolve({ content: '# 产品介绍 A', lines: [] })
      }
      return Promise.resolve({})
    })
    apiAdminPost.mockImplementation((url, payload) =>
      Promise.resolve(
        duplicateCandidates({
          strategy: payload.strategy,
          primary_version_id: null,
          logical_knowledge_ids: [],
          fragment_match_ids: ['match-1']
        })
      )
    )

    const wrapper = mountWorkspace()
    await flushPromises()
    const comparisonTab = wrapper
      .findAll('.evidence-tabs button')
      .find((button) => button.text().includes('跨文档证据'))
    await comparisonTab.trigger('click')
    await flushPromises()
    const separateButton = wrapper
      .findAll('.duplicate-actions button')
      .find((button) => button.text().includes('分别保留'))
    await separateButton.trigger('click')
    await Modal.confirm.mock.calls[0][0].onOk()
    await flushPromises()

    expect(apiAdminPost).toHaveBeenCalledWith(
      '/api/governance/relations/relation-duplicate/resolve-duplicate',
      expect.objectContaining({ strategy: 'KEEP_SEPARATE' })
    )
    expect(wrapper.get('.duplicate-decision-result').text()).toContain('已决定分别保留')
  })
})
