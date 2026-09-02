<script setup>
import { ref, onMounted, computed, provide, watch } from 'vue'
import { RouterLink, RouterView, useRoute, useRouter } from 'vue-router'
import { useMediaQuery } from '@vueuse/core'
import { GithubOutlined } from '@ant-design/icons-vue'
import {
  BarChart3,
  ClipboardList,
  LibraryBig,
  Box,
  FolderKanban,
  PanelLeftClose,
  PanelLeftOpen,
  MessageCirclePlus,
  Workflow,
  Search
} from 'lucide-vue-next'

import { useConfigStore } from '@/stores/config'
import { useAgentStore } from '@/stores/agent'
import { useChatThreadsStore } from '@/stores/chatThreads'
import { useChatUIStore } from '@/stores/chatUI'
import { useDatabaseStore } from '@/stores/database'
import { useInfoStore } from '@/stores/info'
import { useTaskerStore } from '@/stores/tasker'
import { useUserStore } from '@/stores/user'
import { storeToRefs } from 'pinia'
import UserInfoComponent from '@/components/UserInfoComponent.vue'
import DebugComponent from '@/components/DebugComponent.vue'
import TaskCenterDrawer from '@/components/TaskCenterDrawer.vue'
import SettingsModal from '@/components/SettingsModal.vue'
import ConversationNavSection from '@/components/ConversationNavSection.vue'
import ConversationSearchModal from '@/components/ConversationSearchModal.vue'

const configStore = useConfigStore()
const agentStore = useAgentStore()
const chatThreadsStore = useChatThreadsStore()
const chatUIStore = useChatUIStore()
const databaseStore = useDatabaseStore()
const infoStore = useInfoStore()
const taskerStore = useTaskerStore()
const userStore = useUserStore()
const { activeCount: activeCountRef, isDrawerOpen, sortedTasks } = storeToRefs(taskerStore)
const { threads, currentThreadId, hasMoreThreads, isLoadingMoreThreads } =
  storeToRefs(chatThreadsStore)

// Add state for GitHub stars
const githubStars = ref(0)
const isLoadingStars = ref(false)

// Add state for debug modal
const showDebugModal = ref(false)

// Add state for settings modal
const showSettingsModal = ref(false)
const settingsInitialTab = ref('')

const { sidebarCollapsed } = storeToRefs(chatUIStore)
const isMobileViewport = useMediaQuery('(max-width: 760px)')
const effectiveSidebarCollapsed = computed(() => sidebarCollapsed.value || isMobileViewport.value)
const conversationSearchOpen = ref(false)

// Provide settings modal methods to child components
const openSettingsModal = (tab) => {
  settingsInitialTab.value = tab || (userStore.isAdmin ? 'base' : 'account')
  showSettingsModal.value = true
}

// Handle debug modal close
const handleDebugModalClose = () => {
  showDebugModal.value = false
}

const getRemoteConfig = async () => {
  try {
    await configStore.refreshConfig()
  } catch (error) {
    console.warn('加载系统配置失败:', error)
  }
}

const getRemoteDatabase = async () => {
  try {
    await databaseStore.loadDatabases()
  } catch (error) {
    console.warn('加载知识库列表失败:', error)
  }
}

// Fetch GitHub stars count
const fetchGithubStars = async () => {
  try {
    isLoadingStars.value = true
    // 公共API，可以直接使用fetch
    const response = await fetch('https://api.github.com/repos/chif-1980/sdkb')
    const data = await response.json()
    githubStars.value = data.stargazers_count
  } catch (error) {
    console.error('获取GitHub stars失败:', error)
  } finally {
    isLoadingStars.value = false
  }
}

onMounted(async () => {
  // 加载信息配置与知识库数据无依赖，可并行
  await Promise.all([infoStore.loadInfoConfig(), getRemoteDatabase()])
  await initAgentNavigation()
  await getRemoteConfig()
  // 仅管理员加载任务中心数据
  if (userStore.isAdmin) {
    taskerStore.loadTasks()
    fetchGithubStars() // Fetch GitHub stars on mount
  }
})

const route = useRoute()
const router = useRouter()

