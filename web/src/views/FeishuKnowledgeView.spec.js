// @vitest-environment jsdom

import { flushPromises, mount } from '@vue/test-utils'
import { message, Modal } from 'ant-design-vue'
import QRCode from 'qrcode'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { apiAdminGet, apiAdminPatch, apiAdminPost } from '@/apis/base'
import { feishuKnowledgeApi } from '@/apis/feishu_knowledge_api'
import { documentApi } from '@/apis/knowledge_api'
import FeishuMaterialDetailDrawer from '@/components/feishu/FeishuMaterialDetailDrawer.vue'
import FeishuMaterialTable from '@/components/feishu/FeishuMaterialTable.vue'
import FeishuKnowledgeView from './FeishuKnowledgeView.vue'

vi.mock('@/apis/base', () => ({
  apiAdminGet: vi.fn(),
  apiAdminPatch: vi.fn(),
  apiAdminPost: vi.fn()
}))

vi.mock('@/apis/knowledge_api', () => ({
  documentApi: {
    getDocumentContent: vi.fn()
  }
}))

vi.mock('qrcode', () => ({
  default: {
    toDataURL: vi.fn()
  }
}))

vi.mock('ant-design-vue', async (importOriginal) => {
  const actual = await importOriginal()
  return {
    ...actual,
    message: {
      error: vi.fn(),
      success: vi.fn(),
      warning: vi.fn()
    },
    Modal: {
      confirm: vi.fn()
    }
  }
})

const source = {
  source_id: 'source-1',
  name: '飞书产品资料',
  wiki_root_token: 'wiki-token',
  wiki_root_url: 'https://quickdone.feishu.cn/wiki/wiki-token',
  target_kb_id: 'kb-1',
  last_full_sync_at: null,
  last_incremental_sync_at: null,
  total_count: 12,
  awaiting_review_count: 3,
  failed_count: 1,
  source_invalid_count: 2
}

function makeMaterial(versionId, overrides = {}) {
  return {
    version_id: versionId,
    title: versionId,
    item_type: 'page',
    processing_status: 'awaiting_review',
    review_status: 'pending',
    source_validity: 'valid',
    active: false,
    yuxi_file_id: `file-${versionId}`,
    content_quality: { checked: true, has_body: true, body_length: 24 },
    ...overrides
  }
}

function mountView() {
  return mount(FeishuKnowledgeView, {
    global: {
      stubs: {
        'a-button': {
          props: ['disabled', 'loading'],
          emits: ['click'],
          template:
            '<button :disabled="disabled" :data-loading="String(Boolean(loading))" @click="$emit(\'click\')"><slot /></button>'
        },
        'a-tag': { template: '<span><slot /></span>' },
        'a-alert': { template: '<div><slot name="message" /><slot name="description" /></div>' },
        'a-select': { template: '<div />' },
        'a-input': { template: '<input />' },
        'a-range-picker': { template: '<div />' },
        'a-empty': { template: '<div />' },
        'a-spin': { template: '<div><slot /></div>' },
        'a-tree': { template: '<div />' },
        'a-modal': {
          props: ['open'],
          emits: ['cancel'],
          template:
            '<section v-if="open" data-testid="oauth-qr-modal"><slot /><button data-testid="oauth-qr-close" @click="$emit(\'cancel\')">关闭</button></section>'
        },
        FeishuSyncRunsTable: true,
        FeishuMaterialTable: true,
        FeishuMaterialDetailDrawer: true
      }
    }
  })
}

function mountMaterialTable(props = {}) {
  return mount(FeishuMaterialTable, {
    props: {
      materials: [
        makeMaterial('version-page', { title: '产品手册' }),
        { version_id: 'version-audio', title: '客户访谈', item_type: 'audio' }
      ],
      ...props
    },
    global: {
      stubs: {
        'a-table': {
          name: 'ATable',
          props: ['rowSelection', 'dataSource'],
          template:
            '<div><section v-for="record in dataSource" :key="record.version_id" :data-version-id="record.version_id"><slot name="bodyCell" :column="{ key: \'action\' }" :record="record" /></section></div>'
        },
        'a-button': {
          props: ['disabled'],
          emits: ['click'],
          template: '<button :disabled="disabled" @click="$emit(\'click\')"><slot /></button>'
        },
        'a-tag': { template: '<span><slot /></span>' },
        'a-tooltip': { template: '<div><slot /></div>' },
        'a-dropdown': { template: '<div><slot /><slot name="overlay" /></div>' },
        'a-menu': { template: '<div><slot /></div>' },
        'a-menu-item': {
          props: ['disabled'],
          template: '<button class="menu-action" :disabled="disabled"><slot /></button>'
        }
      }
    }
  })
}

