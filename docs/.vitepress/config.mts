import { defineConfig } from 'vitepress'
import markdownItTaskCheckbox from 'markdown-it-task-checkbox'

export default defineConfig({
  lang: 'zh-CN',
  title: '善达知枢',
  description: '善达知枢企业知识管理与智能问答平台文档中心',
  base: '/sdkb/',
  srcExclude: ['implementation/**', 'superpowers/**', 'vibe/**'],
  head: [
    ['link', { rel: 'icon', type: 'image/webp', href: '/sdkb/quickdone-mark.webp' }],
    ['meta', { name: 'theme-color', content: '#087f96' }]
  ],
  ignoreDeadLinks: [/localhost/, /CONTRIBUTING$/, /docker-compose\.yml$/],
  markdown: {
    config: (md) => {
      md.use(markdownItTaskCheckbox)
    }
  },
  themeConfig: {
    logo: '/quickdone-mark.webp',
    siteTitle: '善达知枢',
    nav: [
      { text: '使用指南', link: '/guide/overview' },
      { text: '系统架构', link: '/guide/architecture' },
      { text: '知识加工', link: '/guide/knowledge-processing' },
      { text: '知识助手', link: '/guide/knowledge-assistant' },
      {
        text: '部署与配置',
        items: [
          { text: '生产部署', link: '/advanced/deployment' },
          { text: '系统配置', link: '/advanced/configuration' },
          { text: '第三方登录', link: '/advanced/third-party-auth' }
        ]
      }
    ],
    sidebar: [
      {
        text: '开始使用',
        items: [
          { text: '产品概览', link: '/guide/overview' },
          { text: '系统架构', link: '/guide/architecture' },
          { text: '术语定义', link: '/guide/terminology' },
          { text: '快速开始', link: '/intro/quick-start' }
        ]
      },
      {
        text: '知识管理',
        items: [
          { text: '知识加工', link: '/guide/knowledge-processing' },
          { text: '审核与发布', link: '/guide/review-and-publish' },
          { text: '运营操作', link: '/guide/operations' },
          { text: '知识助手', link: '/guide/knowledge-assistant' },
          { text: '知识库与知识图谱', link: '/intro/knowledge-base' }
        ]
      },
      {
        text: '系统配置',
        items: [
          { text: '模型配置', link: '/intro/model-config' },
          { text: '智能体配置', link: '/agents/agents-config' },
          { text: '品牌自定义', link: '/advanced/branding' },
          { text: '第三方登录', link: '/advanced/third-party-auth' }
        ]
      },
      {
        text: '部署与维护',
        items: [
          { text: '生产部署', link: '/advanced/deployment' },
          { text: '系统配置详解', link: '/advanced/configuration' },
          { text: '文档解析', link: '/advanced/document-processing' },
          { text: '其他配置', link: '/advanced/misc' }
        ]
      },
      {
        text: '开发与扩展',
        items: [
          { text: 'API Key 集成', link: '/advanced/api-key-integration' },
          { text: '工具系统', link: '/agents/tools-system' },
          { text: 'MCP 集成', link: '/agents/mcp-integration' },
          { text: 'Skills 管理', link: '/agents/skills-management' }
        ]
      }
    ],
    socialLinks: [{ icon: 'github', link: 'https://github.com/chif-1980/sdkb' }],
    footer: {
      message: '善达知枢基于 Yuxi 开源项目构建。',
      copyright: 'Copyright © 2026 Quickdone'
    },
    lastUpdated: {
      text: '最后更新时间',
      formatOptions: {
        dateStyle: 'long',
        timeStyle: 'short'
      }
    },
    search: {
      provider: 'local',
      options: {
        translations: {
          button: { buttonText: '搜索文档', buttonAriaLabel: '搜索文档' },
          modal: {
            noResultsText: '未找到相关内容',
            resetButtonTitle: '清除查询',
            footer: { selectText: '选择', navigateText: '切换', closeText: '关闭' }
          }
        }
      }
    },
    outline: { label: '本页内容', level: [2, 3] },
    docFooter: { prev: '上一页', next: '下一页' },
    returnToTopLabel: '返回顶部',
    sidebarMenuLabel: '目录',
    darkModeSwitchLabel: '外观'
  }
})