const activeTaskCount = computed(() => activeCountRef.value || 0)
const ACTIVE_TASK_STATUSES = new Set(['pending', 'queued', 'running'])
const activeFeishuScanTasks = computed(() =>
  (sortedTasks.value || []).filter(
    (task) => task.type === 'feishu_scan' && ACTIVE_TASK_STATUSES.has(task.status)
  )
)
const activeFeishuScanTask = computed(() => activeFeishuScanTasks.value[0] || null)
const activeFeishuScanProgress = computed(() => {
  const value = Number(activeFeishuScanTask.value?.progress)
  return Number.isFinite(value) ? Math.min(100, Math.max(0, Math.round(value))) : 0
})
const activeFeishuScanMessage = computed(() => {
  const task = activeFeishuScanTask.value
  if (!task) return ''
  if (task.message) return task.message
  return task.status === 'queued' ? '任务已排队，等待开始…' : '正在执行扫描…'
})

const openTaskCenter = () => {
  taskerStore.openDrawer()
}
const activeConversationThreadId = computed(() => {
  return route.path.startsWith('/agent') ? currentThreadId.value : null
})
const organizationName = computed(() => {
  return infoStore.organization.name || infoStore.branding.name || 'ZhiShu'
})

// 下面是导航菜单部分，添加智能体项
const mainList = computed(() => {
  const items = [
    {
      name: '创建新对话',
      path: '/agent',
      icon: MessageCirclePlus,
      activeIcon: MessageCirclePlus,
      action: true,
      exactActive: true
    }
  ]

  items.push({
    name: '工作区',
    path: '/workspace',
    icon: FolderKanban,
    activeIcon: FolderKanban
  })

  items.push({
    name: '智能体扩展',
    path: '/extensions',
    activePaths: ['/extensions'],
    icon: LibraryBig,
    activeIcon: LibraryBig
  })

  items.push({
    name: '智能体管理',
    path: '/model-manage',
    icon: Box,
    activeIcon: Box
  })

  if (userStore.isAdmin) {
    items.push({
      name: '知识加工',
      path: '/feishu-knowledge',
      icon: Workflow,
      activeIcon: Workflow
    })
  }

  if (userStore.isSuperAdmin) {
    items.push({
      name: '数据总览',
      path: '/dashboard',
      icon: BarChart3,
      activeIcon: BarChart3
    })
  }

  return items
})

const primaryNavItem = computed(() => mainList.value[0] || null)
const secondaryNavItems = computed(() => mainList.value.slice(1))

const isNavItemActive = (item) => {
  const activePaths = item.activePaths || [item.path]
  if (item.exactActive) {
    return activePaths.some((path) => route.path === path)
  }
  return activePaths.some((path) => route.path === path || route.path.startsWith(`${path}/`))
}

const setSidebarCollapsed = (collapsed) => {
  sidebarCollapsed.value = collapsed
}

const toggleSidebar = () => {
  setSidebarCollapsed(!sidebarCollapsed.value)
}

const expandSidebar = () => {
  if (!isMobileViewport.value) {
    setSidebarCollapsed(false)
  }
}

const openConversationSearch = () => {
  conversationSearchOpen.value = true
}

const initAgentNavigation = async () => {
  try {
    if (!agentStore.isInitialized) {
      await agentStore.initialize()
    }
    await chatThreadsStore.loadThreads()
  } catch (error) {
    console.warn('加载对话导航失败:', error)
  }
}

const handleSelectChat = (threadId) => {
  if (!threadId) return
  chatThreadsStore.setCurrentThreadId(threadId)
  router.push({ name: 'AgentCompWithThreadId', params: { thread_id: threadId } })
}

const handleSearchThreadFound = (thread) => {
  chatThreadsStore.upsertThread(thread)
}

const handleSearchSelectThread = (thread) => {
  if (!thread?.id) return
  chatThreadsStore.upsertThread(thread)
  handleSelectChat(thread.id)
}

const handleCreateConversationFromSearch = () => {
  chatThreadsStore.setCurrentThreadId(null)
  router.push({ name: 'AgentComp' })
}