function mountDetailDrawer(props = {}) {
  return mount(FeishuMaterialDetailDrawer, {
    props: {
      open: true,
      material: {
        version_id: 'version-page',
        title: '产品手册',
        target_kb_id: 'kb-1',
        yuxi_file_id: 'file-1'
      },
      content: {
        content: '# 产品手册\n\n正文内容',
        lines: [
          { id: 'chunk-1', chunk_order_index: 0, content: '第一段知识' },
          { id: 'chunk-2', chunk_order_index: 1, content: '第二段知识' }
        ]
      },
      ...props
    },
    global: {
      stubs: {
        'a-drawer': { template: '<div><slot /></div>' },
        'a-descriptions': { template: '<div><slot /></div>' },
        'a-descriptions-item': { template: '<div><slot /></div>' },
        'a-tabs': { template: '<div><slot /></div>' },
        'a-tab-pane': { template: '<section><slot /></section>' },
        'a-timeline': { template: '<div><slot /></div>' },
        'a-timeline-item': { template: '<div><slot /></div>' },
        'a-empty': {
          props: ['description'],
          template: '<div>{{ description }}</div>'
        },
        'a-spin': { template: '<div><slot /></div>' },
        'a-skeleton': { template: '<div />' },
        'a-alert': {
          props: ['message'],
          template: '<div>{{ message }}</div>'
        },
        MarkdownPreview: {
          props: ['content'],
          template: '<article data-testid="markdown-preview">{{ content }}</article>'
        }
      }
    }
  })
}

describe('feishuKnowledgeApi', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('映射管理员 API，并编码路径和筛选参数', async () => {
    apiAdminGet.mockResolvedValue({ items: [] })
    apiAdminPatch.mockResolvedValue({})
    apiAdminPost.mockResolvedValue({})

    await feishuKnowledgeApi.listSources()
    await feishuKnowledgeApi.getOAuthStatus('source/1')
    await feishuKnowledgeApi.startOAuth('source/1', 'qr')
    await feishuKnowledgeApi.scanSource('source/1', 'incremental')
    await feishuKnowledgeApi.getRun('run/1')
    await feishuKnowledgeApi.listRuns('source/1')
    await feishuKnowledgeApi.listMaterials('source/1', {
      processing_status: 'failed',
      directory: '产品 资料',
      empty: ''
    })
    await feishuKnowledgeApi.rejectMaterial('version/1', '内容过期')
    await feishuKnowledgeApi.approveMaterial('version/1')
    await feishuKnowledgeApi.retryMaterial('version/1')
    await feishuKnowledgeApi.confirmRemoval('version/1')
    await feishuKnowledgeApi.getMaterial('version/1')
    await feishuKnowledgeApi.listMaterialEvents('version/1')
    await feishuKnowledgeApi.batchAction('approve', ['version/1', 'version/2'])

    expect(apiAdminGet).toHaveBeenNthCalledWith(1, '/api/feishu-knowledge/sources')
    expect(apiAdminGet).toHaveBeenCalledWith(
      '/api/feishu-knowledge/sources/source%2F1/oauth/status'
    )
    expect(apiAdminPost).toHaveBeenCalledWith(
      '/api/feishu-knowledge/sources/source%2F1/oauth/authorize',
      { mode: 'qr' }
    )
    expect(apiAdminPost).toHaveBeenCalledWith('/api/feishu-knowledge/sources/source%2F1/scan', {
      mode: 'incremental'
    })
    expect(apiAdminGet).toHaveBeenCalledWith('/api/feishu-knowledge/runs/run%2F1')
    expect(apiAdminGet).toHaveBeenCalledWith('/api/feishu-knowledge/sources/source%2F1/runs')
    expect(apiAdminGet).toHaveBeenCalledWith(
      '/api/feishu-knowledge/sources/source%2F1/materials?processing_status=failed&directory=%E4%BA%A7%E5%93%81+%E8%B5%84%E6%96%99'
    )
    expect(apiAdminPost).toHaveBeenCalledWith(
      '/api/feishu-knowledge/materials/version%2F1/reject',
      { reason: '内容过期' }
    )
    expect(apiAdminPost).toHaveBeenCalledWith(
      '/api/feishu-knowledge/materials/version%2F1/confirm-removal',
      {}
    )
    expect(apiAdminGet).toHaveBeenCalledWith('/api/feishu-knowledge/materials/version%2F1')
    expect(apiAdminGet).toHaveBeenCalledWith('/api/feishu-knowledge/materials/version%2F1/events')
    expect(apiAdminPost).toHaveBeenCalledWith('/api/feishu-knowledge/materials/batch-action', {
      action: 'approve',
      version_ids: ['version/1', 'version/2']
    })
  })

  it('在请求发出前拒绝超过 100 条的批量操作', async () => {
    await expect(
      feishuKnowledgeApi.batchAction(
        'approve',
        Array.from({ length: 101 }, (_, i) => `v-${i}`)
      )
    ).rejects.toThrow('单次批量操作最多选择 100 条素材')
    expect(apiAdminPost).not.toHaveBeenCalled()
  })

  it('把已知操作冲突和未知错误转换为中文提示', () => {
    expect(
      feishuKnowledgeApi.getErrorMessage(
        { response: { data: { detail: 'A scan is already running for this source' } } },
        '扫描失败'
      )
    ).toBe('该数据源已有扫描任务正在执行')
    expect(
      feishuKnowledgeApi.getErrorMessage(
        { response: { data: { detail: 'Only pending parsed material can be approved' } } },
        '审核失败'
      )
    ).toBe('仅待审核且已解析的素材可以审核通过')
    expect(
      feishuKnowledgeApi.getErrorMessage(
        { response: { data: { detail: 'Only failed material can be retried' } } },
        '重试失败'
      )
    ).toBe('仅处理失败的素材可以重试')
    expect(
      feishuKnowledgeApi.getErrorMessage(
        { response: { data: { detail: 'Material source must be invalid before removal' } } },
        '下架失败'
      )
    ).toBe('仅来源失效的素材可以确认下架')
    expect(
      feishuKnowledgeApi.getErrorMessage(
        { response: { data: { detail: 'unknown detail' } } },
        '操作失败'
      )
    ).toBe('操作失败')
    expect(
      feishuKnowledgeApi.getErrorMessage(
        { response: { data: { detail: '内容已被其他人更新' } } },
        '操作失败'
      )
    ).toBe('操作失败：内容已被其他人更新')
  })
})

