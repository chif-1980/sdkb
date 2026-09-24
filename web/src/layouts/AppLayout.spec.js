// @vitest-environment jsdom

import { shallowMount } from '@vue/test-utils'
import { ref } from 'vue'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import AppLayoutSource from './AppLayout.vue?raw'
import AppLayout from './AppLayout.vue'

const taskerState = vi.hoisted(() => ({
  refs: {
    activeCount: { value: 0 },
    isDrawerOpen: { value: false },
    sortedTasks: { value: [] }
  },
  store: null
}))
const userState = vi.hoisted(() => ({
  isAdmin: false, isSuperAdmin: false, canViewFeedback: false, grants: null,
  hasPermission(permission) {
    if (this.grants) return this.grants.includes(permission)
    if (this.isSuperAdmin) return true
    if (permission === 'feedback.view') return this.canViewFeedback
    if (permission === 'dashboard.view') return false
    return this.isAdmin
  }
}))
const routerState = vi.hoisted(() => ({ push: vi.fn(), replace: vi.fn() }))

vi.mock('pinia', async (importOriginal) => {
  const actual = await importOriginal()
  return {
    ...actual,
    storeToRefs: (store) => store.__refs || {}
  }
})

vi.mock('vue-router', async (importOriginal) => {
  const actual = await importOriginal()
  return {
    ...actual,
    useRoute: () => ({ path: '/feishu-knowledge', params: {} }),
    useRouter: () => routerState
  }
})

vi.mock('@/stores/config', () => ({
  useConfigStore: () => ({ refreshConfig: vi.fn() })
}))

vi.mock('@/stores/agent', () => ({
  useAgentStore: () => ({ isInitialized: true, initialize: vi.fn() })
}))

vi.mock('@/stores/chatThreads', () => ({
  useChatThreadsStore: () => ({
    __refs: {
      threads: ref([]),
      currentThreadId: ref(null),
      hasMoreThreads: ref(false),
      isLoadingMoreThreads: ref(false)
    },
    loadThreads: vi.fn(),
    loadMoreThreads: vi.fn(),
    setCurrentThreadId: vi.fn(),
    upsertThread: vi.fn(),
    deleteThread: vi.fn(),
    updateThread: vi.fn()
  })
}))

vi.mock('@/stores/chatUI', () => ({
  useChatUIStore: () => ({
    __refs: { sidebarCollapsed: ref(false) }
  })
}))

vi.mock('@/stores/database', () => ({
  useDatabaseStore: () => ({ loadDatabases: vi.fn() })
}))

vi.mock('@/stores/info', () => ({
  useInfoStore: () => ({
    organization: { name: 'Quickdone', avatar: '' },
    branding: { name: 'Quickdone' },
    loadInfoConfig: vi.fn()
  })
}))

vi.mock('@/stores/tasker', () => ({
  useTaskerStore: () => {
    const store = {
      __refs: taskerState.refs,
      loadTasks: vi.fn(),
      openDrawer: vi.fn()
    }
    taskerState.store = store
    return store
  }
}))

vi.mock('@/stores/user', () => ({
  useUserStore: () => userState
}))