const handleDeleteChat = async (threadId) => {
  if (!threadId) return
  try {
    await chatThreadsStore.deleteThread(threadId)
    if (route.params.thread_id === threadId) {
      await router.replace({ name: 'AgentComp' })
    }
  } catch (error) {
    console.warn('删除对话失败:', error)
  }
}

const handleRenameChat = async ({ chatId, title }) => {
  try {
    await chatThreadsStore.updateThread(chatId, title)
  } catch (error) {
    console.warn('重命名对话失败:', error)
  }
}

const handleTogglePinChat = async (threadId) => {
  const thread = threads.value.find((item) => item.id === threadId)
  if (!thread) return
  try {
    await chatThreadsStore.updateThread(threadId, null, !thread.is_pinned)
    await chatThreadsStore.loadThreads()
    if (currentThreadId.value) {
      chatThreadsStore.setCurrentThreadId(currentThreadId.value)
    }
  } catch (error) {
    console.warn('更新置顶状态失败:', error)
  }
}

watch(
  () => [route.path, route.params.thread_id],
  () => {
    if (!route.path.startsWith('/agent')) return
    const threadId = typeof route.params.thread_id === 'string' ? route.params.thread_id : null
    chatThreadsStore.setCurrentThreadId(threadId)
  },
  { immediate: true }
)

// Provide settings modal methods to child components
provide('settingsModal', {
  openSettingsModal
})
</script>

