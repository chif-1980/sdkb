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
const userState = vi.hoisted(() => ({ isAdmin: false, isSuperAdmin: false }))

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
    useRouter: () => ({ push: vi.fn(), replace: vi.fn() })
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
    userState.isAdmin = false
    userState.isSuperAdmin = false
    taskerState.refs.sortedTasks.value = []
    taskerState.refs.activeCount.value = 0
    taskerState.refs.isDrawerOpen.value = false
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
