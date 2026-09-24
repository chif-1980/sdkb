import { createRouter, createWebHistory } from 'vue-router'
import AppLayout from '@/layouts/AppLayout.vue'
import { useUserStore } from '@/stores/user'
import { useAgentStore } from '@/stores/agent'
import { sanitizeRedirect } from '@/utils/oidcAutoStart'

const router = createRouter({
  history: createWebHistory(import.meta.env.BASE_URL),
  routes: [
    {
      path: '/auth/feishu/callback',
      component: () => import('../views/FeishuCallbackView.vue'),
      meta: { requiresAuth: false }
    },
    {
      path: '/feishu-account',
      component: AppLayout,
      children: [
        {
          path: '',
          component: () => import('../views/FeishuAccountView.vue'),
          meta: { requiresAuth: true }
        }
      ]
    },
    {
      path: '/',
      name: 'main',
      redirect: '/feishu-knowledge'
    },
    {
      path: '/login',
      name: 'login',
      component: () => import('../views/LoginView.vue'),
      meta: { requiresAuth: false }
    },
    {
      path: '/auth/oidc/callback', // oidc登录回调页面
      name: 'OIDCCallback',
      component: () => import('@/views/OIDCCallbackView.vue'),
      meta: { public: true }
    },
    {
      path: '/auth/cli/authorize',
      name: 'CLIAuthAuthorize',
      component: () => import('@/views/CLIAuthAuthorizeView.vue'),
      meta: { requiresAuth: true }
    },
    {
      path: '/agent',
      name: 'AgentMain',
      component: AppLayout,
      children: [
        {
          path: '',
          name: 'AgentComp',
          component: () => import('../views/AgentView.vue'),
          meta: { keepAlive: true, requiresAuth: true }
        },
        {
          path: ':thread_id',
          name: 'AgentCompWithThreadId',
          component: () => import('../views/AgentView.vue'),
          meta: { keepAlive: true, requiresAuth: true }
        }
      ]
    },
    {
      path: '/enterprise-assistant',
      component: AppLayout,
      children: [
        {
          path: '',
          name: 'EnterpriseAssistantEmbed',
          component: () => import('../views/EnterpriseAssistantEmbedView.vue'),
          meta: { requiresAuth: true, keepAlive: false }
        }
      ]
    },
    ...(import.meta.env.DEV
      ? [
          {
            path: '/role-permissions-prototype',
            name: 'RolePermissionsPrototype',
            component: AppLayout,
            children: [
              {
                path: '',
                name: 'RolePermissionsPrototypeView',
                component: () => import('../views/RolePermissionsPrototypeView.vue'),
                meta: { requiresAuth: true }
              }
            ]
          }
        ]
      : []),
    {
      path: '/role-permissions',
      component: AppLayout,
      children: [
        {
          path: '',
          name: 'RolePermissions',
          component: () => import('../components/RolePermissionsComponent.vue'),
          meta: {
            requiresAuth: true,
            requiresPermission: 'admin.manage',
            requiresPermissionScope: 'all',
            keepAlive: false
          }
        }
      ]
    },
    {
      path: '/workspace',
      name: 'workspace',
      component: AppLayout,
      children: [
        {
          path: '',
          name: 'WorkspaceComp',
          component: () => import('../views/WorkspaceView.vue'),
          meta: { keepAlive: true, requiresAuth: true, requiresPermission: 'workspace.view' }
        }
      ]
    },
    {
      path: '/feedbacks',
      component: AppLayout,
      children: [
        {
          path: '',
          name: 'UserFeedback',
          component: () => import('../views/UserFeedbackView.vue'),
          meta: { requiresAuth: true, requiresPermission: 'feedback.view' }
        }
      ]
    },
    {
      path: '/dashboard',
      name: 'dashboard',
      component: AppLayout,
      children: [
        {
          path: '',
          name: 'DashboardComp',
          component: () => import('../views/DashboardView.vue'),
          meta: { keepAlive: false, requiresAuth: true, requiresPermission: 'dashboard.view' }
        }
      ]
    },
    {
      path: '/feishu-knowledge',
      name: 'feishu-knowledge',
      component: AppLayout,
      children: [
        {
          path: '',
          name: 'FeishuKnowledgeComp',
          component: () => import('../views/FeishuKnowledgeView.vue'),
          meta: {
            keepAlive: false,
            requiresAuth: true,
            requiresPermission: 'feishu_knowledge.view'
          }
        }
      ]
    },
    {
      path: '/meeting-management',
      component: AppLayout,
      children: [
        {
          path: '',
          name: 'MeetingManagement',
          component: () => import('../views/MeetingManagementView.vue'),
          meta: { requiresAuth: true, requiresPermission: 'meetings.view' }
        },
        {
          path: ':id',
          name: 'MeetingManagementDetail',
          component: () => import('../views/MeetingManagementDetail.vue'),
          meta: { requiresAuth: true, requiresPermission: 'meetings.view' }
        }
      ]
    },
    {
      path: '/model-manage',
      name: 'model-manage',
      component: AppLayout,
      children: [
        {
          path: '',
          name: 'ModelManageComp',
          component: () => import('../views/ModelManageView.vue'),
          meta: { keepAlive: false, requiresAuth: true, requiresPermission: 'agents.view' }
        }
      ]
    },
    {
      path: '/extensions',
      name: 'extensions',
      component: AppLayout,
      children: [
        {
          path: '',
          name: 'ExtensionsComp',
          component: () => import('../views/ExtensionsView.vue'),
          meta: {
            keepAlive: false,
            requiresAuth: true,
            requiresPermission: 'extensions.view'
          },
          children: [
            {
              path: 'knowledgebase/:kbId',
              name: 'ExtensionKnowledgeBaseDetail',
              component: () => import('../views/DataBaseInfoView.vue'),
              meta: {
                keepAlive: false,
                requiresAuth: true,
                requiresPermission: 'knowledge.view'
              }
            },
            {
              path: 'mcp/:slug',
              name: 'ExtensionMcpDetail',
              component: () => import('../components/extensions/McpDetailView.vue'),
              meta: {
                keepAlive: false,
                requiresAuth: true,
                requiresPermission: 'extensions.manage'
              }
            },
            {
              path: 'skill/:slug',
              name: 'ExtensionSkillDetail',
              component: () => import('../components/extensions/SkillDetailView.vue'),
              meta: {
                keepAlive: false,
                requiresAuth: true,
                requiresPermission: 'extensions.view'
              }
            }
          ]
        }
      ]
    },
    {
      path: '/:pathMatch(.*)*',
      name: 'NotFound',
      component: () => import('../views/EmptyView.vue'),
      meta: { requiresAuth: false }
    }
  ]
})