<template>
  <div class="app-layout" :class="{ 'sidebar-collapsed': effectiveSidebarCollapsed }">
    <div class="header">
      <div class="sidebar-brand" @click.stop>
        <div v-if="!effectiveSidebarCollapsed" class="brand-link brand-static">
          <img :src="infoStore.organization.avatar" class="brand-avatar" />
          <span class="brand-name">{{ organizationName }}</span>
        </div>
        <button
          v-else
          type="button"
          class="brand-link brand-expand-button"
          :aria-label="isMobileViewport ? '移动端侧边栏已折叠' : '展开侧边栏'"
          :disabled="isMobileViewport"
          @click="expandSidebar"
        >
          <img :src="infoStore.organization.avatar" class="brand-avatar brand-avatar-image" />
          <PanelLeftOpen class="brand-expand-icon" size="20" />
        </button>
        <button
          v-if="!effectiveSidebarCollapsed"
          type="button"
          class="sidebar-toggle"
          aria-label="折叠侧边栏"
          @click="toggleSidebar"
        >
          <PanelLeftClose size="18" />
        </button>
      </div>
      <div class="nav">
        <RouterLink
          v-if="primaryNavItem"
          :to="primaryNavItem.path"
          class="nav-item"
          :class="{ active: isNavItemActive(primaryNavItem) }"
          :active-class="primaryNavItem.action ? '' : 'active'"
          @click.stop
        >
          <a-tooltip placement="right" :open="effectiveSidebarCollapsed ? undefined : false">
            <template #title>{{ primaryNavItem.name }}</template>
            <component
              class="icon"
              :is="
                isNavItemActive(primaryNavItem) ? primaryNavItem.activeIcon : primaryNavItem.icon
              "
              size="18"
            />
          </a-tooltip>
          <span class="nav-text">{{ primaryNavItem.name }}</span>
        </RouterLink>

        <button
          type="button"
          class="nav-item"
          :class="{ active: conversationSearchOpen }"
          @click.stop="openConversationSearch"
        >
          <a-tooltip placement="right" :open="effectiveSidebarCollapsed ? undefined : false">
            <template #title>搜索对话</template>
            <Search class="icon" size="18" />
          </a-tooltip>
          <span class="nav-text">搜索对话</span>
        </button>

        <RouterLink
          v-for="(item, index) in secondaryNavItems"
          :key="index"
          :to="item.path"
          v-show="!item.hidden"
          class="nav-item"
          :class="{ active: isNavItemActive(item) }"
          :active-class="item.action ? '' : 'active'"
          @click.stop
        >
          <a-tooltip placement="right" :open="effectiveSidebarCollapsed ? undefined : false">
            <template #title>{{ item.name }}</template>
            <component
              class="icon"
              :is="isNavItemActive(item) ? item.activeIcon : item.icon"
              size="18"
            />
          </a-tooltip>
          <span class="nav-text">{{ item.name }}</span>
        </RouterLink>
      </div>
      <div class="fill">
        <ConversationNavSection
          v-if="!effectiveSidebarCollapsed"
          class="sidebar-conversations"
          :current-chat-id="activeConversationThreadId"
          :chats-list="threads"
          :has-more-chats="hasMoreThreads"
          :is-loading-more="isLoadingMoreThreads"
          @select-chat="handleSelectChat"
          @delete-chat="handleDeleteChat"
          @rename-chat="handleRenameChat"
          @toggle-pin="handleTogglePinChat"
          @load-more-chats="() => chatThreadsStore.loadMoreThreads()"
        />
      </div>
      <div class="foo">
        <div class="github nav-item" @click.stop>
          <a-tooltip placement="right" :open="effectiveSidebarCollapsed ? undefined : false">
            <template #title>欢迎 Star</template>
            <a href="https://github.com/chif-1980/sdkb" target="_blank" class="github-link">
              <GithubOutlined class="icon" />
              <span class="nav-text">GitHub</span>
              <span v-if="githubStars > 0" class="github-stars">
                <span class="star-count">{{ (githubStars / 1000).toFixed(1) }}k</span>
              </span>
            </a>
          </a-tooltip>
        </div>
        <!-- 用户信息组件 -->
        <div class="nav-item user-info" @click.stop>
          <UserInfoComponent :show-role="!effectiveSidebarCollapsed">
            <template v-if="userStore.isAdmin" #actions>
              <a-tooltip placement="top" title="任务中心">
                <button
                  class="user-task-center"
                  :class="{ active: isDrawerOpen }"
                  type="button"
                  aria-label="任务中心"
                  @click.stop="taskerStore.openDrawer()"
                >
                  <a-badge
                    :count="activeTaskCount"
                    :overflow-count="99"
                    class="task-center-badge"
                    size="small"
                  >
                    <ClipboardList class="icon" size="16" />
                  </a-badge>
                </button>
              </a-tooltip>
            </template>
          </UserInfoComponent>
        </div>
      </div>
    </div>
    <router-view v-slot="{ Component, route }" id="app-router-view">
      <keep-alive v-if="route.meta.keepAlive !== false">
        <component :is="Component" />
      </keep-alive>
      <component :is="Component" v-else />
    </router-view>

    <button
      v-if="userStore.isAdmin && activeFeishuScanTask"
      type="button"
      class="global-scan-progress"
      data-testid="global-scan-progress"
      :aria-label="`${activeFeishuScanTask.name}，${activeFeishuScanProgress}%`"
      @click="openTaskCenter"
    >
      <span class="global-scan-progress-heading">
        <span class="global-scan-progress-title">飞书知识扫描</span>
        <span>{{ activeFeishuScanProgress }}%</span>
      </span>
      <span class="global-scan-progress-track" aria-hidden="true">
        <span :style="{ width: `${activeFeishuScanProgress}%` }"></span>
      </span>
      <span class="global-scan-progress-message">{{ activeFeishuScanMessage }}</span>
      <span v-if="activeFeishuScanTasks.length > 1" class="global-scan-progress-more">
        还有 {{ activeFeishuScanTasks.length - 1 }} 个扫描任务
      </span>
    </button>

    <ConversationSearchModal
      v-model:open="conversationSearchOpen"
      :recent-threads="threads"
      @select-thread="handleSearchSelectThread"
      @create-thread="handleCreateConversationFromSearch"
      @thread-found="handleSearchThreadFound"
    />

    <!-- Debug Modal -->
    <a-modal
      v-model:open="showDebugModal"
      title="调试面板"
      width="90%"
      :footer="null"
      @cancel="handleDebugModalClose"
      :maskClosable="true"
      :destroyOnClose="true"
      class="debug-modal"
    >
      <DebugComponent />
    </a-modal>
    <TaskCenterDrawer v-if="userStore.isAdmin" />
    <SettingsModal
      v-model:visible="showSettingsModal"
      :initial-tab="settingsInitialTab"
      @close="() => (showSettingsModal = false)"
    />
  </div>
