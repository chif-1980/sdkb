// @vitest-environment jsdom

import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { apiAdminGet } from '@/apis/base'
import FeishuReviewWorkspace from './FeishuReviewWorkspace.vue'

vi.mock('@/apis/base', () => ({
  apiAdminGet: vi.fn(),
  apiAdminPost: vi.fn()
}))

vi.mock('ant-design-vue', async (importOriginal) => {
  const actual = await importOriginal()
  return {
    ...actual,
    message: { error: vi.fn(), success: vi.fn() }
  }
})

const reviews = [
  {
    review_id: 'review-first',
    version_id: 'version-first',
    title: '第一份资料',
    problem_tags: [],
    applicability_scope: {},
    relation_types: [],
    comparison_count: 0,
    risk_level: 'LOW'
  },
  {
    review_id: 'review-target',
    version_id: 'version-target',
    title: '目标资料',
    problem_tags: ['CONFLICT'],
    applicability_scope: { industry: '制造业', product: '知识助手' },
    relation_types: ['CONFLICT'],
    comparison_count: 1,
    risk_level: 'HIGH'
  }
]

function mountWorkspace(props = {}) {
  return mount(FeishuReviewWorkspace, {
    props: { sourceId: 'source-1', ...props },
    global: {
      stubs: {
        'a-spin': { template: '<div><slot /></div>' },
        'a-empty': { template: '<div />' },
        'a-tag': { template: '<span><slot /></span>' },
        'a-button': { template: '<button><slot /></button>' },
        'a-select': { template: '<div />' },
        'a-input': {
          props: ['value', 'placeholder'],
          template: '<input :value="value" :placeholder="placeholder" />'
        },
        'a-textarea': { template: '<textarea />' }
      }
    }
  })
}

describe('FeishuReviewWorkspace', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    apiAdminGet.mockImplementation((url) => {
      if (url.startsWith('/api/governance/reviews?')) return Promise.resolve({ items: reviews })
      if (url === '/api/governance/reviewers') return Promise.resolve({ items: [] })
      if (url.endsWith('/comparisons')) return Promise.resolve({ items: [] })
      return Promise.resolve({})
    })
  })

  it('从跨文档关系进入时选择对应任务并初始化适用范围', async () => {
    const wrapper = mountWorkspace({ targetReviewId: 'version-target' })
    await flushPromises()

    expect(wrapper.get('.record-heading h2').text()).toBe('目标资料')
    expect(wrapper.get('input[placeholder="行业"]').element.value).toBe('制造业')
    expect(wrapper.get('input[placeholder="产品"]').element.value).toBe('知识助手')
    expect(wrapper.emitted('target-consumed')).toHaveLength(1)
    expect(apiAdminGet).toHaveBeenCalledWith(
      '/api/governance/reviews/review-target/comparisons'
    )
  })

  it('普通进入时用第一条任务初始化审核表单', async () => {
    const wrapper = mountWorkspace()
    await flushPromises()

    expect(wrapper.get('.record-heading h2').text()).toBe('第一份资料')
    expect(wrapper.get('input[placeholder="行业"]').element.value).toBe('')
  })
})
