// @vitest-environment jsdom

import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { governanceApi } from '@/apis/governance_api'
import FeishuWorkItemsPanel from './FeishuWorkItemsPanel.vue'

vi.mock('@/apis/governance_api', () => ({
  governanceApi: {
    listWorkItems: vi.fn(),
    getWorkItemSummary: vi.fn(),
    getErrorMessage: vi.fn((_error, fallback) => fallback)
  }
}))

function mountPanel() {
  return mount(FeishuWorkItemsPanel, {
    props: { sourceId: 'source-1' },
    global: {
      stubs: {
        'a-button': {
          props: ['loading'],
          emits: ['click'],
          template: '<button @click="$emit(\'click\')"><slot /></button>'
        },
        'a-select': {
          props: ['value', 'options'],
          emits: ['update:value'],
          template:
            '<select :value="value" :aria-label="$attrs[\'aria-label\']" @change="$emit(\'update:value\', $event.target.value)"><option v-for="option in options" :key="option.value" :value="option.value">{{ option.label }}</option></select>'
        },
        'a-checkbox': {
          props: ['checked'],
          emits: ['update:checked'],
          template:
            '<label><input type="checkbox" :checked="checked" @change="$emit(\'update:checked\', $event.target.checked)" /><slot /></label>'
        },
        'a-alert': { template: '<div />' },
        'a-spin': { template: '<div><slot /></div>' },
        'a-empty': { template: '<div />' }
      }
    }
  })
}

describe('FeishuWorkItemsPanel', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    governanceApi.listWorkItems.mockResolvedValue({
      items: [
        {
          id: 'feedback:1',
          type: 'USER_FEEDBACK',
          title: '产品架构回答需修正',
          source: { path: '产品 / 架构说明' },
          risk: 'HIGH',
          status: 'OPEN',
          assigneeId: null,
          overdue: true,
          aiSummary: '用户反馈引用内容可能过时',
          suggestedAction: '核对正式知识并修订来源',
          blockReasons: ['等待来源材料修正'],
          navigation: { module: 'source-change', packageId: 'package-1' },
          qualityScore: null,
          createdAt: '2026-09-01T00:00:00Z'
        }
      ]
    })
    governanceApi.getWorkItemSummary.mockResolvedValue({
      total: 1,
      overdue: 1,
      unassigned: 1,
      byRisk: { HIGH: 1 },
      byType: { USER_FEEDBACK: 1 }
    })
  })

  it('展示 AI 摘要并直接跳转到现有处理页面', async () => {
    const wrapper = mountPanel()
    await flushPromises()

    expect(wrapper.text()).toContain('用户反馈')
    expect(wrapper.text()).toContain('用户反馈引用内容可能过时')
    expect(wrapper.text()).toContain('等待来源材料修正')
    expect(wrapper.emitted('count-change')?.at(-1)).toEqual([1])

    await wrapper.get('.work-title').trigger('click')
    expect(wrapper.emitted('navigate')?.at(-1)).toEqual([
      { module: 'source-change', packageId: 'package-1' }
    ])
  })

  it('按类型和超期条件重新加载待办', async () => {
    const wrapper = mountPanel()
    await flushPromises()

    await wrapper.get('select[aria-label="待办类型"]').setValue('USER_FEEDBACK')
    await wrapper.get('input[type="checkbox"]').setValue(true)
    await flushPromises()

    expect(governanceApi.listWorkItems).toHaveBeenLastCalledWith(
      expect.objectContaining({
        source_id: 'source-1',
        type: 'USER_FEEDBACK',
        overdue: true
      })
    )
  })
})