</template>

<style lang="less" scoped>
// Less 变量定义
@sidebar-width: 230px;
@sidebar-collapsed-width: 56px;
@sidebar-padding-y: 6px;
@sidebar-padding-x: 8px;
@sidebar-padding: @sidebar-padding-y @sidebar-padding-x;
@sidebar-border-width: 1px;
@sidebar-item-height: 32px;
@sidebar-item-padding-x: 10px;
@sidebar-icon-size: 16px;
@brand-avatar-size: 28px;
@sidebar-collapsed-content-width: @sidebar-collapsed-width - (2 * @sidebar-padding-x) -
  @sidebar-border-width;
@sidebar-collapsed-icon-padding-x: (
  (@sidebar-collapsed-content-width - @sidebar-icon-size - (2 * @sidebar-border-width)) / 2
);
@sidebar-collapsed-avatar-padding-x: (
  (@sidebar-collapsed-content-width - @sidebar-item-height - (2 * @sidebar-border-width)) / 2
);
@sidebar-collapsed-brand-padding-x: ((@sidebar-collapsed-content-width - @brand-avatar-size) / 2);
@sidebar-collapsed-brand-icon-padding-x: (
  (@sidebar-collapsed-content-width - @sidebar-icon-size) / 2
);

.app-layout {
  display: flex;
  flex-direction: row;
  width: 100%;
  height: 100vh;
  min-width: 0;
}

div.header,
#app-router-view {
  height: 100%;
  max-width: 100%;
}

#app-router-view {
  flex: 1 1 auto;
  min-width: 0;
  overflow-y: auto;
}

