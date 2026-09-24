// @vitest-environment jsdom
import { flushPromises, mount } from '@vue/test-utils'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import RolePermissionsComponent from './RolePermissionsComponent.vue'

const mocks = vi.hoisted(() => ({
  isSuperAdmin: true,
  list: vi.fn(),
  save: vi.fn(),
  refresh: vi.fn(),
  create: vi.fn(),
  preview: vi.fn(),
  user: vi.fn(),
  department: vi.fn()
}))
vi.mock('@/apis/role_permission_api', () => ({
  rolePermissionApi: {
    list: mocks.list,
    setRolePermissions: mocks.save,
    create: mocks.create,
    preview: mocks.preview,
    remove: vi.fn(),
    setUserRoles: mocks.user,
    setDepartmentRoles: mocks.department
  }
}))
vi.mock('@/stores/user', () => ({
  useUserStore: () => ({
    refreshPermissions: mocks.refresh,
    get isSuperAdmin() {
      return mocks.isSuperAdmin
    }
  })
}))
vi.mock('vue-router', () => ({ onBeforeRouteLeave: vi.fn() }))
vi.mock('ant-design-vue', () => ({
  message: { success: vi.fn(), error: vi.fn(), warning: vi.fn() },
  Modal: { confirm: ({ onOk, onCancel }) => (window.confirm() ? onOk() : onCancel()) }
}))
const stubs = {
  'a-spin': { template: '<div><slot /></div>' },
  'a-alert': { props: ['message'], template: '<div role="alert">{{ message }}</div>' }
}
let payload, wrapper
const tab = async (name) => {
  await wrapper
    .findAll('.permission-tabs button')
    .find((button) => button.text() === name)
    .trigger('click')
  await flushPromises()
}
const start = async () => {
  wrapper = mount(RolePermissionsComponent, { global: { stubs } })
  await flushPromises()
}
beforeEach(() => {
  vi.clearAllMocks()
  mocks.isSuperAdmin = true
  vi.spyOn(window, 'confirm').mockReturnValue(true)
  payload = {
    modules: {
      workspace: '工作区',
      agents: '智能体管理',
      extensions: '扩展管理',
      knowledge: '知识库',
      feishu_knowledge: '飞书知识源',
      meetings: '会议管理',
      feedback: '用户反馈',
      dashboard: '数据总览',
      admin: '系统管理'
    },
    scopes: ['none', 'self', 'department', 'all'],
    roles: [
      {
        role_key: 'reviewer',
        name: '审阅员',
        is_builtin: false,
        permissions: {},
        user_ids: [],
        departments: []
      },
      {
        role_key: 'other',
        name: '其他角色',
        is_builtin: false,
        permissions: {},
        user_ids: [1],
        departments: [{ id: 1 }]
      },
      {
        role_key: 'superadmin',
        name: '超级管理员',
        is_builtin: true,
        permissions: { 'admin.manage': 'all', 'admin.view': 'all' },
        user_ids: [],
        departments: []
      }
    ],
    users: [{ id: 1, username: '成员', uid: 'member', role: 'user' }],
    feishu_departments: [
      { id: 1, name: '研发部', tenant_key: 'tenant', feishu_department_id: 'dept-1', user_ids: [1] }
    ]
  }
  mocks.list.mockImplementation(async () => structuredClone(payload))
  mocks.save.mockResolvedValue({})
  mocks.preview.mockResolvedValue({
    permissions: { 'feedback.view': 'department' },
    sources: [
      {
        kind: 'department',
        name: '审阅员',
        department_name: '研发部',
        permissions: { 'feedback.view': 'department' }
      }
    ]
  })
})
afterEach(() => {
  wrapper?.unmount()
  vi.restoreAllMocks()
})
it('按主菜单配置角色，保存权限和并发快照', async () => {
  await start()
  expect(wrapper.text()).toContain('与主菜单一一对应')
  expect(wrapper.text()).toContain('智能体扩展')
  await wrapper.get('[aria-label="用户反馈权限"]').setValue('1')
  await tab('数据范围')
  await wrapper.get('input[value="department"]').setValue(true)
  await wrapper.get('.save-bar .primary-button').trigger('click')
  await flushPromises()
  expect(mocks.save).toHaveBeenCalledWith(
    'reviewer',
    expect.objectContaining({
      permissions: expect.objectContaining({
        'feedback.view': 'department',
        'feedback.manage': 'none'
      }),
      expected_permissions: expect.objectContaining({ 'feedback.view': 'none' })
    })
  )
  expect(mocks.refresh).toHaveBeenCalledOnce()
})
it('关闭父菜单时清除页面内能力，未授权的子能力不可编辑', async () => {
  payload.roles[0].permissions = { 'extensions.view': 'all', 'knowledge.view': 'all' }
  await start()
  await wrapper.get('[aria-label="展开智能体扩展页面内能力"]').trigger('click')
  await wrapper.get('[aria-label="智能体扩展权限"]').setValue('0')
  expect(wrapper.get('[aria-label="知识库权限"]').element.value).toBe('0')
  expect(wrapper.get('[aria-label="知识库权限"]').element.disabled).toBe(true)
})
it('保留用户的其他角色，失败后勾选回退', async () => {
  mocks.user.mockRejectedValue(new Error('冲突，请刷新'))
  await start()
  await tab('授权成员')
  const input = wrapper.get('[aria-label="给成员直接授权审阅员"]')
  await input.setValue(true)
  await flushPromises()
  expect(mocks.user).toHaveBeenCalledWith(1, ['other', 'reviewer'], ['other'])
  expect(input.element.checked).toBe(false)
})
it('取消部门授权不保留虚假的勾选，并可翻页查看更多部门', async () => {
  payload.feishu_departments = Array.from({ length: 21 }, (_, i) => ({
    id: i + 1,
    name: `部门${i + 1}`,
    tenant_key: 'tenant',
    user_ids: []
  }))
  await start()
  await tab('授权成员')
  window.confirm.mockReturnValue(false)
  const input = wrapper.get('[aria-label="给部门1绑定审阅员"]')
  await input.setValue(true)
  await flushPromises()
  expect(input.element.checked).toBe(false)
  expect(mocks.department).not.toHaveBeenCalled()
  await wrapper.get('[aria-label="飞书部门分页"] button:last-child').trigger('click')
  expect(wrapper.find('[aria-label="给部门21绑定审阅员"]').exists()).toBe(true)
  await wrapper.get('[aria-label="搜索飞书部门"]').setValue('部门1')
  expect(wrapper.get('[aria-label="飞书部门分页"]').text()).toContain('1 / 1')
})
it('生效预览来自服务器并说明飞书继承来源，草稿不会被当作生效权限', async () => {
  await start()
  await wrapper.get('[aria-label="会议管理权限"]').setValue('2')
  await tab('生效预览')
  expect(mocks.preview).toHaveBeenCalledWith(1)
  expect(wrapper.text()).toContain('当前草稿尚未保存')
  expect(wrapper.text()).toContain('飞书部门 研发部 → 审阅员')
  expect(wrapper.get('.menu-preview').text()).toContain('用户反馈')
  expect(wrapper.get('.menu-preview').text()).not.toContain('会议管理')
})
it('创建角色后选中新增角色；内置身份只读', async () => {
  mocks.create.mockImplementation(async ({ name }) => {
    payload.roles.push({ role_key: 'new', name, permissions: {}, user_ids: [], departments: [] })
    return { role_key: 'new' }
  })
  await start()
  await wrapper.get('[aria-label="新角色名称"]').setValue('会议运营')
  await wrapper.get('.create-role').trigger('submit')
  await flushPromises()
  expect(wrapper.get('.role-detail-heading h3').text()).toBe('会议运营')
  await wrapper
    .findAll('.role-card')
    .find((button) => button.text().includes('超级管理员'))
    .trigger('click')
  expect(wrapper.get('[aria-label="工作区权限"]').element.disabled).toBe(true)
  expect(wrapper.get('.save-bar .primary-button').element.disabled).toBe(true)
})
it('加载失败不允许默认配置覆盖权限', async () => {
  mocks.list.mockRejectedValue(new Error('network'))
  await start()
  expect(wrapper.get('[role="alert"]').text()).toContain('加载失败')
  expect(wrapper.find('.permissions-workspace').exists()).toBe(false)
  expect(mocks.save).not.toHaveBeenCalled()
})

