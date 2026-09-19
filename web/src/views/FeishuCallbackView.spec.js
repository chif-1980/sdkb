// @vitest-environment jsdom
import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, expect, it, vi } from 'vitest'
import FeishuCallbackView from './FeishuCallbackView.vue'

const mocks = vi.hoisted(() => ({
  replace: vi.fn(), finish: vi.fn(),
  user: { token: '', logout: vi.fn(), getCurrentUser: vi.fn() }
}))
vi.mock('vue-router', () => ({ useRouter: () => ({ replace: mocks.replace }) }))
vi.mock('@/stores/user', () => ({ useUserStore: () => mocks.user }))
vi.mock('@/apis/feishuAuth', () => ({ finishFeishuLogin: mocks.finish }))

beforeEach(() => {
  vi.resetAllMocks()
  localStorage.clear()
  sessionStorage.setItem('feishu_login_verifier', 'browser-proof')
  mocks.user.token = 'previous-token'
  mocks.user.logout.mockImplementation(() => {
    mocks.user.token = ''
    localStorage.removeItem('user_token')
  })
  mocks.finish.mockResolvedValue({ access_token: 'new-token' })
  mocks.user.getCurrentUser.mockResolvedValue({ role: 'admin' })
  window.history.replaceState(null, '', '/auth/feishu/callback#code=once')
})

async function render() {
  const wrapper = mount(FeishuCallbackView, { global: { stubs: {
    'a-spin': true, 'a-button': true,
    'a-result': { props: ['subTitle'], template: '<div>{{ subTitle }}</div>' }
  } } })
  await flushPromises()
  return wrapper
}

it('成功后载入当前身份并进入会议管理，清除回调凭证', async () => {
  const wrapper = await render()
  expect(mocks.finish).toHaveBeenCalledWith('once')
  expect(localStorage.getItem('user_token')).toBe('new-token')
  expect(mocks.user.getCurrentUser).toHaveBeenCalledOnce()
  expect(mocks.replace).toHaveBeenCalledWith('/meeting-management')
  expect(window.location.hash).toBe('')
  expect(sessionStorage.getItem('feishu_login_verifier')).toBeNull()
  wrapper.unmount()
})

it('无管理权限时显示原因且不清除已有登录', async () => {
  window.history.replaceState(null, '', '/auth/feishu/callback#error=MANAGEMENT_ACCESS_REQUIRED')
  const wrapper = await render()
  expect(wrapper.text()).toContain('尚无知枢管理权限')
  expect(mocks.finish).not.toHaveBeenCalled()
  expect(mocks.user.logout).not.toHaveBeenCalled()
  expect(sessionStorage.getItem('feishu_login_verifier')).toBeNull()
  wrapper.unmount()
})

it('获取身份失败后清理刚签发的令牌，不留下半登录状态', async () => {
  mocks.user.getCurrentUser.mockRejectedValue(new Error('身份读取失败'))
  const wrapper = await render()
  expect(wrapper.text()).toContain('身份读取失败')
  expect(mocks.user.token).toBe('')
  expect(localStorage.getItem('user_token')).toBeNull()
  expect(mocks.replace).not.toHaveBeenCalled()
  wrapper.unmount()
})