describe('FeishuMaterialTable', () => {
  it('把 Ant Table 勾选键映射为批量操作的 versionIds', async () => {
    const wrapper = mountMaterialTable()
    const rowSelection = wrapper.findComponent({ name: 'ATable' }).props('rowSelection')

    rowSelection.onChange(['version-page'])
    await wrapper.vm.$nextTick()
    await wrapper
      .findAll('button')
      .find((button) => button.text() === '审核通过')
      .trigger('click')

    expect(wrapper.emitted('batch-action')).toEqual([
      [{ action: 'approve', versionIds: ['version-page'] }]
    ])
  })

  it('禁止勾选音视频，并在组件层限制批量选择数量', async () => {
    const wrapper = mountMaterialTable({ maxSelection: 1 })
    const rowSelection = wrapper.findComponent({ name: 'ATable' }).props('rowSelection')

    expect(
      rowSelection.getCheckboxProps({ version_id: 'version-audio', item_type: 'audio' })
    ).toEqual({
      disabled: true
    })
    expect(
      rowSelection.getCheckboxProps({ version_id: 'version-directory', item_type: 'directory' })
    ).toEqual({
      disabled: true
    })
    rowSelection.onChange(['version-page', 'version-extra'])
    await wrapper.vm.$nextTick()
    await wrapper
      .findAll('button')
      .find((button) => button.text() === '审核通过')
      .trigger('click')

    expect(wrapper.emitted('selection-limit')).toEqual([[1]])
    expect(wrapper.emitted('batch-action')).toEqual([
      [{ action: 'approve', versionIds: ['version-page'] }]
    ])
  })

  it('按后端状态机禁用单条素材操作', () => {
    const wrapper = mountMaterialTable({
      materials: [
        makeMaterial('reviewable', { title: '待审核素材' }),
        makeMaterial('missing-review-file', {
          title: '缺少待审核文件',
          yuxi_file_id: ''
        }),
        makeMaterial('retryable', {
          title: '解析失败素材',
          processing_status: 'parse_failed'
        }),
        makeMaterial('removable', {
          title: '可下架素材',
          processing_status: 'published',
          review_status: 'approved',
          source_validity: 'invalid',
          active: true
        }),
        makeMaterial('inactive-removal', {
          title: '非活跃素材',
          processing_status: 'published',
          review_status: 'approved',
          source_validity: 'invalid'
        }),
        makeMaterial('missing-file-removal', {
          title: '缺少知识库文件',
          processing_status: 'removal_failed',
          review_status: 'approved',
          source_validity: 'invalid',
          active: true,
          yuxi_file_id: ''
        }),
        makeMaterial('failed-removal', {
          title: '下架失败素材',
          processing_status: 'removal_failed',
          review_status: 'approved',
          source_validity: 'invalid',
          active: true
        })
      ]
    })

    const disabledActions = (versionId) =>
      Object.fromEntries(
        wrapper
          .get(`[data-version-id="${versionId}"]`)
          .findAll('.menu-action')
          .map((button) => [button.text(), button.attributes('disabled') !== undefined])
      )

    expect(disabledActions('reviewable')).toEqual({
      审核通过: false,
      驳回: false,
      重试: true,
      确认下架: true
    })
    expect(disabledActions('retryable')).toEqual({
      审核通过: true,
      驳回: false,
      重试: false,
      确认下架: true
    })
    expect(disabledActions('missing-review-file').审核通过).toBe(true)
    expect(disabledActions('missing-review-file').驳回).toBe(false)
    expect(disabledActions('removable')).toEqual({
      审核通过: true,
      驳回: true,
      重试: true,
      确认下架: false
    })
    expect(disabledActions('inactive-removal').确认下架).toBe(true)
    expect(disabledActions('missing-file-removal').确认下架).toBe(true)
    expect(disabledActions('failed-removal').确认下架).toBe(false)
  })

  it('仅当全部所选素材符合对应条件时启用四种批量操作', async () => {
    const wrapper = mountMaterialTable({
      materials: [
        makeMaterial('reviewable-1', {
          title: '待审核素材一',
          processing_status: 'parsed'
        }),
        makeMaterial('reviewable-2', { title: '待审核素材二' }),
        makeMaterial('rejectable-without-file', {
          title: '缺少知识库文件的待审核素材',
          yuxi_file_id: ''
        }),
        makeMaterial('retryable', {
          title: '发布失败素材',
          processing_status: 'publish_failed',
          review_status: 'approved'
        }),
        makeMaterial('retryable-2', {
          title: '解析失败素材',
          processing_status: 'parse_failed'
        }),
        makeMaterial('removable-1', {
          title: '可下架素材一',
          processing_status: 'published',
          review_status: 'approved',
          source_validity: 'invalid',
          active: true
        }),
        makeMaterial('removable-2', {
          title: '可下架素材二',
          processing_status: 'removal_failed',
          review_status: 'approved',
          source_validity: 'invalid',
          active: true
        })
      ]
    })
    const rowSelection = wrapper.findComponent({ name: 'ATable' }).props('rowSelection')
    const batchDisabledActions = () =>
      Object.fromEntries(
        wrapper
          .get('.batch-actions')
          .findAll('button')
          .map((button) => [button.text(), button.attributes('disabled') !== undefined])
      )

    rowSelection.onChange(['reviewable-1', 'retryable'])
    await wrapper.vm.$nextTick()
    expect(batchDisabledActions()).toEqual({
      审核通过: true,
      驳回: true,
      重试: true,
      确认下架: true
    })
    const approveButton = wrapper
      .get('.batch-actions')
      .findAll('button')
      .find((button) => button.text() === '审核通过')
    await approveButton.trigger('click')
    expect(wrapper.emitted('batch-action')).toBeUndefined()

    rowSelection.onChange(['retryable-2', 'rejectable-without-file'])
    await wrapper.vm.$nextTick()
    expect(batchDisabledActions()).toEqual({
      审核通过: true,
      驳回: false,
      重试: true,
      确认下架: true
    })
    const rejectButton = wrapper
      .get('.batch-actions')
      .findAll('button')
      .find((button) => button.text() === '驳回')
    await rejectButton.trigger('click')
    expect(wrapper.emitted('batch-action')).toEqual([
      [{ action: 'reject', versionIds: ['retryable-2', 'rejectable-without-file'] }]
    ])

    rowSelection.onChange(['reviewable-1', 'reviewable-2'])
    await wrapper.vm.$nextTick()
    expect(batchDisabledActions()).toEqual({
      审核通过: false,
      驳回: false,
      重试: true,
      确认下架: true
    })

    rowSelection.onChange(['retryable', 'retryable-2'])
    await wrapper.vm.$nextTick()
    expect(batchDisabledActions()).toEqual({
      审核通过: true,
      驳回: true,
      重试: false,
      确认下架: true
    })

    rowSelection.onChange(['removable-1', 'removable-2'])
    await wrapper.vm.$nextTick()
    expect(batchDisabledActions()).toEqual({
      审核通过: true,
      驳回: true,
      重试: true,
      确认下架: false
    })
  })
})

