// @vitest-environment jsdom
import { mount, flushPromises } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, expect, it, vi } from 'vitest'
import { message } from 'ant-design-vue'
import { configApi } from '@/apis/system_api'
import { useConfigStore } from '@/stores/config'
import Panel from './ModelAssignmentsPanel.vue'

vi.mock('@/apis/system_api', () => ({ configApi: { getConfig: vi.fn(), updateConfigBatch: vi.fn() } }))
vi.mock('ant-design-vue', () => ({ message: { success: vi.fn(), error: vi.fn() } }))
const initial = { default_model: 'p:old' }
const mountPanel = () => mount(Panel, {
  props: { providers: [{ provider_id: 'p', display_name: '供应商', is_enabled: true,
    enabled_models: [{ id: 'old', type: 'chat' }, { id: 'new', type: 'chat' }] }] },
  global: { stubs: {
    'a-spin': { template: '<div><slot /></div>' },
    'a-alert': { template: '<div><slot name="action" /></div>' },
    'a-button': { template: '<button><slot /></button>' },
    'a-select': { props: ['value', 'options'], emits: ['update:value'],
      template: '<select :value="value" @change="$emit(\'update:value\', $event.target.value)"><option v-for="o in options" :value="o.value">{{ o.label }}</option></select>' }
  } }
})
beforeEach(() => {
  setActivePinia(createPinia())
  vi.clearAllMocks()
  configApi.getConfig.mockResolvedValue({ ...initial })
})
it('保存成功后更新共享配置，重开页面恢复已保存的值', async () => {
  configApi.updateConfigBatch.mockResolvedValue({ default_model: 'p:new' })
  const wrapper = mountPanel()
  await flushPromises()
  await wrapper.get('#assignment-default_model').setValue('p:new')
  await wrapper.get('button').trigger('click')
  await flushPromises()
  expect(configApi.updateConfigBatch).toHaveBeenCalledWith({ default_model: 'p:new' })
  expect(useConfigStore().config.default_model).toBe('p:new')
  expect(message.success).toHaveBeenCalledOnce()
  configApi.getConfig.mockResolvedValue({ default_model: 'p:new' })
  wrapper.unmount()
  const reopened = mountPanel()
  await flushPromises()
  expect(reopened.get('#assignment-default_model').element.value).toBe('p:new')
})
it('保存失败时保留草稿供重试，实际配置不变', async () => {
  configApi.updateConfigBatch.mockRejectedValue(new Error('保存失败'))
  const wrapper = mountPanel()
  await flushPromises()
  await wrapper.get('#assignment-default_model').setValue('p:new')
  await wrapper.get('button').trigger('click')
  await flushPromises()
  expect(useConfigStore().config.default_model).toBe('p:old')
  expect(wrapper.get('#assignment-default_model').element.value).toBe('p:new')
  expect(message.error).toHaveBeenCalled()
  expect(message.success).not.toHaveBeenCalled()
  expect(wrapper.get('button').attributes('disabled')).toBeUndefined()
})
