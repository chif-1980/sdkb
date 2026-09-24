<script setup>
import { computed, reactive, ref } from 'vue'
import {
  Check,
  ChevronDown,
  ChevronRight,
  CircleHelp,
  Eye,
  Layers3,
  LockKeyhole,
  Users
} from 'lucide-vue-next'

// Isolated, in-memory prototype. No stores, API clients or persistent storage.
const activeRoleKey = ref('meeting')
const activeTab = ref('menus')
const previewUserKey = ref('li')
const expanded = ref([])
const roleSearch = ref('')
const createOpen = ref(false)
const newName = ref('')
const notice = ref('')
const scopes = { self: '本人', department: '本部门', all: '全企业' }
const levels = ['不可访问', '仅查看', '可管理']
const tabs = [
  { key: 'menus', name: '菜单与操作', icon: Layers3 },
  { key: 'scope', name: '数据范围', icon: Eye },
  { key: 'members', name: '授权成员', icon: Users },
  { key: 'preview', name: '生效预览', icon: Check }
]
const menus = [
  { key: 'workspace', name: '工作区', help: '工作区文件与目录' },
  {
    key: 'extensions',
    name: '智能体扩展',
    help: '知识库、工具与技能',
    children: [
      { key: 'knowledgebase', name: '知识库', help: '知识库内容管理' },
      { key: 'evaluation', name: '知识评估', help: '知识库详情中的 RAG 评估' },
      { key: 'graph', name: '知识图谱', help: '知识库详情中的图谱' }
    ]
  },
  { key: 'agents', name: '智能体管理', help: '智能体配置与维护' },
  {
    key: 'knowledge',
    name: '知识加工',
    help: '知识来源与扫描',
    children: [{ key: 'governance', name: '知识治理', help: '审核任务、运营待办、正式知识' }]
  },
  { key: 'meetings', name: '会议管理', help: '会议、纪要与待办' },
  { key: 'feedback', name: '用户反馈', help: '回答反馈的查看与处理' },
  { key: 'dashboard', name: '数据总览', help: '使用情况与运行数据' }
]
const settings = [
  { key: 'users', name: '用户与部门', help: '设置 → 用户管理 / 部门管理' },
  { key: 'admin', name: '系统管理', help: '设置 → 基本设置 / 角色权限（管理）' },
  { key: 'models', name: '模型配置', help: '模型提供商配置' },
  { key: 'tasks', name: '后台任务', help: '左下角 → 任务中心' }
]
const allItems = [...menus.flatMap((item) => [item, ...(item.children || [])]), ...settings]
const roles = reactive([
  {
    key: 'superadmin',
    name: '超级管理员',
    description: '内置角色，全部权限只读展示',
    locked: true,
    scope: 'all',
    grants: Object.fromEntries(allItems.map((item) => [item.key, 2]))
  },
  {
    key: 'meeting',
    name: '会议运营',
    description: '管理会议待办，查看部门反馈',
    scope: 'department',
    grants: { workspace: 1, meetings: 2, feedback: 1 }
  },
  {
    key: 'editor',
    name: '知识编辑',
    description: '维护知识来源，审核正式知识',
    scope: 'self',
    grants: {
      workspace: 1,
      extensions: 1,
      knowledge: 2,
      knowledgebase: 2,
      governance: 2,
      graph: 1,
      evaluation: 1
    }
  },
  {
    key: 'viewer',
    name: '普通用户',
    description: '默认仅可对话，演示基础角色',
    scope: 'self',
    grants: {}
  }
])
const people = reactive([
  {
    key: 'root',
    name: '示例管理员',
    department: '企业管理',
    base: '超级管理员',
    direct: ['superadmin'],
    deptRoles: []
  },
  {
    key: 'zhang',
    name: '张宁',
    department: '项目部',
    base: '普通用户',
    direct: ['meeting'],
    deptRoles: []
  },
  {
    key: 'li',
    name: '李可',
    department: '创新应用事业部',
    base: '普通用户',
    direct: ['editor'],
    deptRoles: ['meeting']
  },
  { key: 'wang', name: '王晨', department: '市场部', base: '普通用户', direct: [], deptRoles: [] }
])
const currentRole = computed(() => roles.find((role) => role.key === activeRoleKey.value))
const visibleRoles = computed(() => roles.filter((role) => role.name.includes(roleSearch.value)))
const previewUser = computed(() => people.find((person) => person.key === previewUserKey.value))
const roleMembers = computed(() =>
  people.filter(
    (person) =>
      [...person.direct, ...person.deptRoles].includes(activeRoleKey.value) ||
      (activeRoleKey.value === 'viewer' && person.base === '普通用户')
  )
)
const previewGrants = computed(() =>
  menus.map((item) => {
    const sources = []
    const baseline = roles.find((role) => role.key === 'viewer')
    if (previewUser.value.base === '普通用户' && baseline.grants[item.key])
      sources.push({ role: baseline, origin: '账号基础角色' })
    for (const role of roles) {
      if (!role.grants[item.key]) continue
      if (previewUser.value.direct.includes(role.key))
        sources.push({ role, origin: '用户直接授权' })
      if (previewUser.value.deptRoles.includes(role.key))
        sources.push({ role, origin: `飞书部门 · ${previewUser.value.department}` })
    }
    const level = Math.max(0, ...sources.map((source) => source.role.grants[item.key]))
    const scope = ['self', 'department', 'all'][
      Math.max(
        0,
        ...sources.map((source) => ['self', 'department', 'all'].indexOf(source.role.scope))
      )
    ]
    return { ...item, level, sources, scope }
  })
)
function selectRole(key) {
  activeRoleKey.value = key
  notice.value = ''
}
function menuCount(role) {
  return menus.filter((item) => role.grants[item.key] > 0).length
}
function membersFor(role) {
  return people.filter(
    (person) =>
      [...person.direct, ...person.deptRoles].includes(role.key) ||
      (role.key === 'viewer' && person.base === '普通用户')
  ).length
}
function toggle(key) {
  expanded.value = expanded.value.includes(key)
    ? expanded.value.filter((item) => item !== key)
    : [...expanded.value, key]
}
function setLevel(item, value) {
  currentRole.value.grants[item.key] = Number(value)
  if (!Number(value))
    for (const child of item.children || []) currentRole.value.grants[child.key] = 0
  notice.value = '已更新原型草稿，可切换到生效预览查看影响。'
}
function createRole() {
  if (!newName.value || roles.some((role) => role.name === newName.value)) {
    notice.value = '请填写未使用的角色名称。'
    return
  }
  const key = `sample-${roles.length}`
  roles.push({
    key,
    name: newName.value,
    description: '自定义角色 · 尚未授权',
    scope: 'self',
    grants: {}
  })
  activeRoleKey.value = key
  activeTab.value = 'menus'
  newName.value = ''
  createOpen.value = false
  notice.value = '已在原型中创建角色，刷新页面后恢复示例。'
}
function assign(person, checked) {
  person.direct = checked
    ? [...person.direct, activeRoleKey.value]
    : person.direct.filter((key) => key !== activeRoleKey.value)
  notice.value = '已更新直接授权；部门继承的权限继续保留。'
}
</script>

