// @vitest-environment jsdom
import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, expect, it, vi } from 'vitest'
import RolePermissionsComponent from './RolePermissionsComponent.vue'

const mocks = vi.hoisted(() => ({ list: vi.fn(), save: vi.fn(), refresh: vi.fn() }))
vi.mock('@/apis/role_permission_api', () => ({
  rolePermissionApi: { list: mocks.list, setFeedbackScope: mocks.save }
}))
vi.mock('@/stores/user', () => ({ useUserStore: () => ({ refreshPermissions: mocks.refresh }) }))
vi.mock('ant-design-vue', () => ({ message: { success: vi.fn(), error: vi.fn() } }))
const stubs = {
  'a-spin': { template: '<div><slot /></div>' },
  'a-form': { emits: ['finish'], template: '<form @submit.prevent="$emit(\'finish\')"><slot /></form>' },
  'a-form-item': { template: '<div><slot /></div>' },
  'a-button': { template: '<button type="submit"><slot /></button>' },
  'a-alert': { props: ['message'], template: '<div role="alert">{{ message }}</div>' },
  'a-select': {
    props: ['value', 'options'], emits: ['update:value'],
    template: '<select :value="value" @change="$emit(\'update:value\', $event.target.value)"><option v-for="item in options" :key="item.value" :value="item.value">{{ item.label }}</option></select>'
  }
}
beforeEach(() => {
  vi.clearAllMocks()
  mocks.list.mockResolvedValue({ admin: 'none' })
  mocks.save.mockResolvedValue({ scope: 'department' })
})
it('加载角色配置并保存本部门权限', async () => {
  const wrapper = mount(RolePermissionsComponent, { global: { stubs } })
  await flushPromises()
  expect(wrapper.get('select').element.value).toBe('none')
  await wrapper.get('select').setValue('department')
  await wrapper.get('form').trigger('submit')
  await flushPromises()
  expect(mocks.save).toHaveBeenCalledWith('department')
  expect(mocks.refresh).toHaveBeenCalledOnce()
  wrapper.unmount()
})
it('配置加载失败时不允许使用默认值覆盖权限', async () => {
  mocks.list.mockRejectedValue(new Error('network'))
  const wrapper = mount(RolePermissionsComponent, { global: { stubs } })
  await flushPromises()
  expect(wrapper.get('[role="alert"]').text()).toContain('加载失败')
  expect(wrapper.find('form').exists()).toBe(false)
  expect(mocks.save).not.toHaveBeenCalled()
  wrapper.unmount()
})
