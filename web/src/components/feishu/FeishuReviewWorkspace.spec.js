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

function knowledgeUnitPackageDetail() {
  return {
    ...packageDetail('package-first'),
    item_count: 3,
    knowledge_unit_count: 3,
    attention_unit_count: 2,
    safe_recommendation_count: 1,
    decided_unit_count: 0,
    remaining_unit_count: 3,
    included_unit_count: 0,
    excluded_unit_count: 0,
    recommendation_counts: { PUBLISH: 1, ADOPT_NEW_VERSION: 1, KEEP_CURRENT: 1 },
    items: [
      {
        review_item_id: 'unit-safe',
        review_type: 'NEW',
        subject_type: 'KNOWLEDGE_UNIT',
        subject_id: 'knowledge-unit-safe',
        title: '产品能力',
        summary: '未发现冲突或解析异常，建议纳入知识库。',
        subject_locator: { page: 1 },
        relation_ids: [],
        problem_tags: [],
        item_status: 'PENDING',
        allowed_outcomes: ['PUBLISH', 'REQUEST_SOURCE_CHANGE', 'EXCLUDE'],
        knowledge_unit: true,
        content: '产品支持知识加工、审核和来源追溯。',
        source_segment_ids: ['seg-1'],
        change_type: 'NEW',
        recommended_outcome: 'PUBLISH',
        recommendation_reason: '未发现冲突或解析异常，建议纳入知识库。',
        recommendation_confidence: 0.92,
        manual_review_required: false,
        comparison_status: 'completed'
      },
      {
        review_item_id: 'unit-updated',
        review_type: 'UPDATE',
        subject_type: 'KNOWLEDGE_UNIT',
        subject_id: 'knowledge-unit-updated',
        title: '部署要求',
        summary: '该知识单元内容已变化，请核对差异后采用新版。',
        subject_locator: { page: 2 },
        relation_ids: [],
        problem_tags: [],
        item_status: 'PENDING',
        allowed_outcomes: ['ADOPT_NEW_VERSION', 'KEEP_CURRENT', 'EXCLUDE'],
        knowledge_unit: true,
        content: '生产环境至少需要八核处理器。',
        previous_content: '生产环境至少需要四核处理器。',
        source_segment_ids: ['seg-2'],
        change_type: 'UPDATED',
        recommended_outcome: 'ADOPT_NEW_VERSION',
        recommendation_reason: '该知识单元内容已变化，请核对差异后采用新版。',
        recommendation_confidence: 0.9,
        manual_review_required: true,
        comparison_status: 'completed'
      },
      {
        review_item_id: 'unit-conflict',
        review_type: 'CONFLICT',
        subject_type: 'KNOWLEDGE_UNIT',
        subject_id: 'knowledge-unit-conflict',
        title: '服务端口',
        summary: '发现结论冲突，需要人工确认。',
        subject_locator: { page: 3 },
        relation_ids: ['relation-1'],
        problem_tags: ['CONFLICT'],
        item_status: 'PENDING',
        allowed_outcomes: ['KEEP_CURRENT', 'ADOPT_NEW_VERSION'],
        knowledge_unit: true,
        content: '服务端口为 9090。',
        source_segment_ids: ['seg-3'],
        change_type: 'UPDATED',
        recommended_outcome: 'KEEP_CURRENT',
        recommendation_reason: '发现结论冲突，需要人工确认。',
        recommendation_confidence: 1,
        manual_review_required: true,
        comparison_status: 'completed'
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

    expect(wrapper.find('.record-heading').exists()).toBe(false)
    expect(wrapper.find('.unit-overview').exists()).toBe(false)
    expect(wrapper.find('.item-navigation').exists()).toBe(false)
    expect(wrapper.find('.knowledge-lineage').exists()).toBe(false)
    expect(wrapper.find('.evidence-context').exists()).toBe(false)
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

  it('整篇资料可从顶部批量审核，当前知识单元仍从右侧处理', async () => {
    const wrapper = mountWorkspace()
    await flushPromises()

    const wholeReviewButton = wrapper
      .findAll('.record-actions button')
      .find((button) => button.text().includes('整篇批量审核'))
    expect(wholeReviewButton).toBeTruthy()
    await wholeReviewButton.trigger('click')
    expect(Modal.confirm).toHaveBeenCalledWith(
      expect.objectContaining({
        title: '整篇批量审核',
        okText: '确认批量处理整篇资料'
      })
    )

    await Modal.confirm.mock.calls[0][0].onOk()
    await flushPromises()
    expect(apiAdminPost).toHaveBeenCalledWith(
      '/api/governance/review-packages/package-first/resolve',
      expect.objectContaining({
        decisions: [
          expect.objectContaining({
            review_item_id: 'item-review-first',
            outcome: 'PUBLISH'
          })
        ]
      })
    )

    await wrapper
      .findAll('.record-actions button')
      .find((button) => button.text().includes('审核处理'))
      .trigger('click')
    expect(wrapper.get('.decision-panel').classes()).toContain('open')
    expect(wrapper.get('.outcome-list').exists()).toBe(true)
    expect(wrapper.get('.field-label-row label').text()).toBe('问题记录（可选）')
    expect(wrapper.get('.problem-help').attributes('aria-label')).toContain('不等同于审核结果')
    await wrapper
      .findAll('.outcome-list button')
      .find((button) => button.text().includes('退回飞书修改'))
      .trigger('click')
    expect(wrapper.get('.field-label-row label').text()).toBe('退回原因（可多选）')
    expect(wrapper.get('.problem-help').attributes('aria-label')).toContain('作为修改依据')
  })

  it('整篇资料没有可批量项时仍显示入口并明确提示逐条处理', async () => {
    const detail = packageDetail('package-first')
    detail.items[0].item_status = 'WAITING_SOURCE_CHANGE'
    apiAdminGet.mockImplementation((url) => {
      if (url.startsWith('/api/governance/review-packages?')) {
        return Promise.resolve({ items: [packages[0]], total: 1, counts: { mine: 1 } })
      }
      if (url === '/api/governance/review-packages/package-first') {
        return Promise.resolve(detail)
      }
      if (url === '/api/governance/review-packages/package-first/segments') {
        return Promise.resolve({ items: [] })
      }
      if (url === '/api/governance/reviewers') return Promise.resolve({ items: [] })
      return Promise.resolve({})
    })

    const wrapper = mountWorkspace()
    await flushPromises()

    const wholeReviewButton = wrapper
      .findAll('.record-actions button')
      .find((button) => button.text().includes('整篇批量审核'))
    expect(wholeReviewButton).toBeTruthy()
    expect(wholeReviewButton.attributes('disabled')).toBeDefined()
    expect(wholeReviewButton.attributes('title')).toContain('逐条处理')
  })

  it('滚动到审核任务列表底部时继续加载下一页，直到显示全部任务', async () => {
    const nextPackage = {
      ...packages[0],
      package_id: 'package-next',
      source_version_id: 'version-next',
      title: '下一页资料'
    }
    apiAdminGet.mockImplementation((url) => {
      if (url === '/api/governance/review-packages?source_id=source-1&view=mine') {
        return Promise.resolve({ items: packages, total: 3, counts: { mine: 3 } })
      }
      if (url === '/api/governance/review-packages?source_id=source-1&view=mine&page=2&page_size=20') {
        return Promise.resolve({ items: [nextPackage], total: 3, counts: { mine: 3 } })
      }
      if (url.startsWith('/api/governance/review-packages/package-')) {
        return Promise.resolve(packageDetail(url.split('/').pop()))
      }
      return Promise.resolve({})
    })

    const wrapper = mountWorkspace()
    await flushPromises()

    const loadMore = wrapper.get('.queue-load-more-button')
    expect(loadMore.text()).toContain('已显示 2 / 3')
    await loadMore.trigger('click')
    await flushPromises()

    expect(wrapper.findAll('.queue-item')).toHaveLength(3)
    expect(wrapper.get('.queue-load-more').text()).toContain('已显示全部 3 个审核任务')
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
    expect(apiAdminGet).toHaveBeenCalledWith(
      '/api/governance/review-packages/package-ppt/presentation/slides/2',
      {},
      'blob'
    )
    await wrapper.findAll('.presentation-fragment-hotspot')[1].trigger('click')
    expect(wrapper.get('.presentation-fragment-focus').text()).toContain(
      '提供咨询、实施、交付和持续运营服务。'
    )

    await wrapper.get('button[aria-label="下一页幻灯片"]').trigger('click')
    await flushPromises()
    expect(wrapper.get('.presentation-toolbar').text()).toContain('第 2 / 2 页')
    const previewCallCount = apiAdminGet.mock.calls.filter(([url]) =>
      url.includes('/presentation/slides/')
    ).length
    await wrapper.get('button[aria-label="上一页幻灯片"]').trigger('click')
    await flushPromises()
    expect(wrapper.get('.presentation-toolbar').text()).toContain('第 1 / 2 页')
    expect(
      apiAdminGet.mock.calls.filter(([url]) => url.includes('/presentation/slides/')).length
    ).toBe(previewCallCount)
    expect(createObjectUrl).toHaveBeenCalledTimes(2)
    expect(revokeObjectUrl).not.toHaveBeenCalled()
    wrapper.unmount()
    expect(revokeObjectUrl).toHaveBeenCalled()
  })

  it('PPT 知识单元在页面清单加载后自动定位对应页', async () => {
    const pptSummary = {
      ...packages[0],
      package_id: 'package-ppt-unit',
      source_version_id: 'version-ppt-unit',
      title: '实施方案.pptx',
      knowledge_unit_count: 1,
      attention_unit_count: 1
    }
    const pptDetail = {
      ...packageDetail('package-first'),
      ...pptSummary,
      item_type: 'pptx',
      yuxi_file_id: 'file-ppt-unit',
      recommendation_counts: { PUBLISH: 1 },
      safe_recommendation_count: 0,
      items: [
        {
          review_item_id: 'unit-slide-2',
          review_type: 'NEW',
          subject_type: 'KNOWLEDGE_UNIT',
          subject_id: 'knowledge-unit-slide-2',
          title: '第 2 页幻灯片',
          summary: '请核对当前页面。',
          subject_locator: { source_segment_ids: ['seg-slide-2'] },
          relation_ids: [],
          problem_tags: [],
          item_status: 'PENDING',
          allowed_outcomes: ['PUBLISH', 'REQUEST_SOURCE_CHANGE', 'EXCLUDE'],
          knowledge_unit: true,
          content: '第二页完整内容',
          source_segment_ids: ['seg-slide-2'],
          change_type: 'NEW',
          recommended_outcome: 'PUBLISH',
          manual_review_required: true
        }
      ]
    }
    Object.defineProperty(URL, 'createObjectURL', {
      configurable: true,
      value: vi.fn(() => 'blob:ppt-slide-2')
    })
    Object.defineProperty(URL, 'revokeObjectURL', { configurable: true, value: vi.fn() })
    apiAdminGet.mockImplementation((url) => {
      if (url.startsWith('/api/governance/review-packages?')) {
        return Promise.resolve({ items: [pptSummary], total: 1, counts: { mine: 1 } })
      }
      if (url === '/api/governance/review-packages/package-ppt-unit') {
        return Promise.resolve(pptDetail)
      }
      if (url === '/api/governance/review-packages/package-ppt-unit/segments') {
        return Promise.resolve({ items: [], count: 0, token_count: 0 })
      }
      if (url === '/api/governance/review-packages/package-ppt-unit/presentation') {
        return Promise.resolve({
          supported: true,
          slide_count: 2,
          aspect_ratio: 1.777778,
          slides: [
            { slide_number: 1, fragment_count: 0, fragments: [] },
            {
              slide_number: 2,
              fragment_count: 1,
              fragments: [
                {
                  fragment_id: 'slide-2-shape-1',
                  fragment_number: 1,
                  content: '第二页完整内容',
                  left: 10,
                  top: 10,
                  width: 50,
                  height: 20,
                  source_segment_ids: ['seg-slide-2']
                }
              ]
            }
          ]
        })
      }
      if (url.startsWith('/api/governance/review-packages/package-ppt-unit/presentation/slides/')) {
        return Promise.resolve({ blob: () => Promise.resolve(new Blob(['image'])) })
      }
      if (url === '/api/knowledge/databases/kb-1/documents/file-ppt-unit/content') {
        return Promise.resolve({ content: '# 实施方案', lines: [] })
      }
      if (url === '/api/governance/reviewers') return Promise.resolve({ items: [] })
      return Promise.resolve({})
    })

    const wrapper = mountWorkspace()
    await flushPromises()

    expect(wrapper.get('.presentation-toolbar').text()).toContain('第 2 / 2 页')
    expect(wrapper.get('.presentation-fragment-hotspot').classes()).toContain('active')
    expect(wrapper.get('.presentation-stage-row').classes()).toContain('has-side-panel')
    expect(wrapper.get('.layout-context-sidebar').text()).toContain('审核信息')
    expect(wrapper.get('.layout-sidebar-primary').text()).toContain('处理当前知识单元')
    expect(
      wrapper.findAll('.record-actions button').some((button) => button.text().includes('处理当前知识单元'))
    ).toBe(false)
    expect(apiAdminGet).toHaveBeenCalledWith(
      '/api/governance/review-packages/package-ppt-unit/presentation/slides/2',
      {},
      'blob'
    )
  })

  it('Word 版式按页面展示并支持编辑内容块审核草稿', async () => {
    const documentSummary = {
      ...packages[0],
      package_id: 'package-docx-layout',
      source_version_id: 'version-docx-layout',
      title: '部署指南.docx',
      item_type: 'docx'
    }
    const documentDetail = {
      ...packageDetail('package-first'),
      ...documentSummary,
      yuxi_file_id: 'file-docx-layout'
    }
    documentDetail.knowledge_unit_count = 1
    documentDetail.items = documentDetail.items.map((item) => ({
      ...item,
      knowledge_unit: true,
      source_segment_ids: []
    }))
    Object.defineProperty(URL, 'createObjectURL', {
      configurable: true,
      value: vi.fn(() => 'blob:document-page')
    })
    Object.defineProperty(URL, 'revokeObjectURL', { configurable: true, value: vi.fn() })
    apiAdminGet.mockImplementation((url) => {
      if (url.startsWith('/api/governance/review-packages?')) {
        return Promise.resolve({ items: [documentSummary], total: 1, counts: { mine: 1 } })
      }
      if (url === '/api/governance/review-packages/package-docx-layout') {
        return Promise.resolve(documentDetail)
      }
      if (url === '/api/governance/review-packages/package-docx-layout/segments') {
        return Promise.resolve({ items: [], count: 0, token_count: 0 })
      }
      if (url === '/api/governance/review-packages/package-docx-layout/layout') {
        return Promise.resolve({
          supported: true,
          file_type: '.docx',
          page_count: 1,
          pages: [
            {
              page_number: 1,
              label: '第 1 页',
              aspect_ratio: 0.707,
              render_mode: 'image',
              block_count: 1,
              blocks: [
                {
                  block_id: 'page-1-block-1',
                  content: '部署前准备',
                  left: 10,
                  top: 10,
                  width: 30,
                  height: 8,
                  source_segment_ids: []
                }
              ]
            }
          ],
          edits: {}
        })
      }
      if (url === '/api/governance/review-packages/package-docx-layout/layout/pages/1') {
        return Promise.resolve({ blob: () => Promise.resolve(new Blob(['image'])) })
      }
      if (url === '/api/knowledge/databases/kb-1/documents/file-docx-layout/content') {
        return Promise.resolve({ content: '# 部署指南', lines: [] })
      }
      if (url === '/api/governance/reviewers') return Promise.resolve({ items: [] })
      return Promise.resolve({})
    })
    apiAdminPatch.mockResolvedValue({
      lock_version: 4,
      draft: { layout_edits: { 'page-1-block-1': { content: '部署前准备（已确认）' } } }
    })

    const wrapper = mountWorkspace()
    await flushPromises()

    expect(wrapper.get('.document-layout-review').text()).toContain('1 个可定位内容块')
    expect(wrapper.get('.document-layout-review').text()).not.toContain('版式查看')
    expect(wrapper.get('.evidence-tabs-actions').text()).toContain('查看飞书原文')
    expect(wrapper.find('.record-heading').exists()).toBe(false)
    expect(wrapper.get('.layout-sidebar-primary').text()).toContain('处理当前知识单元')
    expect(wrapper.findAll('.record-actions button').some((button) => button.text().includes('处理当前知识单元'))).toBe(false)
    expect(wrapper.get('.layout-context-sidebar').text()).toContain('部署指南.docx')
    expect(wrapper.get('.layout-context-sidebar').text()).toContain('审核信息')
    expect(wrapper.findAll('.document-layout-block')).toHaveLength(1)
    await wrapper.get('.document-layout-block').trigger('click')
    const editor = wrapper.get('.document-layout-editor textarea')
    await editor.setValue('部署前准备（已确认）')
    await wrapper.get('.document-layout-save').trigger('click')
    await flushPromises()

    expect(apiAdminPatch).toHaveBeenCalledWith(
      '/api/governance/review-packages/package-docx-layout/layout/edits',
      expect.objectContaining({
        block_id: 'page-1-block-1',
        content: '部署前准备（已确认）'
      })
    )
    expect(message.success).toHaveBeenCalledWith('版式编辑草稿已保存，飞书原文未被修改')
  })

  it('点击 Word 知识单元时按来源片段切页并高亮对应内容块', async () => {
    const documentSummary = {
      ...packages[0],
      package_id: 'package-docx-unit-linkage',
      source_version_id: 'version-docx-unit-linkage',
      title: '合同条款.docx',
      item_type: 'docx',
      knowledge_unit_count: 2
    }
    const baseItem = packageDetail('package-first').items[0]
    const documentDetail = {
      ...packageDetail('package-first'),
      ...documentSummary,
      yuxi_file_id: 'file-docx-unit-linkage',
      item_count: 2,
      remaining_unit_count: 2,
      items: [
        {
          ...baseItem,
          review_item_id: 'unit-page-1',
          subject_type: 'KNOWLEDGE_UNIT',
          subject_id: 'knowledge-unit-page-1',
          title: '第一页条款',
          subject_locator: { source_segment_ids: ['seg-page-1'] },
          knowledge_unit: true,
          content: '第一页条款内容',
          source_segment_ids: ['seg-page-1'],
          change_type: 'NEW',
          recommended_outcome: 'PUBLISH',
          manual_review_required: false
        },
        {
          ...baseItem,
          review_item_id: 'unit-page-2',
          subject_type: 'KNOWLEDGE_UNIT',
          subject_id: 'knowledge-unit-page-2',
          title: '第二页条款',
          subject_locator: { source_segment_ids: ['seg-page-2'] },
          knowledge_unit: true,
          content: '第二页条款内容',
          source_segment_ids: ['seg-page-2'],
          change_type: 'NEW',
          recommended_outcome: 'PUBLISH',
          manual_review_required: false
        }
      ]
    }
    Object.defineProperty(URL, 'createObjectURL', {
      configurable: true,
      value: vi.fn((blob) => `blob:document-page-${blob.size}`)
    })
    Object.defineProperty(URL, 'revokeObjectURL', { configurable: true, value: vi.fn() })
    apiAdminGet.mockImplementation((url) => {
      if (url.startsWith('/api/governance/review-packages?')) {
        return Promise.resolve({ items: [documentSummary], total: 1, counts: { mine: 1 } })
      }
      if (url === '/api/governance/review-packages/package-docx-unit-linkage') {
        return Promise.resolve(documentDetail)
      }
      if (url === '/api/governance/review-packages/package-docx-unit-linkage/segments') {
        return Promise.resolve({ items: [], count: 0, token_count: 0 })
      }
      if (url === '/api/governance/review-packages/package-docx-unit-linkage/layout') {
        return Promise.resolve({
          supported: true,
          file_type: '.docx',
          page_count: 2,
          pages: [
            {
              page_number: 1,
              label: '第 1 页',
              aspect_ratio: 0.707,
              render_mode: 'image',
              block_count: 1,
              blocks: [
                {
                  block_id: 'page-1-block-1',
                  content: '第一页条款内容',
                  left: 10,
                  top: 10,
                  width: 30,
                  height: 8,
                  source_segment_ids: ['seg-page-1']
                }
              ]
            },
            {
              page_number: 2,
              label: '第 2 页',
              aspect_ratio: 0.707,
              render_mode: 'image',
              block_count: 1,
              blocks: [
                {
                  block_id: 'page-2-block-1',
                  content: '第二页条款内容',
                  left: 20,
                  top: 20,
                  width: 40,
                  height: 8,
                  source_segment_ids: ['seg-page-2']
                }
              ]
            }
          ],
          edits: {}
        })
      }
      if (url.startsWith('/api/governance/review-packages/package-docx-unit-linkage/layout/pages/')) {
        return Promise.resolve({ blob: () => Promise.resolve(new Blob([url])) })
      }
      if (url === '/api/knowledge/databases/kb-1/documents/file-docx-unit-linkage/content') {
        return Promise.resolve({ content: '# 合同条款', lines: [] })
      }
      if (url === '/api/governance/reviewers') return Promise.resolve({ items: [] })
      return Promise.resolve({})
    })

    const wrapper = mountWorkspace()
    await flushPromises()

    expect(wrapper.get('.document-layout-page-strip button.active').text()).toContain('第 1 页')
    expect(wrapper.get('.document-layout-block.active').attributes('title')).toBe('第一页条款内容')
    await wrapper.get('.layout-sidebar-list-toggle').trigger('click')
    await wrapper.findAll('.layout-sidebar-unit-list button')[1].trigger('click')
    await flushPromises()

    expect(wrapper.get('.document-layout-page-strip button.active').text()).toContain('第 2 页')
    expect(wrapper.get('.document-layout-block.active').attributes('title')).toBe('第二页条款内容')
    expect(wrapper.get('.layout-sidebar-facts').text()).toContain('2 / 2')
    expect(apiAdminGet).toHaveBeenCalledWith(
      '/api/governance/review-packages/package-docx-unit-linkage/layout/pages/2',
      {},
      'blob'
    )
  })

  it('Excel 内容较多时使用可滚动的表格画布并保持可读尺寸', async () => {
    const spreadsheetSummary = {
      ...packages[0],
      package_id: 'package-xlsx-layout',
      source_version_id: 'version-xlsx-layout',
      title: '投标清单.xlsx',
      item_type: 'xlsx'
    }
    const spreadsheetDetail = {
      ...packageDetail('package-first'),
      ...spreadsheetSummary,
      yuxi_file_id: 'file-xlsx-layout'
    }
    apiAdminGet.mockImplementation((url) => {
      if (url.startsWith('/api/governance/review-packages?')) {
        return Promise.resolve({ items: [spreadsheetSummary], total: 1, counts: { mine: 1 } })
      }
      if (url === '/api/governance/review-packages/package-xlsx-layout') {
        return Promise.resolve(spreadsheetDetail)
      }
      if (url === '/api/governance/review-packages/package-xlsx-layout/segments') {
        return Promise.resolve({ items: [], count: 0, token_count: 0 })
      }
      if (url === '/api/governance/review-packages/package-xlsx-layout/layout') {
        return Promise.resolve({
          supported: true,
          file_type: '.xlsx',
          page_count: 1,
          pages: [
            {
              page_number: 1,
              label: '报价明细',
              width: 12,
              height: 90,
              render_mode: 'grid',
              block_count: 2,
              blocks: [
                {
                  block_id: 'sheet-1-cell-A1',
                  content: '项目名称',
                  left: 0,
                  top: 0,
                  width: 8.3333,
                  height: 1.1111,
                  locator: { sheet: '报价明细', cell: 'A1' },
                  source_segment_ids: []
                },
                {
                  block_id: 'sheet-1-cell-L90',
                  content: '较长的说明文字',
                  left: 91.6667,
                  top: 98.8889,
                  width: 8.3333,
                  height: 1.1111,
                  locator: { sheet: '报价明细', cell: 'L90' },
                  source_segment_ids: []
                }
              ]
            }
          ],
          edits: {}
        })
      }
      if (url === '/api/knowledge/databases/kb-1/documents/file-xlsx-layout/content') {
        return Promise.resolve({ content: '# 投标清单', lines: [] })
      }
      if (url === '/api/governance/reviewers') return Promise.resolve({ items: [] })
      return Promise.resolve({})
    })

    const wrapper = mountWorkspace()
    await flushPromises()

    expect(wrapper.get('.spreadsheet-viewport').text()).toContain('项目名称')
    expect(wrapper.get('.document-layout-toolbar').text()).toContain('横向、纵向滚动查看')
    expect(wrapper.get('.review-workspace').classes()).toContain('layout-focus')
    expect(wrapper.find('.spreadsheet-canvas').attributes('style')).toContain('--sheet-columns: 12')
    expect(wrapper.findAll('.spreadsheet-cell')).toHaveLength(2)
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

    expect(wrapper.find('.record-heading').exists()).toBe(false)
    expect(wrapper.find('.reopen-trail').exists()).toBe(false)
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

    expect(wrapper.find('.record-heading').exists()).toBe(false)
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
    expect(wrapper.find('.record-heading').exists()).toBe(false)
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
    expect(wrapper.get('.duplicate-action-help').attributes('aria-label')).toContain('独有内容')
  })

  it('跨文档证据只高亮对应文字块，异页证据分别定位且不联动翻页', async () => {
    const detail = duplicatePackageDetail()
    Object.defineProperty(URL, 'createObjectURL', {
      configurable: true,
      value: vi.fn(() => 'blob:relation-page')
    })
    Object.defineProperty(URL, 'revokeObjectURL', { configurable: true, value: vi.fn() })
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
      if (url === '/api/governance/review-packages/package-duplicate') return Promise.resolve(detail)
      if (url === '/api/governance/relations/relation-duplicate/duplicate-candidates') {
        return Promise.resolve(duplicateCandidates())
      }
      if (url === '/api/governance/relations/relation-duplicate/layout-comparison') {
        return Promise.resolve({
          supported: true,
          relation_id: 'relation-duplicate',
          relation_type: 'EXACT_DUPLICATE',
          source: {
            title: '产品介绍 A',
            revision: '2',
            pages: [
              {
                page_number: 1,
                aspect_ratio: 1.77,
                blocks: []
              },
              {
                page_number: 2,
                aspect_ratio: 1.77,
                blocks: [
                  {
                    block_id: 'source-block-1',
                    content: '公司简介：狗狗你是公司专注企业数字化服务。',
                    left: 10,
                    top: 10,
                    width: 70,
                    height: 10,
                    source_segment_ids: []
                  },
                  {
                    block_id: 'source-block-unrelated',
                    content: '本页还包含产品功能介绍。',
                    left: 10,
                    top: 30,
                    width: 70,
                    height: 10,
                    source_segment_ids: []
                  }
                ]
              },
              {
                page_number: 3,
                aspect_ratio: 1.77,
                blocks: []
              }
            ]
          },
          target: {
            title: '产品介绍 B',
            revision: '1',
            pages: [
              {
                page_number: 1,
                aspect_ratio: 1.77,
                blocks: []
              },
              {
                page_number: 2,
                aspect_ratio: 1.77,
                blocks: []
              },
              {
                page_number: 3,
                aspect_ratio: 1.77,
                blocks: [
                  {
                    block_id: 'target-block-1',
                    content: '公司简介：狗狗你是公司专注企业数字化服务。',
                    left: 12,
                    top: 12,
                    width: 70,
                    height: 10,
                    source_segment_ids: []
                  },
                  {
                    block_id: 'target-block-unrelated',
                    content: '本页还包含客户案例。',
                    left: 12,
                    top: 32,
                    width: 70,
                    height: 10,
                    source_segment_ids: []
                  }
                ]
              }
            ]
          },
          matches: [
            {
              match_id: 'match-1',
              similarity: 0.98,
              source_page_number: 2,
              target_page_number: 3,
              source_block_ids: ['source-block-1'],
              target_block_ids: ['target-block-1'],
              source_overlap_excerpt: '狗狗你是公司专注企业数字化服务。',
              target_overlap_excerpt: '狗狗你是公司专注企业数字化服务。'
            }
          ]
        })
      }
      if (url.includes('/layout-comparison/source/pages/') || url.includes('/layout-comparison/target/pages/')) {
        return Promise.resolve({ blob: () => Promise.resolve(new Blob(['image'])) })
      }
      if (url === '/api/governance/reviewers') return Promise.resolve({ items: [] })
      if (url === '/api/knowledge/databases/kb-1/documents/file-duplicate/content') {
        return Promise.resolve({ content: '# 产品介绍 A', lines: [] })
      }
      return Promise.resolve({})
    })

    const wrapper = mountWorkspace()
    await flushPromises()
    await wrapper
      .findAll('.evidence-tabs button')
      .find((button) => button.text().includes('跨文档证据'))
      .trigger('click')
    await flushPromises()

    expect(wrapper.get('.comparison-layout-review').text()).toContain('版式对比')
    expect(wrapper.findAll('.comparison-layout-pane')).toHaveLength(2)
    expect(wrapper.get('.comparison-match-strip').text()).toContain('98%')
    expect(wrapper.findAll('.comparison-layout-block.comparison-block-match')).toHaveLength(2)
    expect(wrapper.findAll('.comparison-layout-block.comparison-block-selected')).toHaveLength(2)
    expect(wrapper.findAll('.comparison-layout-pane')[0].text()).toContain('第 2 / 3 页')
    expect(wrapper.findAll('.comparison-layout-pane')[1].text()).toContain('第 3 / 3 页')
    expect(apiAdminGet).toHaveBeenCalledWith(
      '/api/governance/relations/relation-duplicate/layout-comparison/source/pages/2',
      {},
      'blob'
    )
    expect(apiAdminGet).toHaveBeenCalledWith(
      '/api/governance/relations/relation-duplicate/layout-comparison/target/pages/2',
      {},
      'blob'
    )

    const targetPageTwoCallCount = apiAdminGet.mock.calls.filter(([url]) =>
      url.endsWith('/layout-comparison/target/pages/2')
    ).length
    await wrapper.get('button[aria-label="来源二上一页"]').trigger('click')
    await flushPromises()

    expect(wrapper.findAll('.comparison-layout-pane')[0].text()).toContain('第 2 / 3 页')
    expect(wrapper.findAll('.comparison-layout-pane')[1].text()).toContain('第 2 / 3 页')
    expect(
      apiAdminGet.mock.calls.filter(([url]) =>
        url.endsWith('/layout-comparison/target/pages/2')
      ).length
    ).toBe(targetPageTwoCallCount)
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

  it('知识单元默认展示待处理项并可从顶部批量处理低风险项', async () => {
    const detail = knowledgeUnitPackageDetail()
    apiAdminGet.mockImplementation((url) => {
      if (url.startsWith('/api/governance/review-packages?')) {
        return Promise.resolve({
          items: [
            {
              ...packages[0],
              knowledge_unit_count: 3,
              attention_unit_count: 2,
              decided_unit_count: 0,
              remaining_unit_count: 3,
              included_unit_count: 0
            }
          ],
          total: 1,
          counts: { mine: 1 }
        })
      }
      if (url === '/api/governance/review-packages/package-first') {
        return Promise.resolve(detail)
      }
      if (url === '/api/governance/review-packages/package-first/segments') {
        return Promise.resolve({
          items: [
            {
              segment_id: 'seg-1',
              segment_index: 0,
              title_path: ['产品能力'],
              locator_label: '第1页',
              content: '产品支持知识加工、审核和来源追溯。'
            },
            {
              segment_id: 'seg-2',
              segment_index: 1,
              title_path: ['部署要求'],
              locator_label: '第2页',
              content: '生产环境至少需要八核处理器。'
            },
            {
              segment_id: 'seg-3',
              segment_index: 2,
              title_path: ['服务端口'],
              locator_label: '第3页',
              content: '服务端口为 9090。'
            }
          ]
        })
      }
      if (url === '/api/governance/reviewers') return Promise.resolve({ items: [] })
      if (url === '/api/knowledge/databases/kb-1/documents/file-first/content') {
        return Promise.resolve({ content: '# 第一份资料正文', lines: [] })
      }
      return Promise.resolve({})
    })
    apiAdminPost.mockResolvedValue({
      unit_publish_version_ids: ['version-first'],
      remaining_unit_count: 2
    })

    const wrapper = mountWorkspace()
    await flushPromises()

    expect(wrapper.get('.queue-unit-summary').text()).toContain('0/3 已处理')
    expect(wrapper.get('.queue-unit-summary').text()).toContain('待处理 3')
    expect(wrapper.get('.queue-unit-summary').text()).toContain('已纳入 0')
    expect(wrapper.find('.unit-overview').exists()).toBe(false)
    expect(wrapper.find('.item-navigation').exists()).toBe(false)
    expect(wrapper.find('.knowledge-lineage').exists()).toBe(false)
    expect(wrapper.find('.evidence-context').exists()).toBe(false)
    expect(wrapper.get('.record-actions').text()).toContain('处理当前知识单元')
    expect(wrapper.get('.version-change-list').text()).toContain('四核处理器')
    expect(wrapper.get('.version-change-list').text()).toContain('八核处理器')

    const batchButton = wrapper
      .findAll('.record-actions button')
      .find((button) => button.text().includes('整篇批量审核'))
    await batchButton.trigger('click')
    expect(Modal.confirm).toHaveBeenCalledOnce()
    await Modal.confirm.mock.calls[0][0].onOk()
    await flushPromises()

    expect(apiAdminPost).toHaveBeenCalledWith(
      '/api/governance/review-packages/package-first/resolve',
      expect.objectContaining({
        decisions: [
          expect.objectContaining({
            review_item_id: 'unit-safe',
            outcome: 'PUBLISH'
          })
        ]
      })
    )
    expect(wrapper.emitted('knowledge-change')).toHaveLength(1)
    expect(message.success).toHaveBeenCalledWith('已批量处理 1 个安全项；仍有 2 个知识单元待逐条审核')
  })

  it('知识单元支持批量不纳入和批量退回资料修改', async () => {
    const detail = knowledgeUnitPackageDetail()
    apiAdminGet.mockImplementation((url) => {
      if (url.startsWith('/api/governance/review-packages?')) {
        return Promise.resolve({ items: [packages[0]], total: 1, counts: { mine: 1 } })
      }
      if (url === '/api/governance/review-packages/package-first') {
        return Promise.resolve(detail)
      }
      if (url === '/api/governance/review-packages/package-first/segments') {
        return Promise.resolve({ items: [], count: 0, token_count: 0 })
      }
      if (url === '/api/knowledge/databases/kb-1/documents/file-first/content') {
        return Promise.resolve({ content: '# 第一份资料正文', lines: [] })
      }
      if (url === '/api/governance/reviewers') return Promise.resolve({ items: [] })
      return Promise.resolve({})
    })
    apiAdminPost.mockResolvedValue({ remaining_unit_count: 1 })

    const wrapper = mountWorkspace()
    await flushPromises()

    const excludeButton = wrapper
      .findAll('.batch-action-secondary')
      .find((button) => button.text() === '批量不纳入')
    expect(excludeButton.attributes('disabled')).toBeUndefined()
    await excludeButton.trigger('click')
    await Modal.confirm.mock.calls[0][0].onOk()
    await flushPromises()
    expect(apiAdminPost).toHaveBeenCalledWith(
      '/api/governance/review-packages/package-first/resolve',
      expect.objectContaining({
        decisions: expect.arrayContaining([
          expect.objectContaining({ outcome: 'EXCLUDE' })
        ])
      })
    )

    const returnButton = wrapper
      .findAll('.batch-action-secondary')
      .find((button) => button.text() === '批量退回')
    await returnButton.trigger('click')
    await Modal.confirm.mock.calls[1][0].onOk()
    await flushPromises()
    expect(apiAdminPost).toHaveBeenLastCalledWith(
      '/api/governance/review-packages/package-first/resolve',
      expect.objectContaining({
        decisions: [expect.objectContaining({ outcome: 'REQUEST_SOURCE_CHANGE' })]
      })
    )
  })

  it('已处理知识单元数量仍在审核任务列表显示', async () => {
    const detail = knowledgeUnitPackageDetail()
    detail.decided_unit_count = 1
    detail.remaining_unit_count = 2
    detail.included_unit_count = 1
    detail.items[0] = {
      ...detail.items[0],
      item_status: 'DECIDED',
      outcome: 'PUBLISH',
      decision_comment: '已确认纳入'
    }
    apiAdminGet.mockImplementation((url) => {
      if (url.startsWith('/api/governance/review-packages?')) {
        return Promise.resolve({
          items: [
            {
              ...packages[0],
              knowledge_unit_count: 3,
              decided_unit_count: 1,
              remaining_unit_count: 2,
              included_unit_count: 1
            }
          ],
          total: 1,
          counts: { mine: 1 }
        })
      }
      if (url === '/api/governance/review-packages/package-first') return Promise.resolve(detail)
      if (url === '/api/governance/review-packages/package-first/segments') {
        return Promise.resolve({ items: [] })
      }
      if (url === '/api/governance/reviewers') return Promise.resolve({ items: [] })
      if (url.includes('/documents/file-first/content')) {
        return Promise.resolve({ content: '# 第一份资料正文', lines: [] })
      }
      return Promise.resolve({})
    })

    const wrapper = mountWorkspace()
    await flushPromises()

    expect(wrapper.get('.queue-unit-summary').text()).toContain('1/3 已处理')
    expect(wrapper.get('.queue-unit-summary').text()).toContain('待处理 2')
    expect(wrapper.get('.queue-unit-summary').text()).toContain('已纳入 1')
    expect(wrapper.find('.item-navigation').exists()).toBe(false)
  })

  it('发布单个知识单元时明确提示正在加入正式知识和剩余数量', async () => {
    const detail = knowledgeUnitPackageDetail()
    apiAdminGet.mockImplementation((url) => {
      if (url.startsWith('/api/governance/review-packages?')) {
        return Promise.resolve({
          items: [
            {
              ...packages[0],
              knowledge_unit_count: 3,
              decided_unit_count: 0,
              remaining_unit_count: 3,
              included_unit_count: 0
            }
          ],
          total: 1,
          counts: { mine: 1 }
        })
      }
      if (url === '/api/governance/review-packages/package-first') return Promise.resolve(detail)
      if (url === '/api/governance/review-packages/package-first/segments') {
        return Promise.resolve({ items: [] })
      }
      if (url === '/api/governance/reviewers') return Promise.resolve({ items: [] })
      if (url.includes('/documents/file-first/content')) {
        return Promise.resolve({ content: '# 第一份资料正文', lines: [] })
      }
      return Promise.resolve({})
    })
    apiAdminPost.mockResolvedValue({
      unit_publish_version_ids: ['version-first'],
      remaining_unit_count: 2
    })

    const wrapper = mountWorkspace()
    await flushPromises()
    await wrapper.findAll('.decision-footer button')[1].trigger('click')
    await flushPromises()

    expect(message.success).toHaveBeenCalledWith(
      '“部署要求”已确认纳入，正在加入正式知识；本材料还有 2 个知识单元待处理'
    )
    expect(wrapper.emitted('knowledge-change')).toHaveLength(1)
  })
})