<template>
  <main class="permission-prototype">
    <header class="prototype-header">
      <div>
        <div class="eyebrow"><Layers3 :size="14" /> 设置 / 角色权限</div>
        <h1>角色权限</h1>
        <p>选择角色 → 配置菜单和操作 → 分配成员 → 检查生效结果</p>
      </div>
      <div class="prototype-status">交互原型 · 示例数据 · 刷新重置</div>
    </header>
    <section class="prototype-layout">
      <aside class="role-rail" aria-label="角色列表">
        <div class="rail-heading">
          <strong>角色</strong
          ><button class="outline-button" @click="createOpen = !createOpen">＋ 新建</button>
        </div>
        <form v-if="createOpen" class="create-form" @submit.prevent="createRole">
          <label>角色名称<input v-model.trim="newName" maxlength="30" /></label
          ><button class="outline-button" type="submit">创建示例角色</button>
        </form>
        <input
          v-model="roleSearch"
          class="role-search"
          aria-label="搜索角色"
          placeholder="搜索角色"
        />
        <div class="role-list">
          <button
            v-for="role in visibleRoles"
            :key="role.key"
            class="role-card"
            :class="{ active: activeRoleKey === role.key }"
            :aria-pressed="activeRoleKey === role.key"
            @click="selectRole(role.key)"
          >
            <span class="role-card-top"
              ><strong>{{ role.name }}</strong
              ><LockKeyhole v-if="role.locked" :size="15" /><ChevronRight v-else :size="15"
            /></span>
            <span class="role-card-description">{{ role.description }}</span>
            <span class="role-card-meta"
              >{{ membersFor(role) }} 位示例成员 · {{ menuCount(role) }} 个菜单</span
            >
          </button>
          <p v-if="!visibleRoles.length" class="empty-note">没有匹配的角色</p>
        </div>
        <div class="rail-note">
          <CircleHelp :size="16" />
          <p>左侧为当前管理员的导航。右侧配置的是所选角色，实际菜单请查看「生效预览」。</p>
        </div>
      </aside>
      <div class="role-detail">
        <div class="detail-heading">
          <div>
            <h2>
              {{ currentRole.name }}
              <span v-if="currentRole.locked" class="role-state">不可修改</span>
            </h2>
            <p>{{ currentRole.description }}</p>
          </div>
          <button class="outline-button" @click="activeTab = 'members'">
            <Users :size="16" />{{ roleMembers.length }} 位成员
          </button>
        </div>
        <nav class="prototype-tabs" aria-label="权限视图">
          <button
            v-for="tab in tabs"
            :key="tab.key"
            :class="{ active: activeTab === tab.key }"
            :aria-pressed="activeTab === tab.key"
            @click="activeTab = tab.key"
          >
            <component :is="tab.icon" :size="16" />{{ tab.name }}
          </button>
        </nav>
        <section v-if="activeTab === 'menus'" class="tab-panel">
          <div class="panel-intro">
            <div>
              <h3>与主菜单一一对应</h3>
              <p>菜单名称与排列顺序保持一致；展开后配置页面内能力。</p>
            </div>
          </div>
          <div class="permission-legend">
            <span>不可访问：隐藏入口</span><span>仅查看：显示入口，可读</span
            ><span>可管理：同时允许修改</span>
          </div>
          <div class="menu-group">
            <template v-for="item in menus" :key="item.key">
              <article class="permission-row">
                <div class="menu-item-main">
                  <button
                    v-if="item.children"
                    class="expand-button"
                    :aria-label="`展开${item.name}页面内能力`"
                    :aria-expanded="expanded.includes(item.key)"
                    @click="toggle(item.key)"
                  >
                    <ChevronDown v-if="expanded.includes(item.key)" :size="16" /><ChevronRight
                      v-else
                      :size="16"
                    /></button
                  ><span v-else class="tree-spacer"></span>
                  <div>
                    <strong>{{ item.name }}</strong
                    ><small>{{ item.help }}</small>
                  </div>
                </div>
                <select
                  :aria-label="`${item.name}权限`"
                  :value="currentRole.grants[item.key] || 0"
                  :disabled="currentRole.locked"
                  @change="setLevel(item, $event.target.value)"
                >
                  <option v-for="(level, index) in levels" :key="level" :value="index">
                    {{ level }}
                  </option>
                </select>
              </article>
              <div v-if="item.children && expanded.includes(item.key)" class="child-permissions">
                <article
                  v-for="child in item.children"
                  :key="child.key"
                  class="permission-row child"
                >
                  <div>
                    <strong>{{ child.name }}</strong
                    ><small>{{ child.help }}</small>
                  </div>
                  <select
                    :aria-label="`${child.name}权限`"
                    :value="currentRole.grants[child.key] || 0"
                    :disabled="currentRole.locked || !currentRole.grants[item.key]"
                    @change="setLevel(child, $event.target.value)"
                  >
                    <option v-for="(level, index) in levels" :key="level" :value="index">
                      {{ level }}
                    </option>
                  </select>
                </article>
                <p v-if="!currentRole.grants[item.key]" class="child-hint">
                  先开放「{{ item.name }}」入口，再配置页面内能力。
                </p>
              </div>
            </template>
          </div>
          <details class="additional-permissions">
            <summary>其他入口与系统设置 · {{ settings.length }} 项</summary>
            <article v-for="item in settings" :key="item.key" class="permission-row">
              <div>
                <strong>{{ item.name }}</strong
                ><small>{{ item.help }}</small>
              </div>
              <select
                :aria-label="`${item.name}权限`"
                :value="currentRole.grants[item.key] || 0"
                :disabled="currentRole.locked"
                @change="setLevel(item, $event.target.value)"
              >
                <option v-for="(level, index) in levels" :key="level" :value="index">
                  {{ level }}
                </option>
              </select>
            </article>
          </details>
          <p class="empty-note">创建新对话、搜索本人对话和个人账户设置为登录后的固定入口。</p>
        </section>
        <section v-else-if="activeTab === 'scope'" class="tab-panel">
          <div class="panel-intro">
            <div>
              <h3>可以查看哪些数据</h3>
              <p>数据范围与菜单访问分开配置。当前仅用户反馈支持以下细分范围。</p>
            </div>
          </div>
          <article class="scope-editor">
            <h4>用户反馈 · 查看范围</h4>
            <p v-if="!currentRole.grants.feedback">
              尚未开放用户反馈入口，请先在「菜单与操作」中授权。
            </p>
            <label v-for="(label, value) in scopes" :key="value" class="scope-choice"
              ><input
                v-model="currentRole.scope"
                type="radio"
                :value="value"
                :disabled="currentRole.locked || !currentRole.grants.feedback"
                name="feedback-scope"
              /><span
                ><strong>{{ label }}</strong
                ><small>{{
                  value === 'self'
                    ? '仅自己提交的反馈'
                    : value === 'department'
                      ? '按系统归属部门筛选的反馈'
                      : '本企业内所有反馈'
                }}</small></span
              ></label
            >
          </article>
          <div class="explain-strip">
            <CircleHelp :size="17" />
            <div>
              <strong>其他模块保持现有数据规则</strong
              ><span
                >会议管理等页面本轮不新增「本人 /
                本部门」筛选。查看授权也不会改变飞书原文的访问权限。</span
              >
            </div>
          </div>
          <p class="empty-note">
            「可管理」沿用现有模块管理权限，不代表管理操作也按此范围收窄；正式实施需要校验该组合的权限边界。
          </p>
        </section>
        <section v-else-if="activeTab === 'members'" class="tab-panel">
          <div class="panel-intro">
            <div>
              <h3>谁会获得这个角色</h3>
              <p>用户直接授权与飞书部门继承分开显示，移除直接授权不会取消部门继承。</p>
            </div>
          </div>
          <h4 class="subheading">用户直接授权</h4>
          <article
            v-for="person in people.filter((person) => person.key !== 'root')"
            :key="person.key"
            class="assignment-item"
          >
            <label
              ><input
                type="checkbox"
                :aria-label="`给${person.name}直接授权${currentRole.name}`"
                :checked="person.direct.includes(currentRole.key)"
                :disabled="currentRole.locked || currentRole.key === 'viewer'"
                @change="assign(person, $event.target.checked)"
              /><span
                ><strong>{{ person.name }}</strong
                ><small>{{ person.department }} · 账号基础角色：{{ person.base }}</small></span
              ></label
            ><span class="source-badge" v-if="person.deptRoles.includes(currentRole.key)"
              >同时从飞书部门继承</span
            ><span class="empty-note" v-else-if="!person.direct.includes(currentRole.key)">{{
              currentRole.key === 'viewer' ? '账号基础角色' : '未直接授权此角色'
            }}</span>
          </article>
          <h4 class="subheading">飞书部门继承</h4>
          <article v-if="currentRole.key === 'meeting'" class="department-card">
            <strong>企业根部门 / 创新应用事业部</strong>
            <p>部门角色：会议运营 · 示例成员：李可</p>
            <span class="source-badge">来自已同步的飞书组织架构</span
            ><small
              >角色映射由知枢管理员配置；飞书提供部门与成员关系，不自动同步飞书管理员权限。</small
            >
          </article>
          <p v-else class="empty-note">此示例角色未绑定飞书部门。</p>
          <div class="explain-strip">
            <CircleHelp :size="17" />
            <div>
              <strong>部门映射入口放在这里</strong
              ><span
                >正式设计将支持搜索已同步部门并绑定角色，显示同步时间和失败状态。本原型仅演示已有部门继承。</span
              >
            </div>
          </div>
        </section>
        <section v-else class="tab-panel">
          <div class="panel-intro">
            <div>
              <h3>按用户检查最终权限</h3>
              <p>这里查看用户全部角色的合并结果，包含其他角色的授权。</p>
            </div>
          </div>
          <label class="preview-select"
            >选择示例用户<select v-model="previewUserKey">
              <option v-for="person in people" :key="person.key" :value="person.key">
                {{ person.name }} · {{ person.department }}
              </option>
            </select></label
          >
          <div class="preview-user">
            <span class="avatar large">{{ previewUser.name.slice(0, 1) }}</span>
            <div>
              <strong>{{ previewUser.name }}</strong
              ><span>基础角色：{{ previewUser.base }}</span
              ><span
                >直接角色：{{
                  previewUser.direct
                    .map((key) => roles.find((role) => role.key === key).name)
                    .join('、') || '未单独授权'
                }}</span
              ><span
                >部门继承：{{
                  previewUser.deptRoles
                    .map((key) => roles.find((role) => role.key === key).name)
                    .join('、') || '无'
                }}</span
              >
            </div>
          </div>
          <div class="preview-columns">
            <aside class="simulated-sidebar">
              <strong>该用户看到的主菜单</strong><span>创建新对话</span><span>搜索对话</span
              ><span v-for="item in previewGrants.filter((item) => item.level > 0)" :key="item.key"
                ><Check :size="13" />{{ item.name }}</span
              >
              <p v-if="!previewGrants.some((item) => item.level)" class="empty-note">
                未获得业务菜单授权
              </p>
            </aside>
            <div class="source-results">
              <article v-for="item in previewGrants" :key="item.key" class="grant-result">
                <div>
                  <strong>{{ item.name }}</strong
                  ><span :class="{ granted: item.level > 0 }">{{ levels[item.level] }}</span>
                </div>
                <p v-for="source in item.sources" :key="source.role.key + source.origin">
                  {{ source.origin }} → {{ source.role.name }}
                </p>
                <small v-if="item.key === 'feedback' && item.level"
                  >查看范围：{{ scopes[item.scope] }}</small
                >
                <p v-if="!item.level">所有来源均未授权</p>
              </article>
            </div>
          </div>
          <div class="explain-strip">
            <CircleHelp :size="17" />
            <div>
              <strong>多个角色的授权取并集</strong
              ><span
                >在一个角色里设为「不可访问」表示该角色不授予权限，不会否决另一个角色已经授予的权限。</span
              >
            </div>
          </div>
        </section>
        <footer class="prototype-footer">
          <span role="status">{{ notice || '可以直接试用；全部操作只影响本页示例。' }}</span
          ><button class="outline-button" @click="activeTab = 'preview'">查看生效结果</button>
        </footer>
      </div>
    </section>
  </main>
