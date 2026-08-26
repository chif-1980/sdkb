// @vitest-environment jsdom

import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { apiAdminGet } from '@/apis/base'
import FeishuFormalKnowledgePanel from './FeishuFormalKnowledgePanel.vue'

vi.mock('@/apis/base', () => ({
  apiAdminGet: vi.fn()
}))

function mountPanel() {
  return mount(FeishuFormalKnowledgePanel, {
    props: { sourceId: 'source-1' },
    global: {
      stubs: {
        'a-button': {
          emits: ['click'],
          template: '<button @click="$emit(\'click\')"><slot /></button>'
        },
        'a-select': { template: '<select />' },
        'a-input': { template: '<input />' },
        'a-spin': { template: '<div><slot /></div>' },
        'a-tag': { template: '<span><slot /></span>' },
        'a-empty': {
          props: ['description'],
          template: '<div>{{ description }}</div>'
        },
        'a-modal': {
          props: ['open'],
          template: '<section v-if="open" class="modal-stub"><slot /></section>'
        },
        'a-table': {
          props: ['columns', 'dataSource'],
          template:
            '<div class="table-stub"><template v-for="record in dataSource" :key="record.knowledge_id || record.version_id"><div v-for="column in columns" :key="column.key"><slot name="bodyCell" :column="column" :record="record" /></div></template><slot v-if="!dataSource.length" name="emptyText" /></div>'
        }
      }
    }
  })
}

describe('FeishuFormalKnowledgePanel', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    apiAdminGet.mockImplementation((url) => {
      if (url === '/api/governance/knowledge?source_id=source-1') {
        return Promise.resolve({
          items: [
            {
              knowledge_id: 'item-1:section:deployment',
              knowledge_level: 'UNIT',
              unit_id: 'unit-1',
              title: '部署前置条件',
              revision: '3',
              source_item_id: 'item-1',
              source_title: 'Q900 部署指南',
              source_url: 'https://quickdone.feishu.cn/wiki/source-1',
              wiki_path: '产品资料 / Q900 部署指南',
              source_role: 'PRIMARY',
              source_segment_count: 2,
              source_locator: { page: 3 },
              applicability_scope: {},
              chunk_count: 8
            }
          ]
        })
      }
      if (url === '/api/governance/knowledge/item-1/versions') {
        return Promise.resolve({
          items: [
            {
              version_id: 'version-3',
              revision: '3',
              active: true,
              review_status: 'approved',
              published_at: '2026-08-25T10:00:00Z'
            }
          ]
        })
      }
      return Promise.resolve({ items: [] })
    })
  })

  it('按知识单元展示正式知识并可追溯原始材料与版本', async () => {
    const wrapper = mountPanel()
    await flushPromises()

    expect(wrapper.text()).toContain('正式知识单元')
    expect(wrapper.text()).toContain('部署前置条件')
    expect(wrapper.text()).toContain('2 个来源片段')
    expect(wrapper.text()).toContain('Q900 部署指南')
    expect(wrapper.text()).toContain('按原始材料分组展示')

    await wrapper.get('.knowledge-row').trigger('click')
    await flushPromises()

    expect(apiAdminGet).toHaveBeenCalledWith('/api/governance/knowledge/item-1/versions')
    expect(wrapper.get('.source-trace').text()).toContain('原始材料')
    expect(wrapper.get('.source-trace').text()).toContain('Q900 部署指南')
    expect(wrapper.get('.source-trace').text()).toContain('知识单元')
    expect(wrapper.get('.source-trace').text()).toContain('第 3 页')
  })
})