describe('FeishuMaterialDetailDrawer', () => {
  it('渲染 Markdown 正文和逐条 Chunks', () => {
    const wrapper = mountDetailDrawer()

    expect(wrapper.get('[data-testid="markdown-preview"]').text()).toContain('正文内容')
    expect(wrapper.text()).toContain('第一段知识')
    expect(wrapper.text()).toContain('第二段知识')
  })

  it('逐条保留两个完全重复的 Chunks', () => {
    const duplicateContent = '重复片段内容用于验证逐条展示不合并'
    const wrapper = mountDetailDrawer({
      content: {
        content: '',
        lines: [
          { id: 'chunk-1', chunk_order_index: 0, content: duplicateContent },
          { id: 'chunk-2', chunk_order_index: 1, content: duplicateContent }
        ]
      }
    })

    expect(wrapper.findAll('.chunk-item')).toHaveLength(2)
    expect(wrapper.get('.metric-line strong').text()).toBe('2')
  })

  it('没有知识库文件 ID 时显示真实空态', () => {
    const wrapper = mountDetailDrawer({
      material: { version_id: 'version-new', title: '待加工素材' },
      content: { content: '', lines: [] }
    })

    expect(wrapper.text()).toContain('尚未生成可预览内容')
  })

  it('目录节点明确说明不进入知识库且无需生成正文', () => {
    const wrapper = mountDetailDrawer({
      material: { version_id: 'version-directory', title: '产品资料', is_directory: true },
      content: { content: '', lines: [] }
    })

    expect(wrapper.text()).toContain('目录节点不进入知识库')
    expect(wrapper.text()).toContain('目录节点仅用于组织下级内容，无需生成正文')
    expect(wrapper.text()).toContain('目录节点不参与知识分块和检索')
  })
})