it('超级管理员可以编辑管理员默认权限，成员身份仍不可在此改动', async () => {
  payload.roles.unshift({
    role_key: 'admin',
    name: '管理员',
    is_builtin: true,
    permissions: { 'meetings.view': 'all', 'meetings.manage': 'all' },
    user_ids: [],
    departments: []
  })
  await start()
  await wrapper
    .findAll('.role-card')
    .find((button) => button.text().startsWith('管理员'))
    .trigger('click')
  await flushPromises()
  expect(wrapper.get('[aria-label="会议管理权限"]').element.disabled).toBe(false)
  await wrapper.get('[aria-label="会议管理权限"]').setValue('1')
  await wrapper.get('.save-bar .primary-button').trigger('click')
  await flushPromises()
  expect(mocks.save).toHaveBeenCalledWith(
    'admin',
    expect.objectContaining({
      permissions: expect.objectContaining({ 'meetings.manage': 'none' }),
      expected_permissions: expect.objectContaining({ 'meetings.manage': 'all' })
    })
  )
  await tab('授权成员')
  expect(wrapper.get('[aria-label="给成员直接授权管理员"]').element.disabled).toBe(true)
})
it('非超级管理员只能查看内置默认权限', async () => {
  mocks.isSuperAdmin = false
  payload.roles = [
    {
      role_key: 'user',
      name: '普通用户',
      is_builtin: true,
      permissions: {},
      user_ids: [],
      departments: []
    }
  ]
  await start()
  expect(wrapper.get('[aria-label="会议管理权限"]').element.disabled).toBe(true)
  expect(wrapper.text()).toContain('仅超级管理员可以编辑')
})