// 全局前置守卫
router.beforeEach(async (to) => {
  // 检查路由是否需要认证
  const requiresAuth = to.matched.some((record) => record.meta.requiresAuth === true)
  const requiredPermissions = to.matched.flatMap((record) =>
    record.meta.requiresPermission
      ? [
          {
            permission: record.meta.requiresPermission,
            scope: record.meta.requiresPermissionScope || 'self'
          }
        ]
      : []
  )
  const requiresAdmin = to.matched.some((record) => record.meta.requiresAdmin)
  const requiresSuperAdmin = to.matched.some((record) => record.meta.requiresSuperAdmin)

  const userStore = useUserStore()

  // 如果有 token 但用户信息未加载，先获取用户信息
  if (userStore.token && !userStore.userId) {
    try {
      await userStore.getCurrentUser()
    } catch (error) {
      // 如果获取用户信息失败（如 token 过期），清除 token
      console.error('获取用户信息失败:', error)
      userStore.logout()
    }
  }

  const isLoggedIn = userStore.isLoggedIn
  const isAdmin = userStore.isAdmin
  const isSuperAdmin = userStore.isSuperAdmin

  if (isLoggedIn) {
    try {
      await userStore.refreshPermissions()
    } catch (error) {
      console.error('获取角色权限失败:', error)
    }
  }
  if (
    isLoggedIn &&
    requiredPermissions.some(({ permission, scope }) => !userStore.hasPermission(permission, scope))
  ) {
    return '/agent'
  }

  // 如果路由需要认证但用户未登录
  if (requiresAuth && !isLoggedIn) {
    // 保存尝试访问的路径，登录后跳转
    sessionStorage.setItem('redirect', to.fullPath)
    return '/login'
  }

  // 如果路由需要管理员权限但用户不是管理员
  if (requiresAdmin && !isAdmin) {
    // 如果是普通用户，跳转到聊天页空态
    try {
      const agentStore = useAgentStore()
      // 等待 store 初始化完成
      if (!agentStore.isInitialized) {
        await agentStore.initialize()
      }
      return '/agent'
    } catch (error) {
      console.error('获取智能体信息失败:', error)
      return '/agent'
    }
  }

  // 如果路由需要超级管理员权限但用户不是超级管理员
  if (requiresSuperAdmin && !isSuperAdmin) {
    try {
      const agentStore = useAgentStore()
      if (!agentStore.isInitialized) {
        await agentStore.initialize()
      }
      return '/agent'
    } catch (error) {
      console.error('获取智能体信息失败:', error)
      return '/agent'
    }
  }

  // 如果用户已登录但访问登录页，按 redirect 参数跳转
  if (to.path === '/login' && isLoggedIn) {
    return sanitizeRedirect(to.query.redirect)
  }

  // 其他情况正常导航
  return true
})

export default router
