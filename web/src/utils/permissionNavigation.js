export const PERMISSION_MENUS = [
  {
    module: 'workspace',
    menu: '工作区',
    path: '/workspace',
    description: '工作区文件与目录',
    icon: 'FolderKanban'
  },
  {
    module: 'extensions',
    menu: '智能体扩展',
    path: '/extensions',
    description: '知识库、工具与技能',
    icon: 'LibraryBig',
    children: [
      { module: 'knowledge', menu: '知识库', path: '智能体扩展 / 知识库详情' },
      { module: 'evaluation', menu: '知识评估', path: '智能体扩展 / 知识库 / RAG 评估' },
      { module: 'graph', menu: '知识图谱', path: '智能体扩展 / 知识库 / 图谱' }
    ]
  },
  {
    module: 'agents',
    menu: '智能体管理',
    path: '/model-manage',
    description: '智能体配置与维护',
    icon: 'Box'
  },
  {
    module: 'feishu_knowledge',
    menu: '知识加工',
    path: '/feishu-knowledge',
    description: '知识来源与扫描',
    icon: 'Workflow',
    children: [
      { module: 'governance', menu: '知识治理', path: '知识加工 / 审核任务、运营待办、正式知识' }
    ]
  },
  {
    module: 'meetings',
    menu: '会议管理',
    path: '/meeting-management',
    description: '会议、纪要与待办',
    icon: 'ClipboardList'
  },
  {
    module: 'feedback',
    menu: '用户反馈',
    path: '/feedbacks',
    description: '回答反馈的查看与处理',
    icon: 'MessageSquareText'
  },
  {
    module: 'dashboard',
    menu: '数据总览',
    path: '/dashboard',
    description: '使用情况与运行数据',
    icon: 'BarChart3'
  }
]
export const SYSTEM_PERMISSIONS = [
  { module: 'users', menu: '用户与部门', path: '设置 / 用户管理、部门管理' },
  { module: 'admin', menu: '系统管理', path: '设置 / 基本设置、角色权限' },
  { module: 'tasks', menu: '后台任务', path: '左下角 / 任务中心' },
  { module: 'models', menu: '模型配置', path: '智能体管理 / 模型供应商' }
]
