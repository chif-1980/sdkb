// @vitest-environment jsdom
import { shallowMount } from '@vue/test-utils'
import { beforeEach, expect, it, vi } from 'vitest'
import ExtensionsView from './ExtensionsView.vue'
import ModelManageView from './ModelManageView.vue'
const state = vi.hoisted(() => ({ grants: [], query: {}, replace: vi.fn() }))
vi.mock('@/stores/user', () => ({ useUserStore: () => ({ isAdmin: false, hasPermission: (permission) => state.grants.includes(permission) }) }))
vi.mock('vue-router', () => ({ useRoute: () => ({ path: '/extensions', query: state.query }), useRouter: () => ({ replace: state.replace }) }))
vi.mock('@/components/extensions/ToolsCardList.vue', () => ({ default: { template: '<div />' } }))
vi.mock('@/components/extensions/McpCardList.vue', () => ({ default: { template: '<div />' } }))
vi.mock('@/components/extensions/SkillCardList.vue', () => ({ default: { template: '<div />' } }))
vi.mock('@/views/DataBaseView.vue', () => ({ default: { template: '<div />' } }))
vi.mock('@/components/model-management/AgentManagePanel.vue', () => ({ default: { template: '<div />' } }))
vi.mock('@/components/model-management/ModelProviderManagePanel.vue', () => ({ default: { template: '<div />' } }))
beforeEach(() => { state.grants = []; state.query = {}; vi.clearAllMocks() })
it('知识库页签按子能力授权显示，普通账号也可进入', () => {
  state.grants = ['extensions.view', 'knowledge.view']
  let wrapper = shallowMount(ExtensionsView)
  expect(wrapper.findComponent({ name: 'PageHeader' }).props('tabs').map((t) => t.key)).toContain('knowledge')
  wrapper.unmount()
  state.grants = ['extensions.view']
  state.query = { tab: 'knowledge' }
  wrapper = shallowMount(ExtensionsView)
  expect(wrapper.findComponent({ name: 'PageHeader' }).props('tabs').map((t) => t.key)).not.toContain('knowledge')
  expect(state.replace).toHaveBeenCalled()
  wrapper.unmount()
})
it('模型供应商页签按 models.view 授权，移除后回到智能体', () => {
  state.grants = ['agents.view', 'models.view']
  state.query = { tab: 'providers' }
  let wrapper = shallowMount(ModelManageView)
  expect(wrapper.findComponent({ name: 'PageHeader' }).props('tabs').map((t) => t.key)).toContain('providers')
  wrapper.unmount()
  state.grants = ['agents.view']
  wrapper = shallowMount(ModelManageView)
  expect(wrapper.findComponent({ name: 'PageHeader' }).props('activeKey')).toBe('agents')
  expect(wrapper.findComponent({ name: 'PageHeader' }).props('tabs').map((t) => t.key)).not.toContain('providers')
  wrapper.unmount()
})