describe('AppLayout', () => {
  beforeEach(() => {
    userState.grants = null
    userState.isAdmin = false
    userState.isSuperAdmin = false
    userState.canViewFeedback = false
    taskerState.refs.sortedTasks.value = []
    taskerState.refs.activeCount.value = 0
    taskerState.refs.isDrawerOpen.value = false
    routerState.push.mockReset()
    routerState.replace.mockReset()
    globalThis.fetch = vi.fn().mockResolvedValue({
      json: async () => ({ stargazers_count: 0 })
    })
    Object.defineProperty(window, 'matchMedia', {
      configurable: true,
      value: vi.fn((query) => ({
        matches: query === '(max-width: 760px)',
        media: query,
        onchange: null,
        addEventListener: vi.fn(),
        removeEventListener: vi.fn(),
        addListener: vi.fn(),
        removeListener: vi.fn(),
        dispatchEvent: vi.fn()
      }))
    })
  })

  it('超级管理员可从会议管理下方直接进入用户反馈', () => {
    userState.isAdmin = true
    userState.isSuperAdmin = true
    userState.canViewFeedback = true
    const wrapper = shallowMount(AppLayout)
    const paths = wrapper.findAllComponents({ name: 'RouterLink' }).map((link) => link.props('to'))
    expect(paths[paths.indexOf('/meeting-management') + 1]).toBe('/feedbacks')
    wrapper.unmount()
  })

  it('普通管理员不显示全企业反馈入口', () => {
    userState.isAdmin = true
    const wrapper = shallowMount(AppLayout)
    const paths = wrapper.findAllComponents({ name: 'RouterLink' }).map((link) => link.props('to'))
    expect(paths).toContain('/meeting-management')
    expect(paths).not.toContain('/feedbacks')
    wrapper.unmount()
  })

  it('授权管理员可查看反馈，撤销后隐藏入口', () => {
    userState.isAdmin = true
    userState.canViewFeedback = true
    const wrapper = shallowMount(AppLayout)
    expect(wrapper.findAllComponents({ name: 'RouterLink' }).map((link) => link.props('to'))).toContain('/feedbacks')
    wrapper.unmount()
    userState.canViewFeedback = false
    const revoked = shallowMount(AppLayout)
    expect(revoked.findAllComponents({ name: 'RouterLink' }).map((link) => link.props('to'))).not.toContain('/feedbacks')
    revoked.unmount()
  })

  it('普通账号按自定义角色显示菜单而非按管理员身份', () => {
    userState.grants = ['meetings.view', 'feedback.view']
    const wrapper = shallowMount(AppLayout)
    const paths = wrapper.findAllComponents({ name: 'RouterLink' }).map((link) => link.props('to'))
    expect(paths).toContain('/meeting-management')
    expect(paths).toContain('/feedbacks')
    expect(paths).not.toContain('/extensions')
    expect(paths).not.toContain('/workspace')
    wrapper.unmount()
  })

  it('点击企业知识助手使用右侧嵌入路由，不离开知枢布局', async () => {
    const wrapper = shallowMount(AppLayout)
    const assistant = wrapper.findAll('button').find((button) => button.text().includes('企业知识助手'))
    expect(assistant.exists()).toBe(true)
    await assistant.trigger('click')
    expect(routerState.push).toHaveBeenCalledWith({ name: 'EnterpriseAssistantEmbed' })
    wrapper.unmount()
  })

  it('在窄屏使用折叠导航且不强制根布局宽度', async () => {
    const wrapper = shallowMount(AppLayout)
    await wrapper.vm.$nextTick()

    expect(wrapper.classes()).toContain('sidebar-collapsed')
    expect(AppLayoutSource).toMatch(/\.app-layout\s*{[\s\S]*?min-width:\s*0;/)
  })

  it('切换到其他菜单时仍显示飞书扫描全局进度，并可打开任务中心', async () => {
    userState.isAdmin = true
    taskerState.refs.sortedTasks.value = [
      {
        id: 'scan-task-1',
        name: '全量扫描 · 飞书知识源',
        type: 'feishu_scan',
        status: 'running',
        progress: 42,
        message: '正在扫描资料 · 已处理 8 项'
      }
    ]

    const wrapper = shallowMount(AppLayout)
    await wrapper.vm.$nextTick()

    const progress = wrapper.get('[data-testid="global-scan-progress"]')
    expect(progress.text()).toContain('飞书知识扫描')
    expect(progress.text()).toContain('42%')
    expect(progress.text()).toContain('正在扫描资料 · 已处理 8 项')

    await progress.trigger('click')
    expect(taskerState.store.openDrawer).toHaveBeenCalledTimes(1)
    wrapper.unmount()
  })
})
