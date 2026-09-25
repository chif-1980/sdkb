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
  vite: {
    plugins: [{
      name: 'vitepress-page-chunks-compat',
      enforce: 'post',
      generateBundle(_options, bundle) {
        // VitePress 1.x adds lean chunks by assigning to the bundle, which
        // Rolldown ignores. Emit the full page module for initial hydration.
        for (const chunk of Object.values(bundle)) {
          if (chunk.type !== 'chunk' || !chunk.facadeModuleId?.endsWith('.md')) continue
          const fileName = chunk.fileName.replace(/\.js$/, '.lean.js')
          if (!Object.values(bundle).some((entry) => entry.fileName === fileName)) {
            this.emitFile({ type: 'asset', fileName, source: chunk.code })
          }
        }
      }
    }]
  },
  markdown: {
    config: (md) => {
      md.use(markdownItTaskCheckbox)
      const renderLink = md.renderer.rules.link_open
      md.renderer.rules.link_open = (tokens, index, options, env, self) => {
        // Standalone diagram viewers must bypass the documentation SPA router.
        if (/\/diagrams\/[^/]+\.html$/.test(tokens[index].attrGet('href') || '')) {
          const href = tokens[index].attrGet('href')!
          if (href.startsWith('/diagrams/')) tokens[index].attrSet('href', `/sdkb${href}`)
          tokens[index].attrSet('target', '_blank')
          tokens[index].attrSet('rel', 'noopener')
        }
        return renderLink
          ? renderLink(tokens, index, options, env, self)
          : self.renderToken(tokens, index, options)
      }
    }
  },
  themeConfig: {
    logo: '/quickdone-mark.webp',
    siteTitle: '善达知枢',
    nav: [
      { text: '知枢手册', link: '/guide/zhishu-manual' },
      { text: '架构与流程图', link: '/guide/architecture' },
      { text: '知识加工', link: '/guide/knowledge-processing' },
      { text: '助手手册', link: '/guide/knowledge-assistant' },
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
        text: '产品使用手册',
        items: [
          { text: '善达知枢 · 管理员与审核人员', link: '/guide/zhishu-manual' },
          { text: '企业知识助手 · 员工', link: '/guide/knowledge-assistant' },
          { text: '会议管理与跟进', link: '/guide/zhishu-manual#meeting-management' }
        ]
      },
      {
        text: '开始使用',
        items: [
          { text: '产品概览', link: '/guide/overview' },
          { text: '系统架构与业务流程', link: '/guide/architecture' },
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
      message: '善达知枢基于 ZhiShu 开源项目构建。',
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