describe('FeishuKnowledgeView', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    vi.clearAllMocks()
    sessionStorage.clear()
    QRCode.toDataURL.mockResolvedValue('data:image/png;base64,oauth-qr')
    apiAdminGet.mockImplementation((url) => {
      if (url === '/api/feishu-knowledge/sources') return Promise.resolve({ items: [source] })
      if (url.endsWith('/oauth/status'))
        return Promise.resolve({ authorized: true, status: 'active' })
      if (url.endsWith('/runs')) return Promise.resolve({ items: [] })
      if (url.includes('/materials')) return Promise.resolve({ items: [] })
      return Promise.resolve({ status: 'succeeded' })
    })
  })

  afterEach(() => {
    vi.useRealTimers()
    sessionStorage.clear()
  })

  it('首次进入知识加工页面时立即显示真实待审核数量', async () => {
    apiAdminGet.mockImplementation((url) => {
      if (url === '/api/feishu-knowledge/sources') return Promise.resolve({ items: [source] })
      if (url.endsWith('/oauth/status')) {
        return Promise.resolve({ authorized: true, status: 'active' })
      }
      if (url === '/api/governance/review-packages?source_id=source-1&view=mine') {
        return Promise.resolve({ items: [], total: 2, counts: { mine: 2 } })
      }
      if (url.endsWith('/runs')) return Promise.resolve({ items: [] })
      if (url.includes('/materials')) return Promise.resolve({ items: [] })
      return Promise.resolve({ nodes: [] })
    })

    const wrapper = mountView()
    await flushPromises()

    const reviewTab = wrapper
      .findAll('.governance-tab')
      .find((tab) => tab.text().includes('待审核'))
    expect(reviewTab.text()).toContain('2')
    expect(apiAdminGet).toHaveBeenCalledWith(
      '/api/governance/review-packages?source_id=source-1&view=mine'
    )
    wrapper.unmount()
  })

  it('首次进入页面时立即显示跨文档关系总数和正式知识数', async () => {
    apiAdminGet.mockImplementation((url) => {
      if (url === '/api/feishu-knowledge/sources') return Promise.resolve({ items: [source] })
      if (url.endsWith('/oauth/status'))
        return Promise.resolve({ authorized: true, status: 'active' })
      if (url === '/api/governance/comparisons/status?source_id=source-1') {
        return Promise.resolve({ relation_count: 838, issue_count: 2 })
      }
      if (url === '/api/governance/knowledge?source_id=source-1') {
        return Promise.resolve({
          items: [{ knowledge_id: 'knowledge-1' }, { knowledge_id: 'knowledge-2' }]
        })
      }
      if (url.endsWith('/runs')) return Promise.resolve({ items: [] })
      if (url.includes('/materials')) return Promise.resolve({ items: [] })
      if (url === '/api/governance/review-packages?source_id=source-1&view=mine')
        return Promise.resolve({ items: [], total: 0, counts: { mine: 0 } })
      return Promise.resolve({ nodes: [] })
    })

    const wrapper = mountView()
    await flushPromises()

    const relationTab = wrapper
      .findAll('.governance-tab')
      .find((tab) => tab.text().includes('跨文档检查'))
    const formalTab = wrapper
      .findAll('.governance-tab')
      .find((tab) => tab.text().includes('正式知识'))
    expect(relationTab.text()).toContain('838')
    expect(formalTab.text()).toContain('2')
    expect(apiAdminGet).toHaveBeenCalledWith('/api/governance/knowledge?source_id=source-1')
    wrapper.unmount()
  })

  it('重新进入页面时复用未过期的知识目录缓存', async () => {
    let treeRequests = 0
    apiAdminGet.mockImplementation((url) => {
      if (url === '/api/feishu-knowledge/sources') return Promise.resolve({ items: [source] })
      if (url.endsWith('/oauth/status'))
        return Promise.resolve({ authorized: true, status: 'active' })
      if (url.endsWith('/tree')) {
        treeRequests += 1
        return Promise.resolve({
          nodes: [{ node_token: 'root', title: '产品资料', children: [] }]
        })
      }
      if (url.endsWith('/runs')) return Promise.resolve({ items: [] })
      if (url.includes('/materials')) return Promise.resolve({ items: [] })
      return Promise.resolve({})
    })

    const firstWrapper = mountView()
    await flushPromises()
    firstWrapper.unmount()

    const secondWrapper = mountView()
    await flushPromises()

    expect(treeRequests).toBe(1)
    secondWrapper.unmount()
  })

  it('点击刷新目录时绕过缓存并重新请求飞书目录', async () => {
    let treeRequests = 0
    apiAdminGet.mockImplementation((url) => {
      if (url === '/api/feishu-knowledge/sources') return Promise.resolve({ items: [source] })
      if (url.endsWith('/oauth/status'))
        return Promise.resolve({ authorized: true, status: 'active' })
      if (url.endsWith('/tree')) {
        treeRequests += 1
        return Promise.resolve({ nodes: [] })
      }
      if (url.endsWith('/runs')) return Promise.resolve({ items: [] })
      if (url.includes('/materials')) return Promise.resolve({ items: [] })
      return Promise.resolve({})
    })

    const wrapper = mountView()
    await flushPromises()
    expect(treeRequests).toBe(1)

    await wrapper.get('[aria-label="刷新知识目录"]').trigger('click')
    await flushPromises()

    expect(treeRequests).toBe(2)
    wrapper.unmount()
  })

  it('扫描提交期间显示 loading，并在活跃批次存在时互斥禁用两个按钮', async () => {
    let resolveScan
    apiAdminPost.mockImplementation(() => new Promise((resolve) => (resolveScan = resolve)))
    const wrapper = mountView()
    await flushPromises()

    const fullButton = wrapper.get('[data-testid="scan-full"]')
    const incrementalButton = wrapper.get('[data-testid="scan-incremental"]')
    await fullButton.trigger('click')

    expect(fullButton.attributes('data-loading')).toBe('true')
    expect(incrementalButton.attributes('disabled')).toBeDefined()

    resolveScan({ run_id: 'run-1', status: 'queued' })
    await flushPromises()
    expect(wrapper.get('[data-testid="scan-full"]').attributes('disabled')).toBeDefined()
    expect(wrapper.get('[data-testid="scan-incremental"]').attributes('disabled')).toBeDefined()
  })

  it('未授权飞书用户时禁止扫描并提供授权入口', async () => {
    apiAdminGet.mockImplementation((url) => {
      if (url === '/api/feishu-knowledge/sources') return Promise.resolve({ items: [source] })
      if (url.endsWith('/oauth/status')) {
        return Promise.resolve({ authorized: false, status: 'not_authorized' })
      }
      if (url.endsWith('/runs')) return Promise.resolve({ items: [] })
      if (url.includes('/materials')) return Promise.resolve({ items: [] })
      return Promise.resolve({ nodes: [] })
    })

    const wrapper = mountView()
    await flushPromises()

    expect(wrapper.get('[data-testid="oauth-qr-authorize"]').text()).toContain('扫码授权')
    expect(wrapper.get('[data-testid="scan-full"]').attributes('disabled')).toBeDefined()
    expect(wrapper.get('[data-testid="scan-incremental"]').attributes('disabled')).toBeDefined()
  })

  it('在管理页生成新版 OAuth 二维码，不依赖第三方二维码服务', async () => {
    apiAdminPost.mockResolvedValue({
      authorization_url: 'https://accounts.feishu.cn/open-apis/authen/v1/authorize?state=qr-state',
      started_at: '2026-08-17T08:00:00+00:00'
    })
    const wrapper = mountView()
    await flushPromises()

    await wrapper.get('[data-testid="oauth-qr-authorize"]').trigger('click')
    await flushPromises()

    expect(apiAdminPost).toHaveBeenCalledWith(
      '/api/feishu-knowledge/sources/source-1/oauth/authorize',
      { mode: 'qr' }
    )
    expect(QRCode.toDataURL).toHaveBeenCalledWith(
      expect.stringContaining('https://accounts.feishu.cn/open-apis/authen/v1/authorize'),
      expect.objectContaining({ width: 232, margin: 1 })
    )
    expect(wrapper.get('[data-testid="oauth-qr-image"]').attributes('src')).toBe(
      'data:image/png;base64,oauth-qr'
    )
    expect(wrapper.get('[data-testid="oauth-qr-modal"]').text()).toContain('请使用手机飞书扫码')
  })

  it('手机完成扫码授权后自动关闭二维码并刷新授权状态和目录', async () => {
    let statusRequests = 0
    apiAdminGet.mockImplementation((url) => {
      if (url === '/api/feishu-knowledge/sources') return Promise.resolve({ items: [source] })
      if (url.endsWith('/oauth/status')) {
        statusRequests += 1
        if (statusRequests === 1) {
          return Promise.resolve({ authorized: false, status: 'not_authorized' })
        }
        return Promise.resolve({
          authorized: true,
          status: 'active',
          display_name: '知识库管理员',
          last_refreshed_at: '2026-08-17T08:00:05+00:00'
        })
      }
      if (url.endsWith('/tree')) return Promise.resolve({ nodes: [] })
      if (url.endsWith('/runs')) return Promise.resolve({ items: [] })
      if (url.includes('/materials')) return Promise.resolve({ items: [] })
      return Promise.resolve({})
    })
    apiAdminPost.mockResolvedValue({
      authorization_url: 'https://accounts.feishu.cn/open-apis/authen/v1/authorize?state=qr-state',
      started_at: '2026-08-17T08:00:00+00:00'
    })
    const wrapper = mountView()
    await flushPromises()

    await wrapper.get('[data-testid="oauth-qr-authorize"]').trigger('click')
    await flushPromises()
    await vi.advanceTimersByTimeAsync(2000)
    await flushPromises()
    await vi.advanceTimersByTimeAsync(800)
    await flushPromises()

    expect(wrapper.find('[data-testid="oauth-qr-modal"]').exists()).toBe(false)
    expect(message.success).toHaveBeenCalledWith('飞书用户授权成功，知识目录已刷新')
    expect(apiAdminGet).toHaveBeenCalledWith('/api/feishu-knowledge/sources/source-1/tree')
  })

  it('轮询批次到终态后刷新来源、批次和素材', async () => {
    apiAdminPost.mockResolvedValue({ run_id: 'run-1', status: 'queued' })
    apiAdminGet.mockImplementation((url) => {
      if (url === '/api/feishu-knowledge/sources') return Promise.resolve({ items: [source] })
      if (url.endsWith('/oauth/status'))
        return Promise.resolve({ authorized: true, status: 'active' })
      if (url.endsWith('/runs/run-1'))
        return Promise.resolve({ run_id: 'run-1', status: 'succeeded' })
      if (url.endsWith('/runs')) return Promise.resolve({ items: [] })
      if (url.includes('/materials')) return Promise.resolve({ items: [] })
      return Promise.resolve({})
    })
    const wrapper = mountView()
    await flushPromises()
    await wrapper.get('[data-testid="scan-incremental"]').trigger('click')
    await flushPromises()
    await vi.advanceTimersByTimeAsync(2000)
    await flushPromises()

    expect(apiAdminGet).toHaveBeenCalledWith('/api/feishu-knowledge/runs/run-1')
    expect(
      apiAdminGet.mock.calls.filter(([url]) => url === '/api/feishu-knowledge/sources')
    ).toHaveLength(2)
    expect(message.success).toHaveBeenCalledWith('增量扫描完成')
  })

  it('进度和批次列表连续失败后仍轮询同一批次直至终态', async () => {
    let runRequests = 0
    let listRunRequests = 0
    apiAdminPost.mockResolvedValue({ run_id: 'run-retry', status: 'queued' })
    apiAdminGet.mockImplementation((url) => {
      if (url === '/api/feishu-knowledge/sources') return Promise.resolve({ items: [source] })
      if (url.endsWith('/oauth/status'))
        return Promise.resolve({ authorized: true, status: 'active' })
      if (url.endsWith('/runs/run-retry')) {
        runRequests += 1
        if (runRequests <= 2) return Promise.reject(new Error('temporary network error'))
        return Promise.resolve({
          run_id: 'run-retry',
          run_type: 'incremental',
          status: 'succeeded'
        })
      }
      if (url.endsWith('/runs')) {
        listRunRequests += 1
        if (listRunRequests === 2 || listRunRequests === 3) {
          return Promise.reject(new Error('temporary list error'))
        }
        return Promise.resolve({ items: [] })
      }
      if (url.includes('/materials')) return Promise.resolve({ items: [] })
      return Promise.resolve({})
    })
    const wrapper = mountView()
    await flushPromises()
    await wrapper.get('[data-testid="scan-incremental"]').trigger('click')
    await flushPromises()

    await vi.advanceTimersByTimeAsync(2000)
    await flushPromises()
    expect(wrapper.get('[data-testid="scan-incremental"]').attributes('disabled')).toBeDefined()
    await vi.advanceTimersByTimeAsync(2000)
    await flushPromises()
    expect(wrapper.get('[data-testid="scan-full"]').attributes('disabled')).toBeDefined()
    await vi.advanceTimersByTimeAsync(2000)
    await flushPromises()

    expect(runRequests).toBe(3)
    expect(message.success).toHaveBeenCalledWith('增量扫描完成')
    expect(wrapper.get('[data-testid="scan-incremental"]').attributes('disabled')).toBeUndefined()
  })

  it('卸载后忽略在途轮询响应且不再调度', async () => {
    let resolveFirstRun
    let runRequests = 0
    apiAdminPost.mockResolvedValue({ run_id: 'run-unmount', status: 'queued' })
    apiAdminGet.mockImplementation((url) => {
      if (url === '/api/feishu-knowledge/sources') return Promise.resolve({ items: [source] })
      if (url.endsWith('/oauth/status'))
        return Promise.resolve({ authorized: true, status: 'active' })
      if (url.endsWith('/runs/run-unmount')) {
        runRequests += 1
        if (runRequests === 1) {
          return new Promise((resolve) => (resolveFirstRun = resolve))
        }
        return Promise.resolve({
          run_id: 'run-unmount',
          run_type: 'incremental',
          status: 'succeeded'
        })
      }
      if (url.endsWith('/runs')) return Promise.resolve({ items: [] })
      if (url.includes('/materials')) return Promise.resolve({ items: [] })
      return Promise.resolve({})
    })
    const wrapper = mountView()
    await flushPromises()
    await wrapper.get('[data-testid="scan-incremental"]').trigger('click')
    await flushPromises()
    vi.advanceTimersByTime(2000)
    expect(runRequests).toBe(1)

    wrapper.unmount()
    resolveFirstRun({
      run_id: 'run-unmount',
      run_type: 'incremental',
      status: 'running'
    })
    await flushPromises()
    await vi.advanceTimersByTimeAsync(10000)
    await flushPromises()

    expect(runRequests).toBe(1)
    expect(
      apiAdminGet.mock.calls.filter(([url]) => url === '/api/feishu-knowledge/sources')
    ).toHaveLength(1)
    expect(message.success).not.toHaveBeenCalledWith('增量扫描完成')
  })

  it('审核通过前要求确认，确认后调用接口并刷新素材', async () => {
    Modal.confirm.mockImplementation(({ onOk }) => onOk())
    apiAdminPost.mockResolvedValue({ status: 'publish_queued' })
    const wrapper = mountView()
    await flushPromises()

    wrapper.findComponent({ name: 'FeishuMaterialTable' }).vm.$emit('action', {
      action: 'approve',
      material: { version_id: 'version-1', title: '产品手册' }
    })
    await flushPromises()

    expect(Modal.confirm).toHaveBeenCalledWith(
      expect.objectContaining({ title: '确认审核通过“产品手册”？' })
    )
    expect(apiAdminPost).toHaveBeenCalledWith(
      '/api/feishu-knowledge/materials/version-1/approve',
      {}
    )
  })

  it('打开详情后使用知识库和文件 ID 加载正文及 Chunks', async () => {
    apiAdminGet.mockImplementation((url) => {
      if (url === '/api/feishu-knowledge/sources') return Promise.resolve({ items: [source] })
      if (url.endsWith('/oauth/status'))
        return Promise.resolve({ authorized: true, status: 'active' })
      if (url.endsWith('/runs')) return Promise.resolve({ items: [] })
      if (url.includes('/sources/source-1/materials')) return Promise.resolve({ items: [] })
      if (url.endsWith('/materials/version-1')) {
        return Promise.resolve({
          version_id: 'version-1',
          title: '产品手册',
          target_kb_id: 'kb-1',
          yuxi_file_id: 'file-1'
        })
      }
      if (url.endsWith('/materials/version-1/events')) return Promise.resolve({ items: [] })
      return Promise.resolve({})
    })
    documentApi.getDocumentContent.mockResolvedValue({
      status: 'success',
      content: '# 产品手册',
      lines: [{ id: 'chunk-1', chunk_order_index: 0, content: '产品正文' }]
    })
    const wrapper = mountView()
    await flushPromises()

    wrapper.findComponent({ name: 'FeishuMaterialTable' }).vm.$emit('open-detail', {
      version_id: 'version-1'
    })
    await flushPromises()

    expect(documentApi.getDocumentContent).toHaveBeenCalledWith('kb-1', 'file-1')
    expect(wrapper.findComponent({ name: 'FeishuMaterialDetailDrawer' }).props('content')).toEqual(
      expect.objectContaining({ content: '# 产品手册' })
    )
  })

  it('素材没有知识库文件 ID 时不请求正文', async () => {
    apiAdminGet.mockImplementation((url) => {
      if (url === '/api/feishu-knowledge/sources') return Promise.resolve({ items: [source] })
      if (url.endsWith('/oauth/status'))
        return Promise.resolve({ authorized: true, status: 'active' })
      if (url.endsWith('/runs')) return Promise.resolve({ items: [] })
      if (url.includes('/sources/source-1/materials')) return Promise.resolve({ items: [] })
      if (url.endsWith('/materials/version-new')) {
        return Promise.resolve({
          version_id: 'version-new',
          title: '待加工素材',
          target_kb_id: 'kb-1'
        })
      }
      if (url.endsWith('/materials/version-new/events')) return Promise.resolve({ items: [] })
      return Promise.resolve({})
    })
    const wrapper = mountView()
    await flushPromises()

    wrapper.findComponent({ name: 'FeishuMaterialTable' }).vm.$emit('open-detail', {
      version_id: 'version-new'
    })
    await flushPromises()

    expect(documentApi.getDocumentContent).not.toHaveBeenCalled()
  })

  it('正文接口返回失败状态时保留详情并显示中文错误', async () => {
    apiAdminGet.mockImplementation((url) => {
      if (url === '/api/feishu-knowledge/sources') return Promise.resolve({ items: [source] })
      if (url.endsWith('/oauth/status'))
        return Promise.resolve({ authorized: true, status: 'active' })
      if (url.endsWith('/runs')) return Promise.resolve({ items: [] })
      if (url.includes('/sources/source-1/materials')) return Promise.resolve({ items: [] })
      if (url.endsWith('/materials/version-1')) {
        return Promise.resolve({
          version_id: 'version-1',
          title: '产品手册',
          target_kb_id: 'kb-1',
          yuxi_file_id: 'file-1'
        })
      }
      if (url.endsWith('/materials/version-1/events')) return Promise.resolve({ items: [] })
      return Promise.resolve({})
    })
    documentApi.getDocumentContent.mockResolvedValue({
      status: 'failed',
      message: '解析内容读取失败'
    })
    const wrapper = mountView()
    await flushPromises()

    wrapper.findComponent({ name: 'FeishuMaterialTable' }).vm.$emit('open-detail', {
      version_id: 'version-1'
    })
    await flushPromises()

    const drawer = wrapper.findComponent({ name: 'FeishuMaterialDetailDrawer' })
    expect(drawer.props('material')).toEqual(expect.objectContaining({ title: '产品手册' }))
    expect(drawer.props('content')).toEqual(
      expect.objectContaining({ error: '加载解析内容失败：解析内容读取失败' })
    )
  })

  it('关闭详情后清空状态并忽略仍在途的正文响应', async () => {
    let resolveContent
    apiAdminGet.mockImplementation((url) => {
      if (url === '/api/feishu-knowledge/sources') return Promise.resolve({ items: [source] })
      if (url.endsWith('/oauth/status'))
        return Promise.resolve({ authorized: true, status: 'active' })
      if (url.endsWith('/runs')) return Promise.resolve({ items: [] })
      if (url.includes('/sources/source-1/materials')) return Promise.resolve({ items: [] })
      if (url.endsWith('/materials/version-1')) {
        return Promise.resolve({
          version_id: 'version-1',
          title: '产品手册',
          target_kb_id: 'kb-1',
          yuxi_file_id: 'file-1'
        })
      }
      if (url.endsWith('/materials/version-1/events')) return Promise.resolve({ items: [] })
      return Promise.resolve({})
    })
    documentApi.getDocumentContent.mockImplementation(
      () => new Promise((resolve) => (resolveContent = resolve))
    )
    const wrapper = mountView()
    await flushPromises()

    wrapper.findComponent({ name: 'FeishuMaterialTable' }).vm.$emit('open-detail', {
      version_id: 'version-1'
    })
    await flushPromises()
    wrapper.findComponent({ name: 'FeishuMaterialDetailDrawer' }).vm.$emit('close')
    await flushPromises()

    let drawer = wrapper.findComponent({ name: 'FeishuMaterialDetailDrawer' })
    expect(drawer.props()).toEqual(
      expect.objectContaining({ open: false, material: null, events: [], loading: false })
    )
    expect(drawer.props('content')).toEqual(
      expect.objectContaining({ content: '', lines: [], loading: false, error: '' })
    )

    resolveContent({ status: 'success', content: '不应写回', lines: [] })
    await flushPromises()

    drawer = wrapper.findComponent({ name: 'FeishuMaterialDetailDrawer' })
    expect(drawer.props('content')).toEqual(
      expect.objectContaining({ content: '', lines: [], loading: false, error: '' })
    )
  })

  it('接口失败时显示中文错误提示', async () => {
    apiAdminPost.mockRejectedValue({
      response: { data: { detail: 'A scan is already running for this source' } }
    })
    const wrapper = mountView()
    await flushPromises()
    await wrapper.get('[data-testid="scan-full"]').trigger('click')
    await flushPromises()

    expect(message.error).toHaveBeenCalledWith('该数据源已有扫描任务正在执行')
  })
})