</template>
<style scoped lang="less">
.permission-prototype {
  padding: 26px;
  background: var(--gray-25);
  color: var(--gray-900);
  min-width: 0;
  h1 {
    font-size: 24px;
    font-weight: 600;
    margin: 8px 0 4px;
  }
  h2 {
    font-size: 21px;
    font-weight: 600;
    margin: 0 0 5px;
  }
  h3 {
    font-size: 17px;
    font-weight: 600;
    margin: 0 0 5px;
  }
  h4 {
    font-size: 14px;
    font-weight: 600;
  }
  strong {
    font-weight: 500;
  }
  button,
  input,
  select {
    font: inherit;
  }
  button {
    cursor: pointer;
  }
  select,
  input:not([type='radio']):not([type='checkbox']) {
    font-size: 13px;
    color: var(--gray-900);
    background: var(--gray-0);
    border: 1px solid var(--gray-300);
    border-radius: 6px;
    padding: 7px 9px;
  }
  input[type='checkbox'],
  input[type='radio'] {
    width: 16px;
    height: 16px;
    flex-shrink: 0;
    accent-color: var(--main-700);
  }
  :focus-visible {
    outline: 2px solid var(--main-600);
    outline-offset: 3px;
  }
  button:disabled,
  select:disabled {
    cursor: not-allowed;
    opacity: 0.6;
  }
}
.prototype-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 18px;
  margin: 0 auto 22px;
  max-width: 1340px;
  p {
    color: var(--gray-600);
    font-size: 13px;
  }
}
.eyebrow {
  display: flex;
  gap: 6px;
  align-items: center;
  color: var(--main-700);
  font-size: 12px;
}
.prototype-status {
  color: var(--main-700);
  font-size: 12px;
  border: 1px solid var(--gray-200);
  padding: 7px 10px;
  border-radius: 6px;
  white-space: nowrap;
  background: var(--gray-0);
}
.prototype-layout {
  max-width: 1340px;
  min-height: 650px;
  margin: auto;
  display: grid;
  grid-template-columns: 216px minmax(0, 1fr);
  border: 1px solid var(--gray-200);
  border-radius: 10px;
  overflow: hidden;
  background: var(--gray-0);
}
.role-rail {
  padding: 20px 12px;
  background: var(--gray-25);
  border-right: 1px solid var(--gray-200);
}
.rail-heading,
.role-card-top,
.detail-heading,
.panel-intro {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 12px;
}
.rail-heading {
  padding: 0 5px;
}
.outline-button {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 6px;
  font-size: 12px !important;
  padding: 7px 10px;
  border: 1px solid var(--gray-200);
  border-radius: 6px;
  background: var(--gray-0);
  color: var(--main-700);
  white-space: nowrap;
  &:hover {
    border-color: var(--main-500);
  }
}
.role-search {
  margin-top: 16px;
  width: 100%;
}
.create-form {
  padding: 12px 0;
  font-size: 13px;
  input {
    width: 100%;
    margin: 5px 0 8px;
  }
}
.role-list {
  display: grid;
  gap: 7px;
  margin-top: 15px;
}
.role-card {
  width: 100%;
  padding: 12px;
  border: 1px solid transparent;
  border-radius: 7px;
  background: transparent;
  color: var(--gray-900);
  text-align: left;
  &:hover {
    background: var(--gray-100);
  }
  &.active {
    border-color: var(--main-500);
    background: var(--main-30);
  }
}
.role-card-top {
  font-size: 14px;
  svg {
    color: var(--gray-500);
  }
}
.role-card-description,
.role-card-meta {
  display: block;
  color: var(--gray-600);
  font-size: 12px;
  margin-top: 6px;
}
.rail-note {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  font-size: 12px;
  color: var(--gray-600);
  padding: 20px 6px 0;
  margin-top: 16px;
  border-top: 1px solid var(--gray-200);
  svg {
    flex-shrink: 0;
    margin-top: 2px;
  }
}
.role-detail {
  padding: 24px;
  min-width: 0;
}
.detail-heading p {
  font-size: 13px;
  color: var(--gray-600);
}
.role-state {
  font-size: 12px;
  padding: 3px 6px;
  color: var(--gray-600);
  background: var(--gray-100);
  border-radius: 4px;
}
.prototype-tabs {
  display: flex;
  flex-wrap: wrap;
  gap: 2px;
  padding-bottom: 12px;
  border-bottom: 1px solid var(--gray-200);
  margin: 22px 0;
  button {
    display: inline-flex;
    align-items: center;
    gap: 7px;
    border: 0;
    border-radius: 6px;
    background: transparent;
    color: var(--gray-600);
    padding: 9px;
    font-size: 13px;
    &:hover {
      background: var(--gray-50);
    }
    &.active {
      background: var(--main-700);
      color: var(--gray-0);
    }
  }
}
.tab-panel {
  min-height: 380px;
}
.panel-intro {
  margin-bottom: 16px;
  p {
    font-size: 12px;
    color: var(--gray-600);
  }
}
.permission-legend {
  display: flex;
  flex-wrap: wrap;
  gap: 12px;
  font-size: 12px;
  color: var(--gray-600);
  margin-bottom: 12px;
}
.menu-group {
  border: 1px solid var(--gray-200);
  border-radius: 8px;
  overflow: hidden;
}
.permission-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 12px 14px;
  border-bottom: 1px solid var(--gray-150);
  &:last-child {
    border-bottom: 0;
  }
  strong {
    font-size: 14px;
  }
  small {
    display: block;
    color: var(--gray-600);
    font-size: 12px;
    margin-top: 2px;
  }
  select {
    min-width: 112px;
    flex-shrink: 0;
  }
}
.menu-item-main {
  display: flex;
  align-items: center;
  gap: 8px;
}
.tree-spacer {
  width: 24px;
  flex-shrink: 0;
}
.expand-button {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 24px;
  height: 28px;
  flex-shrink: 0;
  border: 0;
  background: transparent;
  color: var(--gray-600);
}
.child-permissions {
  border-left: 2px solid var(--gray-200);
  margin-left: 28px;
  background: var(--gray-25);
}
.child-hint {
  padding: 4px 14px 10px;
  color: var(--gray-600);
  font-size: 12px;
}
.additional-permissions {
  margin-top: 12px;
  border: 1px solid var(--gray-200);
  border-radius: 8px;
  summary {
    cursor: pointer;
    padding: 12px 14px;
    font-size: 13px;
  }
}
.empty-note {
  color: var(--gray-600);
  font-size: 12px;
  margin: 12px 0;
}
.scope-editor {
  padding: 18px;
  border: 1px solid var(--gray-200);
  border-radius: 8px;
  > p {
    margin-top: 10px;
    font-size: 13px;
    color: var(--gray-600);
  }
}
.scope-choice {
  display: flex;
  align-items: center;
  gap: 12px;
  padding-top: 18px;
  cursor: pointer;
  strong {
    font-size: 14px;
  }
  small {
    display: block;
    color: var(--gray-600);
    font-size: 12px;
    margin-top: 2px;
  }
}
.explain-strip {
  display: flex;
  gap: 10px;
  align-items: flex-start;
  margin-top: 18px;
  padding: 13px;
  border-left: 3px solid var(--main-500);
  background: var(--main-30);
  svg {
    flex-shrink: 0;
    color: var(--main-700);
  }
  strong,
  span {
    display: block;
    font-size: 12px;
  }
  strong {
    color: var(--main-800);
  }
  span {
    color: var(--gray-600);
    margin-top: 4px;
  }
}
.subheading {
  margin: 20px 0 10px;
}
.assignment-item {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 14px;
  padding: 13px 0;
  border-bottom: 1px solid var(--gray-150);
  label {
    display: flex;
    align-items: center;
    gap: 10px;
    cursor: pointer;
  }
  strong {
    font-size: 14px;
  }
  small {
    display: block;
    color: var(--gray-600);
    font-size: 12px;
    margin-top: 2px;
  }
}
.source-badge {
  display: inline-block;
  border-radius: 4px;
  padding: 4px 6px;
  font-size: 12px;
  color: var(--main-700);
  background: var(--main-30);
}
.department-card {
  border: 1px solid var(--gray-200);
  border-radius: 8px;
  padding: 16px;
  font-size: 14px;
  p {
    margin: 6px 0;
    color: var(--gray-600);
    font-size: 13px;
  }
  small {
    display: block;
    margin-top: 10px;
    color: var(--gray-600);
    font-size: 12px;
  }
}
.preview-select {
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
  align-items: center;
  margin-bottom: 14px;
  font-size: 13px;
}
.preview-user {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 16px;
  border: 1px solid var(--gray-200);
  border-radius: 8px;
  background: var(--gray-25);
  strong {
    display: block;
    font-size: 14px;
  }
  div > span {
    display: block;
    font-size: 12px;
    color: var(--gray-600);
    margin-top: 3px;
  }
}
.avatar {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 38px;
  height: 38px;
  border-radius: 50%;
  color: var(--gray-0);
  background: var(--main-600);
  flex-shrink: 0;
}
.preview-columns {
  display: grid;
  grid-template-columns: 180px minmax(0, 1fr);
  gap: 22px;
  margin-top: 18px;
}
.simulated-sidebar {
  border: 1px solid var(--gray-200);
  border-radius: 8px;
  padding: 14px;
  align-self: start;
  background: var(--gray-25);
  strong {
    display: block;
    font-size: 12px;
    color: var(--gray-600);
    margin-bottom: 12px;
  }
  > span {
    display: flex;
    gap: 6px;
    align-items: center;
    padding: 8px 0;
    font-size: 13px;
  }
}
.grant-result {
  padding: 10px 0;
  border-bottom: 1px solid var(--gray-150);
  > div {
    display: flex;
    justify-content: space-between;
    gap: 8px;
    font-size: 13px;
    span {
      color: var(--gray-600);
    }
    .granted {
      color: var(--main-700);
      font-weight: 500;
    }
  }
  p,
  small {
    font-size: 12px;
    color: var(--gray-600);
    margin-top: 5px;
  }
}
.prototype-footer {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 12px;
  border-top: 1px solid var(--gray-200);
  margin-top: 24px;
  padding-top: 16px;
  > span {
    font-size: 12px;
    color: var(--gray-600);
  }
}
@media (max-width: 1100px) {
  .permission-prototype {
    padding: 20px;
  }
  .prototype-layout {
    grid-template-columns: 190px minmax(0, 1fr);
  }
  .role-detail {
    padding: 18px;
  }
  .preview-columns {
    grid-template-columns: 1fr;
  }
  .simulated-sidebar {
    display: flex;
    flex-wrap: wrap;
    gap: 12px;
    strong {
      width: 100%;
      margin: 0;
    }
  }
}
@media (max-width: 850px) {
  .prototype-layout {
    grid-template-columns: 1fr;
  }
  .role-list {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
  .role-rail {
    border-right: 0;
    border-bottom: 1px solid var(--gray-200);
  }
  .assignment-item {
    flex-wrap: wrap;
  }
  .prototype-header {
    flex-wrap: wrap;
  }
}
@media (max-width: 600px) {
  .permission-prototype {
    padding: 12px;
  }
  .role-detail {
    padding: 14px;
  }
  .role-list {
    grid-template-columns: 1fr;
  }
  .prototype-footer {
    flex-wrap: wrap;
  }
}
</style>
