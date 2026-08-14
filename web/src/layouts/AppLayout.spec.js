// @vitest-environment jsdom

import { shallowMount } from '@vue/test-utils'
import { ref } from 'vue'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import AppLayoutSource from './AppLayout.vue?raw'
import AppLayout from './AppLayout.vue'

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
  useTaskerStore: () => ({
    __refs: { activeCount: ref(0), isDrawerOpen: ref(false) },
    loadTasks: vi.fn(),
    openDrawer: vi.fn()
  })
}))

vi.mock('@/stores/user', () => ({
  useUserStore: () => ({ isAdmin: false, isSuperAdmin: false })
}))

describe('AppLayout', () => {
  beforeEach(() => {
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
})