.header {
  display: flex;
  flex-direction: column;
  flex: 0 0 @sidebar-width;
  justify-content: flex-start;
  align-items: stretch;
  gap: 16px;
  background-color: var(--main-5);
  height: 100%;
  width: @sidebar-width;
  border-right: 1px solid var(--gray-100);
  padding: @sidebar-padding;
  overflow: hidden;
  user-select: none;
  transition:
    width 0.18s ease,
    flex-basis 0.18s ease;

  .nav {
    display: flex;
    flex: 0 0 auto;
    flex-direction: column;
    justify-content: flex-start;
    align-items: stretch;
    position: relative;
    gap: 4px;
  }

  .sidebar-conversations {
    height: 100%;
    min-height: 0;
    overflow: hidden;
  }

  .sidebar-brand,
  :deep(.conversation-nav-section:not(.sidebar-conversations)),
  .github,
  .user-info {
    flex-shrink: 0;
  }

  .fill {
    flex: 1 1 0;
    min-height: 0;
  }

  .sidebar-brand {
    display: flex;
    align-items: center;
    justify-content: space-between;
    height: @sidebar-item-height;
    gap: 8px;
  }

  .brand-link {
    display: flex;
    flex: 1 1 auto;
    align-items: center;
    min-width: 0;
    height: @sidebar-item-height;
    color: var(--gray-900);
    text-decoration: none;
    border: 0;
    background: transparent;
    padding: 0 4px;
    cursor: pointer;
  }

  .brand-static {
    cursor: default;
  }

  .brand-avatar {
    flex: 0 0 @brand-avatar-size;
    width: @brand-avatar-size;
    height: @brand-avatar-size;
    object-fit: contain;
  }

  .brand-name {
    min-width: 0;
    margin-left: 10px;
    overflow: hidden;
    color: var(--gray-1000);
    font-size: 15px;
    font-weight: 650;
    line-height: 20px;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .sidebar-toggle {
    display: inline-flex;
    flex: 0 0 32px;
    align-items: center;
    justify-content: center;
    width: 32px;
    height: 32px;
    border: 1px solid transparent;
    border-radius: 8px;
    background: transparent;
    color: var(--gray-600);
    cursor: pointer;
    transition:
      background-color 0.2s ease,
      border-color 0.2s ease,
      color 0.2s ease;

    &:hover,
    &:focus-visible {
      border-color: var(--main-50);
      background: var(--main-20);
      color: var(--main-color);
      outline: none;
    }
  }

  .nav-item {
    display: flex;
    align-items: center;
    justify-content: flex-start;
    width: 100%;
    height: @sidebar-item-height;
    padding: 0 @sidebar-item-padding-x;
    border: 1px solid transparent;
    border-radius: 8px;
    background-color: transparent;
    color: var(--gray-700);
    font-size: 14px;
    font-weight: 450;
    transition:
      background-color 0.2s ease-in-out,
      border-color 0.2s ease-in-out,
      color 0.2s ease-in-out;
    margin: 0;
    text-decoration: none;
    cursor: pointer;
    outline: none;

    .icon {
      flex: 0 0 @sidebar-icon-size;
      width: @sidebar-icon-size;
      height: @sidebar-icon-size;
    }

    .nav-text {
      min-width: 0;
      max-width: 140px;
      margin-left: 8px;
      overflow: hidden;
      line-height: 20px;
      font-weight: 450;
      text-overflow: ellipsis;
      white-space: nowrap;
      transition:
        opacity 0.12s ease,
        margin-left 0.18s ease,
        max-width 0.18s ease;
    }

    & > svg:focus {
      outline: none;
    }
    & > svg:focus-visible {
      outline: none;
    }

    &.active {
      border-color: transparent;
      background-color: color-mix(in srgb, var(--main-color) 6%, var(--gray-0));
      font-weight: 600;
      color: var(--main-color);
    }

    &.primary-action {
      margin-bottom: 8px;
      border-color: var(--gray-150);
      background-color: var(--gray-0);
      color: var(--main-color);
      box-shadow: 0 3px 4px rgba(0, 10, 20, 0.02);

      &:hover {
        border-color: var(--gray-200);
        background-color: var(--gray-0);
        color: var(--main-color);
        box-shadow: 0 3px 4px rgba(0, 10, 20, 0.07);
      }
    }

    &.warning {
      color: var(--color-error-500);
    }

    &:hover {
      border-color: transparent;
      background-color: var(--main-20);
      color: var(--main-color);
    }

    &.github {
      margin-bottom: 8px;
      &:hover {
        border-color: transparent;
      }

      .github-link {
        display: flex;
        align-items: center;
        width: 100%;
        min-width: 0;
        color: inherit;
        text-decoration: none;
      }

      .icon {
        display: inline-flex;
        align-items: center;
        justify-content: center;
        font-size: @sidebar-icon-size;
        line-height: 1;
      }

      .github-stars {
        display: flex;
        align-items: center;
        max-width: 48px;
        margin-left: auto;
        overflow: hidden;
        font-size: 12px;
        color: var(--gray-600);
        background-color: var(--gray-100);
        padding: 2px 8px;
        border-radius: 6px;
        white-space: nowrap;
        transition:
          opacity 0.12s ease,
          max-width 0.18s ease;

        .star-count {
          font-weight: 600;
        }
      }
    }

    &.api-docs {
      padding: 10px 12px;
    }
    &.docs {
      display: none;
    }
    &.theme-toggle-nav {
      .theme-toggle-icon {
        display: flex;
        align-items: center;
        justify-content: center;
        width: 100%;
        height: 100%;
        cursor: pointer;
        color: var(--gray-1000);
        transition: color 0.2s ease-in-out;

        &:hover {
          color: var(--main-color);
        }
      }
    }
    &.user-info {
      margin-bottom: 8px;
      padding: 0 3px;
      overflow: hidden;

      :deep(.user-info-component) {
        width: 100%;
      }

      :deep(.user-info-dropdown) {
        width: 100%;
        height: @sidebar-item-height;
        border-radius: 8px;
        transition:
          background-color 0.2s ease,
          color 0.2s ease;
      }

      :deep(.user-info-dropdown:hover) {
        background: var(--main-20);
        color: var(--main-color);
      }
      :deep(.user-name) {
        flex: 1 1 auto;
      }

      :deep(.user-task-center) {
        display: inline-flex;
        align-items: center;
        justify-content: center;
        width: 28px;
        height: 28px;
        padding: 0;
        border: 1px solid transparent;
        border-radius: 6px;
        background: transparent;
        color: var(--gray-600);
        cursor: pointer;
        transition:
          background-color 0.2s ease,
          color 0.2s ease;

        &:hover,
        &.active {
          background: var(--main-30);
          color: var(--main-color);
        }

        .task-center-badge {
          display: flex;
          justify-content: center;
        }

        .icon {
          display: block;
          width: 16px;
          height: 16px;
        }
      }
    }
  }
}

.app-layout.sidebar-collapsed {
  .header {
    flex-basis: @sidebar-collapsed-width;
    width: @sidebar-collapsed-width;
    align-items: stretch;
    padding: @sidebar-padding;

    .sidebar-brand {
      justify-content: flex-start;
      width: 100%;
    }

    .brand-expand-button {
      flex: 0 0 100%;
      justify-content: flex-start;
      width: 100%;
      padding: 0;
      border-radius: 8px;

      .brand-avatar-image {
        margin-left: @sidebar-collapsed-brand-padding-x;
      }

      .brand-expand-icon {
        display: none;
        margin-left: @sidebar-collapsed-brand-icon-padding-x;
        width: @sidebar-icon-size;
        height: @sidebar-icon-size;
        color: var(--main-color);
      }

      &:not(:disabled):hover,
      &:not(:disabled):focus-visible {
        background: var(--main-20);
        outline: none;

        .brand-avatar-image {
          display: none;
        }

        .brand-expand-icon {
          display: block;
        }
      }

      &:disabled {
        cursor: default;
      }
    }

    .nav {
      align-items: stretch;
      width: 100%;
    }

    .nav-item {
      justify-content: flex-start;
      width: 100%;
      padding: 0 @sidebar-collapsed-icon-padding-x;

      .nav-text,
      .github-stars {
        max-width: 0;
        margin-left: 0;
        opacity: 0;
        pointer-events: none;
      }

      &.github {
        .github-link {
          justify-content: flex-start;
        }
      }

      &.user-info {
        padding: 0 @sidebar-collapsed-avatar-padding-x;

        :deep(.user-info-component),
        :deep(.user-info-dropdown) {
          justify-content: flex-start;
        }

        :deep(.user-info-actions) {
          display: none;
        }
      }
    }
  }
}

.global-scan-progress {
  position: fixed;
  z-index: 1100;
  top: 12px;
  right: 16px;
  display: flex;
  width: min(360px, calc(100vw - 32px));
  flex-direction: column;
  gap: 6px;
  padding: 10px 12px;
  border: 1px solid color-mix(in srgb, var(--main-color) 28%, var(--gray-150));
  border-radius: 8px;
  background: var(--gray-0);
  color: var(--color-text);
  box-shadow: 0 8px 24px rgba(0, 10, 20, 0.12);
  cursor: pointer;
  font: inherit;
  text-align: left;
  transition:
    border-color 160ms ease,
    box-shadow 160ms ease;

  &:hover,
  &:focus-visible {
    border-color: var(--main-color);
    box-shadow: 0 10px 28px rgba(0, 10, 20, 0.16);
    outline: none;
  }
}

.global-scan-progress-heading {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  font-size: 13px;
  font-weight: 600;
}

.global-scan-progress-title {
  color: var(--main-color);
}

.global-scan-progress-track {
  display: block;
  height: 5px;
  overflow: hidden;
  border-radius: 3px;
  background: var(--main-30);
}

.global-scan-progress-track > span {
  display: block;
  height: 100%;
  min-width: 3px;
  border-radius: inherit;
  background: var(--main-color);
  transition: width 240ms ease;
}

.global-scan-progress-message,
.global-scan-progress-more {
  overflow: hidden;
  color: var(--color-text-secondary);
  font-size: 12px;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.global-scan-progress-more {
  color: var(--color-text-tertiary);
  font-size: 11px;
}

@media (max-width: 760px) {
  .global-scan-progress {
    top: 8px;
    right: 8px;
    width: calc(100vw - 16px);
  }
}
</style>
