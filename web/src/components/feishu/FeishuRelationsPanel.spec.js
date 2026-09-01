// @vitest-environment jsdom

import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { apiAdminGet } from '@/apis/base'
import FeishuRelationsPanel from './FeishuRelationsPanel.vue'

vi.mock('@/apis/base', () => ({
  apiAdminGet: vi.fn(),
  apiAdminPost: vi.fn()
}))

function mountPanel() {
  return mount(FeishuRelationsPanel, {
    props: { sourceId: 'source-1' },
    global: {
      stubs: {
        'a-button': { template: '<button><slot /></button>' },
        'a-select': { template: '<select />' },
        'a-input': { template: '<input />' },
        'a-tag': { template: '<span><slot /></span>' },
        'a-empty': { template: '<div />' },
        'a-table': {
          props: ['columns', 'dataSource'],
          template:
            '<div><template v-for="record in dataSource" :key="record.relation_id"><div v-for="column in columns" :key="column.key"><slot name="bodyCell" :column="column" :record="record" /></div></template></div>'
        }
      }
    }
  })
}

describe('FeishuRelationsPanel', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    apiAdminGet.mockImplementation((url) => {
      if (url.startsWith('/api/governance/relations?')) {
        return Promise.resolve({
          items: [
            {
              relation_id: 'relation-1',
              relation_type: 'OVERLAP',
              status: 'open',
              source_title: '待审核资料',
              source_path: '产品资料 / 待审核资料',
              source_processing_status: 'awaiting_review',
              source_review_status: 'pending',
              target_title: '正式知识资料',
              target_path: '正式知识 / 正式知识资料',
              target_processing_status: 'published',
              target_review_status: 'approved',
              same_content: ['共同内容'],
              different_content: [],
              scope_difference: {}
            }
          ]
        })
      }
      return Promise.resolve({
        status: 'completed',
        total: 2,
        completed: 2,
        relation_count: 1,
        issue_count: 0
      })
    })
  })

  it('分别标记待审核资料和已发布知识', async () => {
    const wrapper = mountPanel()
    await flushPromises()

    const stages = wrapper.findAll('.source-stage')
    expect(stages.map((stage) => stage.text())).toEqual(['待审核', '已发布'])
    expect(stages[0].classes()).toContain('source-stage-pending')
    expect(stages[1].classes()).toContain('source-stage-published')
  })
})
