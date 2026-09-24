<template>
  <section class="role-permissions">
    <header class="permissions-header">
      <div>
        <span class="section-kicker">设置 / 角色权限</span>
        <h2>角色权限</h2>
        <p>先选择角色，再配置菜单、页面内能力和成员来源。生效预览会显示用户最终获得的权限。</p>
      </div>
      <div class="header-actions">
        <button
          type="button"
          class="secondary-button"
          @click="reload"
          :disabled="loading || saving"
        >
          刷新
        </button>
        <form class="create-role" @submit.prevent="createRole">
          <input
            v-model.trim="newRoleName"
            aria-label="新角色名称"
            placeholder="新角色名称"
            maxlength="100"
          />
          <button type="submit" class="primary-button" :disabled="!newRoleName || saving">
            创建角色
          </button>
        </form>
      </div>
    </header>

    <a-alert v-if="error" type="error" :message="error" show-icon />
    <a-spin :spinning="loading">
      <template v-if="!loading && !error">
        <div class="permissions-workspace">
          <aside class="role-list-panel" aria-label="角色列表">
            <div class="role-list-heading">
              <div>
                <span class="section-kicker">角色</span
                ><strong>{{ filteredRoles.length }} 个角色</strong>
              </div>
              <span class="role-count">{{ data.roles.length }} 个</span>
            </div>
            <input
              v-model.trim="roleSearch"
              class="role-search"
              aria-label="搜索角色"
              placeholder="搜索角色"
            />
            <div class="role-list">
              <button
                v-for="role in filteredRoles"
                :key="role.role_key"
                type="button"
                class="role-card"
                :class="{ active: selectedRoleKey === role.role_key }"
                :aria-pressed="selectedRoleKey === role.role_key"
                @click="selectRole(role.role_key)"
              >
                <span class="role-card-title"
                  ><strong>{{ role.name }}</strong
                  ><span v-if="role.is_builtin" class="builtin-tag">内置</span></span
                >
                <span class="role-card-description">{{
                  role.description || '自定义角色，尚未填写说明'
                }}</span>
                <span class="role-card-meta"
                  >{{ roleUserCount(role) }} 位用户 · {{ roleDepartmentCount(role) }} 个部门 ·
                  {{ roleMenuCount(role) }} 个入口</span
                >
              </button>
              <p v-if="!filteredRoles.length" class="empty-state">没有匹配角色</p>
            </div>
            <div class="role-list-help">
              <span class="help-icon">?</span>
              <p>左侧主导航属于当前管理员。这里配置所选角色，实际菜单请查看“生效预览”。</p>
            </div>
          </aside>

          <div v-if="selectedRole" class="role-detail-panel">
            <div class="role-detail-heading">
              <div>
                <span class="section-kicker">当前角色</span>
                <h3>
                  {{ selectedRole.name }}
                  <span v-if="selectedRole.is_builtin" class="status-tag">内置角色</span>
                </h3>
                <p>{{ selectedRole.description || '配置此角色可访问的菜单和页面能力。' }}</p>
              </div>
              <div class="detail-actions">
                <span class="member-summary"
                  >{{ roleUserCount(selectedRole) }} 位用户 ·
                  {{ roleDepartmentCount(selectedRole) }} 个部门</span
                ><button
                  v-if="!selectedRole.is_builtin"
                  type="button"
                  class="danger-button"
                  :disabled="saving"
                  @click="removeRole"
                >
                  删除角色
                </button>
              </div>
            </div>

            <nav class="permission-tabs" aria-label="权限视图">
              <button
                type="button"
                :class="{ active: activeTab === 'menus' }"
                :aria-pressed="activeTab === 'menus'"
                @click="activeTab = 'menus'"
              >
                <Layers3 :size="16" />菜单与操作
              </button>
              <button
                type="button"
                :class="{ active: activeTab === 'scope' }"
                :aria-pressed="activeTab === 'scope'"
                @click="activeTab = 'scope'"
              >
                <Eye :size="16" />数据范围
              </button>
              <button
                type="button"
                :class="{ active: activeTab === 'members' }"
                :aria-pressed="activeTab === 'members'"
                @click="activeTab = 'members'"
              >
                <Users :size="16" />授权成员
              </button>
              <button
                type="button"
                :class="{ active: activeTab === 'preview' }"
                :aria-pressed="activeTab === 'preview'"
                @click="activeTab = 'preview'"
              >
                <Check :size="16" />生效预览
              </button>
            </nav>

            <section v-if="activeTab === 'menus'" class="permission-view">
              <div class="view-heading">
                <div>
                  <h4>与主菜单一一对应</h4>
                  <p>菜单名称和顺序与左侧主菜单保持一致；展开后配置页面内能力。</p>
                </div>
                <span class="permission-legend"
                  >不可访问：隐藏入口 · 仅查看：只读 · 可管理：允许修改</span
                >
              </div>
              <div v-for="group in permissionGroups" :key="group.key" class="permission-group">
                <button type="button" class="group-heading" @click="toggleGroup(group.key)">
                  <span
                    ><ChevronDown v-if="expandedGroups.has(group.key)" :size="16" /><ChevronRight
                      v-else
                      :size="16"
                    />{{ group.name }}</span
                  ><small>{{ group.items.length }} 项</small>
                </button>
                <div v-if="expandedGroups.has(group.key)">
                  <template v-for="item in group.items" :key="item.module">
                    <div class="permission-row">
                      <div class="permission-label">
                        <button
                          v-if="item.children"
                          type="button"
                          class="expand-button"
                          :aria-label="`展开${item.menu}页面内能力`"
                          :aria-expanded="expandedItems.has(item.module)"
                          @click="toggleItem(item.module)"
                        >
                          <ChevronDown
                            v-if="expandedItems.has(item.module)"
                            :size="15"
                          /><ChevronRight v-else :size="15" /></button
                        ><span v-else class="tree-spacer"></span
                        ><span
                          ><strong>{{ item.menu }}</strong
                          ><small>{{ item.description || item.path }}</small></span
                        >
                      </div>
                      <select
                        :aria-label="`${item.menu}权限`"
                        :value="levelFor(item.module)"
                        :disabled="permissionsReadOnly || saving"
                        @change="setLevel(item, $event.target.value)"
                      >
                        <option v-for="(level, index) in levels" :key="level" :value="index">
                          {{ level }}
                        </option>
                      </select>
                    </div>
                    <div
                      v-if="item.children && expandedItems.has(item.module)"
                      class="child-permissions"
                    >
                      <div
                        v-for="child in item.children"
                        :key="child.module"
                        class="permission-row child"
                      >
                        <div class="permission-label">
                          <span class="tree-spacer"></span
                          ><span
                            ><strong>{{ child.menu }}</strong
                            ><small>{{ child.path }}</small></span
                          >
                        </div>
                        <select
                          :aria-label="`${child.menu}权限`"
                          :value="levelFor(child.module)"
                          :disabled="permissionsReadOnly || saving || levelFor(item.module) === 0"
                          @change="setLevel(child, $event.target.value)"
                        >
                          <option v-for="(level, index) in levels" :key="level" :value="index">
                            {{ level }}
                          </option>
                        </select>
                      </div>
                      <p v-if="levelFor(item.module) === 0" class="child-hint">
                        先开放“{{ item.menu }}”入口，再配置页面内能力。
                      </p>
                    </div>
                  </template>
                </div>
              </div>
              <details class="system-permissions">
                <summary>其他入口与系统设置 · {{ systemItems.length }} 项</summary>
                <div v-for="item in systemItems" :key="item.module" class="permission-row">
                  <div class="permission-label">
                    <span class="tree-spacer"></span
                    ><span
                      ><strong>{{ item.menu }}</strong
                      ><small>{{ item.description || item.path }}</small></span
                    >
                  </div>
                  <select
                    :aria-label="`${item.menu}权限`"
                    :value="levelFor(item.module)"
                    :disabled="permissionsReadOnly || saving"
                    @change="setLevel(item, $event.target.value)"
                  >
                    <option v-for="(level, index) in levels" :key="level" :value="index">
                      {{ level }}
                    </option>
                  </select>
                </div>
              </details>
              <p class="fixed-entry-note">
                创建新对话、搜索对话和个人账户设置是登录后的固定入口，不参与角色隐藏。
              </p>
            </section>

            <section v-else-if="activeTab === 'scope'" class="permission-view">
              <div class="view-heading">
                <div>
                  <h4>可以查看哪些数据</h4>
                  <p>数据范围和菜单访问分开配置，避免把“能否进入”和“能看哪些数据”混在一起。</p>
                </div>
              </div>
              <article class="scope-card">
                <h5>用户反馈 · 查看范围</h5>
                <p v-if="!hasView('feedback')" class="scope-disabled">
                  当前角色未开放用户反馈入口，请先在“菜单与操作”中授权。
                </p>
                <label v-for="scope in feedbackScopes" :key="scope.value" class="scope-option"
                  ><input
                    v-model="permissions['feedback.view']"
                    type="radio"
                    :value="scope.value"
                    :disabled="
                      permissionsReadOnly ||
                      !hasView('feedback') ||
                      saving ||
                      (levelFor('feedback') === 2 && scope.value !== 'all')
                    "
                  /><span
                    ><strong>{{ scope.label }}</strong
                    ><small>{{ scope.description }}</small></span
                  ></label
                >
              </article>
              <div class="info-strip">
                <CircleHelp :size="16" />
                <div>
                  <strong>其他模块沿用现有数据规则</strong
                  ><span
                    >此处配置用户反馈的查看范围。会议、知识加工等模块沿用各自现有的数据访问规则。</span
                  >
                </div>
              </div>
            </section>

            <section v-else-if="activeTab === 'members'" class="permission-view">
              <div class="view-heading">
                <div>
                  <h4>谁会获得这个角色</h4>
                  <p>用户直接授权和飞书部门继承分开显示；同一用户可以同时来自多个来源。</p>
                </div>
              </div>
              <p v-if="dirty" class="info-strip">
                请先保存或放弃权限草稿，再修改成员；成员勾选立即生效。
              </p>
              <h5 class="subheading">用户直接授权</h5>
              <input
                v-model="memberSearch"
                aria-label="搜索用户"
                placeholder="搜索姓名或账号"
                class="role-search"
              />
              <div class="assignment-list">
                <label v-for="person in pagedMembers" :key="person.id" class="assignment-row"
                  ><span class="assignment-person"
                    ><input
                      type="checkbox"
                      :aria-label="`给${person.username}直接授权${selectedRole.name}`"
                      :checked="
                        selectedRole.is_builtin
                          ? person.role === selectedRole.role_key
                          : userHasRole(person.id, selectedRole.role_key)
                      "
                      :key="`${person.id}-${saving}`"
                      :disabled="
                        selectedRole.is_builtin || person.role === 'superadmin' || dirty || saving
                      "
                      @change="assignUser(person.id, $event)"
                    /><span
                      ><strong>{{ person.username }}</strong
                      ><small
                        >{{ person.uid }} · 账号身份：{{
                          { superadmin: '超级管理员', admin: '管理员', user: '普通用户' }[
                            person.role
                          ] || person.role
                        }}</small
                      ></span
                    ></span
                  ><span v-if="inherited(person.id)" class="source-tag feishu">从飞书部门继承</span
                  ><span v-if="selectedRole.is_builtin" class="unassigned-tag">{{
                    person.role === selectedRole.role_key ? '当前账号身份' : '其他账号身份'
                  }}</span
                  ><span
                    v-else-if="userHasRole(person.id, selectedRole.role_key)"
                    class="source-tag"
                    >直接绑定</span
                  ><span v-else class="unassigned-tag">未直接授权此角色</span></label
                >
                <p v-if="!filteredMembers.length" class="empty-state">没有匹配用户</p>
              </div>
              <div class="pagination">
                <button :disabled="memberPage <= 1" @click="memberPage--">上一页</button
                ><span
                  >{{ memberPage }} / {{ Math.max(1, Math.ceil(filteredMembers.length / 20)) }} ·
                  {{ filteredMembers.length }} 位用户</span
                ><button
                  :disabled="memberPage * 20 >= filteredMembers.length"
                  @click="memberPage++"
                >
                  下一页
                </button>
              </div>
              <h5 class="subheading">飞书部门继承</h5>
              <input
                v-model="departmentSearch"
                aria-label="搜索飞书部门"
                placeholder="搜索部门名称或企业标识"
                class="role-search"
              />
              <div class="assignment-list">
                <label
                  v-for="department in pagedDepartments"
                  :key="department.id"
                  class="assignment-row"
                  ><span class="assignment-person"
                    ><input
                      type="checkbox"
                      :aria-label="`给${department.name}绑定${selectedRole.name}`"
                      :checked="departmentHasRole(department.id, selectedRole.role_key)"
                      :key="`${department.id}-${saving}`"
                      :disabled="selectedRole.is_builtin || dirty || saving"
                      @change="assignDepartment(department.id, $event)"
                    /><span
                      ><strong>{{ department.name }}</strong
                      ><small
                        >企业：{{ department.tenant_key }} · 部门 ID：{{
                          department.feishu_department_id
                        }}<br />部门记录更新：{{ dateLabel(department.updated_at)
                        }}<br />成员关系更新：{{ dateLabel(department.membership_updated_at) }} ·
                        {{ department.user_ids?.length || 0 }} 位已同步成员</small
                      ></span
                    ></span
                  ><span
                    v-if="departmentHasRole(department.id, selectedRole.role_key)"
                    class="source-tag feishu"
                    >飞书部门继承</span
                  ><span v-else class="unassigned-tag">未映射此角色</span></label
                >
                <p v-if="!filteredDepartments.length" class="empty-state">
                  {{
                    data.feishu_departments.length ? '没有匹配部门' : '飞书部门同步后会显示在这里'
                  }}
                </p>
              </div>
              <div class="pagination" aria-label="飞书部门分页">
                <button :disabled="departmentPage <= 1" @click="departmentPage--">上一页</button>
                <span
                  >{{ departmentPage }} /
                  {{ Math.max(1, Math.ceil(filteredDepartments.length / 20)) }} ·
                  {{ filteredDepartments.length }} 个部门</span
                >
                <button
                  :disabled="departmentPage * 20 >= filteredDepartments.length"
                  @click="departmentPage++"
                >
                  下一页
                </button>
              </div>
              <div class="info-strip">
                <CircleHelp :size="16" />
                <div>
                  <strong>飞书部门角色由知枢管理员映射</strong
                  ><span
                    >只关联当前部门的已同步成员，不递归授权子部门。成员关系通过飞书登录同步；时间为本地记录更新时间，并非全量组织同步完成时间。不会复制飞书管理员或原文访问权限。</span
                  >
                </div>
              </div>
            </section>

            <section v-else class="permission-view">
              <div class="view-heading">
                <div>
                  <h4>按用户检查最终权限</h4>
                  <p>这里查看用户全部角色合并后的结果，并标出权限来源。</p>
                </div>
                <label class="preview-user-select"
                  >选择用户<select v-model="previewUserId" aria-label="选择预览用户">
                    <option v-for="person in data.users" :key="person.id" :value="person.id">
                      {{ person.username }} · {{ person.uid }}
                    </option>
                  </select></label
                >
              </div>
              <p v-if="previewLoading" role="status">正在计算生效权限…</p>
              <p v-else-if="previewError" role="alert">
                {{ previewError }} <button @click="loadPreview">重试</button>
              </p>
              <template v-else-if="previewUser && preview">
                <div class="preview-user">
                  <span class="avatar">{{ previewUser.username.slice(0, 1) }}</span>
                  <div>
                    <strong>{{ previewUser.username }}</strong
                    ><span
                      >账号身份：{{
                        { superadmin: '超级管理员', admin: '管理员', user: '普通用户' }[
                          previewUser.role
                        ] || previewUser.role
                      }}</span
                    ><span>直接角色：{{ roleNames(previewUser.id).direct || '无' }}</span
                    ><span>部门继承：{{ roleNames(previewUser.id).department || '无' }}</span>
                  </div>
                  <span class="effective-tag">已保存的生效权限</span>
                </div>
                <p v-if="dirty" class="info-strip">
                  当前草稿尚未保存，以下仍显示服务器中已生效的权限。
                </p>
                <p v-if="preview.legacy_fallback" class="info-strip">
                  当前使用旧管理员兼容权限。首次绑定角色后改按角色授权；移除最后一个角色会使用该账号身份的默认权限。
                </p>
                <div class="menu-preview">
                  <strong>该用户可见的主菜单</strong><span>创建新对话</span><span>搜索对话</span
                  ><span
                    v-for="item in previewItems.filter(
                      (item) => item.path?.startsWith('/') && item.level > 0
                    )"
                    :key="item.module"
                    >{{ item.menu }}</span
                  >
                </div>
                <div class="preview-grid">
                  <article v-for="item in previewItems" :key="item.module" class="preview-card">
                    <div class="preview-card-title">
                      <strong>{{ item.menu }}</strong
                      ><span :class="item.level ? 'granted-tag' : 'none-tag'">{{
                        levels[item.level]
                      }}</span>
                    </div>
                    <p v-if="item.sources.length">{{ item.sources.join('、') }}</p>
                    <p v-else>所有来源均未授权</p>
                    <small v-if="item.module === 'feedback' && item.level"
                      >查看范围：{{ scopeLabel(previewFeedbackScope) }}</small
                    >
                  </article>
                </div>
              </template>
              <p v-else class="empty-state">暂无可预览用户</p>
              <div class="info-strip">
                <CircleHelp :size="16" />
                <div>
                  <strong>多个角色的权限取并集</strong
                  ><span
                    >一个角色的“不可访问”表示该角色不授予权限，不会否决其他角色已经授予的权限。</span
                  >
                </div>
              </div>
            </section>

            <details v-if="dirty" class="change-summary">
              <summary>
                待保存变更 · {{ changeSummary.length }} 项 · 影响
                {{ roleUserCount(selectedRole) }} 位用户
              </summary>
              <p v-for="line in changeSummary" :key="line">{{ line }}</p>
            </details>
            <p v-if="selectedRole.is_builtin" class="info-strip">
              {{
                selectedRole.role_key === 'superadmin'
                  ? '超级管理员始终拥有全部权限，不可修改或删除。'
                  : userStore.isSuperAdmin
                    ? '可以编辑此内置角色的默认权限；保存后影响该身份下未绑定任何直接或部门角色的账号。'
                    : '仅超级管理员可以编辑此内置角色的默认权限。'
              }}
              账号身份在“用户管理”维护；额外角色和飞书部门授权请选择自定义角色。
            </p>
            <footer class="save-bar">
              <span v-if="dirty">权限草稿尚未保存，保存后会写入审计日志并立即影响关联成员。</span
              ><span v-else>权限保存后生效；成员和部门勾选会立即保存。</span>
              <div>
                <button
                  v-if="dirty"
                  type="button"
                  class="secondary-button"
                  :disabled="saving"
                  @click="discardChanges"
                >
                  放弃修改</button
                ><button
                  type="button"
                  class="primary-button"
                  :disabled="permissionsReadOnly || !dirty || saving"
                  @click="savePermissions"
                >
                  {{ saving ? '保存中…' : '保存权限' }}
                </button>
              </div>
            </footer>
          </div>
          <p v-else class="empty-state">暂无角色数据</p>
        </div>
      </template>
    </a-spin>
  </section>
</template>

<script setup>
import { computed, onMounted, onBeforeUnmount, reactive, ref, watch } from 'vue'
import { onBeforeRouteLeave } from 'vue-router'
import { message, Modal } from 'ant-design-vue'
import { Check, ChevronDown, ChevronRight, CircleHelp, Eye, Layers3, Users } from 'lucide-vue-next'
import { rolePermissionApi } from '@/apis/role_permission_api'
import { useUserStore } from '@/stores/user'
import { PERMISSION_MENUS, SYSTEM_PERMISSIONS } from '@/utils/permissionNavigation'

const loading = ref(true)
const saving = ref(false)
const error = ref('')
const roleSearch = ref('')
const memberSearch = ref('')
const departmentSearch = ref('')
const memberPage = ref(1)
const departmentPage = ref(1)
const newRoleName = ref('')
const selectedRoleKey = ref('')
const activeTab = ref('menus')
const previewUserId = ref(null)
const preview = ref(null)
const previewLoading = ref(false)
const previewError = ref('')
let previewRequest = 0
const expandedGroups = ref(new Set(['main']))
const expandedItems = ref(new Set())
const data = ref({ modules: {}, roles: [], users: [], feishu_departments: [] })
const permissions = reactive({})
const savedPermissions = ref({})
const userStore = useUserStore()
const levels = ['不可访问', '仅查看', '可管理']
const feedbackScopes = [
  { value: 'self', label: '本人', description: '仅查看自己提交的反馈' },
  { value: 'department', label: '本部门', description: '查看系统归属部门内的反馈' },
  { value: 'all', label: '全企业', description: '查看企业内所有反馈' }
]
const permissionGroups = computed(() => [{ key: 'main', name: '主菜单', items: PERMISSION_MENUS }])
const systemItems = SYSTEM_PERMISSIONS
const allItems = [
  ...PERMISSION_MENUS.flatMap((item) => [item, ...(item.children || [])]),
  ...systemItems
]
const selectedRole = computed(() =>
  data.value.roles.find((role) => role.role_key === selectedRoleKey.value)
)
const permissionsReadOnly = computed(
  () =>
    selectedRole.value?.role_key === 'superadmin' ||
    (selectedRole.value?.is_builtin && !userStore.isSuperAdmin)
)
const filteredRoles = computed(() =>
  data.value.roles.filter((role) => role.name.includes(roleSearch.value))
)
const dirty = computed(() =>
  Object.keys(permissions).some((key) => permissions[key] !== savedPermissions.value[key])
)
const previewUser = computed(() =>
  data.value.users.find((user) => user.id === Number(previewUserId.value))
)
const filteredMembers = computed(() =>
  data.value.users.filter((person) =>
    `${person.username} ${person.uid}`.toLowerCase().includes(memberSearch.value.toLowerCase())
  )
)
const filteredDepartments = computed(() =>
  data.value.feishu_departments.filter((dept) =>
    `${dept.name} ${dept.tenant_key} ${dept.feishu_department_id}`
      .toLowerCase()
      .includes(departmentSearch.value.toLowerCase())
  )
)
const pagedMembers = computed(() =>
  filteredMembers.value.slice((memberPage.value - 1) * 20, memberPage.value * 20)
)
const pagedDepartments = computed(() =>
  filteredDepartments.value.slice((departmentPage.value - 1) * 20, departmentPage.value * 20)
)
const previewItems = computed(() =>
  allItems.map((item) => ({
    ...item,
    level: levelForRole({ permissions: preview.value?.permissions || {} }, item.module),
    sources: (preview.value?.sources || [])
      .filter((source) => levelForRole(source, item.module) > 0)
      .map((source) =>
        source.kind === 'department'
          ? `飞书部门 ${source.department_name} → ${source.name}`
          : `${source.kind === 'direct' ? '直接授权' : '系统规则'} → ${source.name}`
      )
  }))
)
const previewFeedbackScope = computed(() => preview.value?.permissions['feedback.view'] || 'none')
const changeSummary = computed(() =>
  Object.keys(permissions)
    .filter((key) => permissions[key] !== savedPermissions.value[key])
    .map(
      (key) =>
        `${data.value.modules[key.split('.')[0]] || key} · ${key.endsWith('.view') ? '查看' : '管理'}：${scopeLabel(savedPermissions.value[key])} → ${scopeLabel(permissions[key])}`
    )
)

onMounted(load)
onBeforeRouteLeave(
  () => !dirty.value || confirmChange('离开角色权限？', '有尚未保存的修改，离开将放弃这些修改。')
)
function beforeUnload(event) {
  if (dirty.value) {
    event.preventDefault()
    event.returnValue = ''
  }
}
window.addEventListener('beforeunload', beforeUnload)
onBeforeUnmount(() => {
  previewRequest++
  window.removeEventListener('beforeunload', beforeUnload)
})
watch([activeTab, previewUserId], () => {
  if (activeTab.value === 'preview') loadPreview()
})
watch(memberSearch, () => {
  memberPage.value = 1
})
watch(departmentSearch, () => {
  departmentPage.value = 1
})
function levelForRole(role, module) {
  const view = role.permissions[`${module}.view`] || 'none'
  const manage = role.permissions[`${module}.manage`] || 'none'
  return manage !== 'none' ? 2 : view !== 'none' ? 1 : 0
}
function levelFor(module) {
  return levelForRole({ permissions }, module)
}
function scopeLabel(scope) {
  return feedbackScopes.find((item) => item.value === scope)?.label || '无权限'
}
function roleUserCount(role) {
  return new Set([
    ...role.user_ids,
    ...(role.inherited_user_ids || []),
    ...(role.is_builtin
      ? data.value.users
          .filter((person) => person.role === role.role_key)
          .map((person) => person.id)
      : [])
  ]).size
}
function roleDepartmentCount(role) {
  return role.departments.length
}
function roleMenuCount(role) {
  return PERMISSION_MENUS.filter((item) => levelForRole(role, item.module) > 0).length
}
function hasView(module) {
  return (permissions[`${module}.view`] || 'none') !== 'none'
}
function userHasRole(userId, roleKey) {
  return data.value.roles.some(
    (role) => role.role_key === roleKey && role.user_ids.includes(userId)
  )
}
function departmentHasRole(departmentId, roleKey) {
  return data.value.roles.some(
    (role) =>
      role.role_key === roleKey &&
      role.departments.some((department) => department.id === departmentId)
  )
}
function inherited(userId) {
  return selectedRole.value?.inherited_user_ids?.includes(userId)
}
function roleNames() {
  const sources = preview.value?.sources || []
  return {
    direct: sources
      .filter((source) => source.kind === 'direct')
      .map((source) => source.name)
      .join('、'),
    department: sources
      .filter((source) => source.kind === 'department')
      .map((source) => `${source.department_name} → ${source.name}`)
      .join('、')
  }
}
function toggleGroup(key) {
  const next = new Set(expandedGroups.value)
  next.has(key) ? next.delete(key) : next.add(key)
  expandedGroups.value = next
}
function toggleItem(key) {
  const next = new Set(expandedItems.value)
  next.has(key) ? next.delete(key) : next.add(key)
  expandedItems.value = next
}
function confirmChange(title, content) {
  return new Promise((resolve) =>
    Modal.confirm({
      title,
      content,
      okText: '确认',
      cancelText: '取消',
      onOk: () => resolve(true),
      onCancel: () => resolve(false)
    })
  )
}
async function allowDiscard() {
  return (
    !dirty.value ||
    (await confirmChange('放弃未保存修改？', '当前角色有未保存修改，继续将放弃这些修改。'))
  )
}
async function selectRole(roleKey) {
  if (saving.value || roleKey === selectedRoleKey.value || !(await allowDiscard())) return
  selectedRoleKey.value = roleKey
  hydratePermissions()
}
function hydratePermissions() {
  for (const key of Object.keys(permissions)) delete permissions[key]
  for (const module of Object.keys(data.value.modules))
    for (const action of ['view', 'manage'])
      permissions[`${module}.${action}`] =
        selectedRole.value?.permissions?.[`${module}.${action}`] || 'none'
  savedPermissions.value = { ...permissions }
}
function setLevel(item, rawLevel) {
  const level = Number(rawLevel)
  permissions[`${item.module}.view`] =
    level === 0
      ? 'none'
      : item.module === 'feedback' && level === 1 && permissions['feedback.view'] !== 'none'
        ? permissions['feedback.view']
        : 'all'
  permissions[`${item.module}.manage`] = level === 2 ? 'all' : 'none'
  if (level === 0)
    for (const child of item.children || []) {
      permissions[`${child.module}.view`] = 'none'
      permissions[`${child.module}.manage`] = 'none'
    }
}
function discardChanges() {
  hydratePermissions()
}
function dateLabel(value) {
  return value ? new Date(value).toLocaleString('zh-CN') : '未记录'
}
async function reload() {
  if (saving.value || !(await allowDiscard())) return
  await load()
}
async function load() {
  loading.value = true
  error.value = ''
  try {
    data.value = await rolePermissionApi.list()
    if (!data.value.roles.some((role) => role.role_key === selectedRoleKey.value))
      selectedRoleKey.value =
        data.value.roles.find((role) => !role.is_builtin)?.role_key ||
        data.value.roles.find((role) => role.role_key === 'superadmin')?.role_key ||
        data.value.roles[0]?.role_key ||
        ''
    if (!data.value.users.some((person) => person.id === previewUserId.value))
      previewUserId.value = data.value.users[0]?.id || null
    memberPage.value = Math.min(
      memberPage.value,
      Math.max(1, Math.ceil(filteredMembers.value.length / 20))
    )
    departmentPage.value = Math.min(
      departmentPage.value,
      Math.max(1, Math.ceil(filteredDepartments.value.length / 20))
    )
    hydratePermissions()
    if (activeTab.value === 'preview') await loadPreview()
  } catch (err) {
    error.value = `角色权限加载失败：${err.message || '请重试'}`
  } finally {
    loading.value = false
  }
}
async function loadPreview() {
  const request = ++previewRequest
  preview.value = null
  previewError.value = ''
  if (!previewUserId.value) {
    previewLoading.value = false
    return
  }
  previewLoading.value = true
  try {
    const result = await rolePermissionApi.preview(previewUserId.value)
    if (request === previewRequest) preview.value = result
  } catch (err) {
    if (request === previewRequest) previewError.value = `生效预览加载失败：${err.message}`
  } finally {
    if (request === previewRequest) previewLoading.value = false
  }
}
async function refreshOwnPermissions() {
  try {
    await userStore.refreshPermissions()
  } catch {
    message.warning('操作已保存，但当前账号权限刷新失败，请刷新页面')
  }
}
async function savePermissions() {
  if (!selectedRole.value || permissionsReadOnly.value || saving.value) return
  if (
    !(await confirmChange(
      '保存角色权限',
      `保存“${selectedRole.value.name}”？影响 ${roleUserCount(selectedRole.value)} 位关联用户。\n${changeSummary.value.join('\n')}`
    ))
  )
    return
  saving.value = true
  try {
    await rolePermissionApi.setRolePermissions(selectedRole.value.role_key, {
      permissions: { ...permissions },
      expected_permissions: { ...savedPermissions.value }
    })
    savedPermissions.value = { ...permissions }
    message.success('角色权限已保存')
    await refreshOwnPermissions()
    await load()
  } catch (err) {
    message.error(`权限保存失败：${err.message}`)
  } finally {
    saving.value = false
  }
}
async function createRole() {
  if (saving.value || !(await allowDiscard())) return
  saving.value = true
  try {
    const role = await rolePermissionApi.create({ name: newRoleName.value })
    newRoleName.value = ''
    selectedRoleKey.value = role.role_key
    await load()
    message.success('角色已创建')
  } catch (err) {
    message.error(`角色创建失败：${err.message}`)
  } finally {
    saving.value = false
  }
}
async function removeRole() {
  if (
    saving.value ||
    !selectedRole.value ||
    !(await allowDiscard()) ||
    !(await confirmChange(
      '删除角色',
      `删除“${selectedRole.value.name}”？将移除 ${roleUserCount(selectedRole.value)} 位关联用户及 ${roleDepartmentCount(selectedRole.value)} 个部门的此角色授权。移除最后一个角色后会使用账号身份的默认权限。`
    ))
  )
    return
  saving.value = true
  try {
    await rolePermissionApi.remove(selectedRole.value.role_key)
    selectedRoleKey.value = ''
    message.success('角色已删除')
    await refreshOwnPermissions()
    await load()
  } catch (err) {
    message.error(`角色删除失败：${err.message}`)
  } finally {
    saving.value = false
  }
}
async function assignUser(userId, event) {
  const checked = event.target.checked
  event.target.checked = userHasRole(userId, selectedRoleKey.value)
  if (dirty.value || saving.value) return
  const target = data.value.users.find((person) => person.id === userId)
  if (
    target.role === 'admin' &&
    !(await confirmChange(
      '更新用户角色',
      '此用户为管理员：有角色时按角色授权，没有任何直接或部门角色时使用管理员默认权限。是否更新？'
    ))
  )
    return
  saving.value = true
  const before = data.value.roles
    .filter((role) => role.user_ids.includes(userId))
    .map((role) => role.role_key)
  const after = before.filter((key) => key !== selectedRoleKey.value)
  if (checked) after.push(selectedRoleKey.value)
  try {
    await rolePermissionApi.setUserRoles(userId, after, before)
    message.success('用户角色已更新')
    await refreshOwnPermissions()
    await load()
  } catch (err) {
    message.error(`用户角色更新失败：${err.message}`)
  } finally {
    saving.value = false
  }
}
async function assignDepartment(departmentId, event) {
  const checked = event.target.checked
  event.target.checked = departmentHasRole(departmentId, selectedRoleKey.value)
  if (dirty.value || saving.value) return
  if (
    !(await confirmChange(
      '更新部门角色',
      `${checked ? '绑定' : '移除'}当前部门的“${selectedRole.value.name}”角色？仅影响已同步的直属成员，不递归包含子部门。移除最后一个角色后会使用账号身份的默认权限。`
    ))
  )
    return
  saving.value = true
  const before = data.value.roles
    .filter((role) => role.departments.some((dept) => dept.id === departmentId))
    .map((role) => role.role_key)
  const after = before.filter((key) => key !== selectedRoleKey.value)
  if (checked) after.push(selectedRoleKey.value)
  try {
    await rolePermissionApi.setDepartmentRoles(departmentId, after, before)
    message.success('飞书部门角色已更新')
    await refreshOwnPermissions()
    await load()
  } catch (err) {
    message.error(`飞书部门角色更新失败：${err.message}`)
  } finally {
    saving.value = false
  }
}
</script>

<style scoped lang="less">
.role-permissions {
  min-width: 0;
  width: 100%;
  height: 100%;
  color: var(--color-text);
}
.permissions-header,
.role-detail-heading,
.view-heading,
.save-bar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
}
.permissions-header {
  margin-bottom: 18px;
  h2 {
    margin: 6px 0 4px;
    font-size: 22px;
    font-weight: 600;
  }
  p {
    margin: 0;
    color: var(--color-text-secondary);
    font-size: 13px;
  }
}
.section-kicker {
  color: var(--main-700);
  font-size: 11px;
  font-weight: 600;
  letter-spacing: 0.08em;
}
.header-actions,
.create-role,
.detail-actions {
  display: flex;
  align-items: center;
  gap: 8px;
}
.create-role input,
.role-search,
select {
  min-height: 34px;
  border: 1px solid var(--gray-200);
  border-radius: 6px;
  padding: 6px 9px;
  background: var(--gray-0);
  color: inherit;
}
.create-role input {
  width: 150px;
}
button {
  min-height: 34px;
  border: 1px solid var(--gray-200);
  border-radius: 6px;
  padding: 0 11px;
  background: var(--gray-0);
  color: inherit;
  cursor: pointer;
}
button:focus-visible,
input:focus-visible,
select:focus-visible {
  outline: 2px solid var(--main-500);
  outline-offset: 2px;
}
button:disabled,
select:disabled,
input:disabled {
  opacity: 0.55;
  cursor: not-allowed;
}
.primary-button {
  border-color: var(--main-700);
  background: var(--main-700);
  color: var(--gray-0);
}
.secondary-button {
  color: var(--main-700);
}
.danger-button {
  border-color: var(--color-error-100);
  color: var(--color-error-700);
}
.permissions-workspace {
  display: grid;
  grid-template-columns: 220px minmax(0, 1fr);
  min-height: 650px;
  border: 1px solid var(--gray-200);
  border-radius: 8px;
  overflow: hidden;
  background: var(--gray-0);
}
.role-list-panel {
  padding: 16px 12px;
  background: var(--gray-25);
  border-right: 1px solid var(--gray-200);
}
.role-list-heading {
  display: flex;
  align-items: end;
  justify-content: space-between;
  padding: 0 5px;
}
.role-list-heading strong {
  display: block;
  margin-top: 5px;
  font-size: 15px;
}
.role-count,
.role-card-meta {
  color: var(--color-text-secondary);
  font-size: 11px;
}
.role-search {
  width: 100%;
  margin: 14px 0 10px;
}
.role-list {
  display: grid;
  gap: 6px;
}
.role-card {
  width: 100%;
  padding: 11px;
  border-color: transparent;
  text-align: left;
}
.role-card:hover {
  background: var(--gray-100);
}
.role-card.active {
  border-color: var(--main-400);
  background: var(--main-30);
}
.role-card-title {
  display: flex;
  align-items: center;
  gap: 6px;
}
.role-card-title strong {
  font-size: 13px;
  font-weight: 600;
}
.builtin-tag,
.status-tag {
  padding: 2px 5px;
  border-radius: 4px;
  background: var(--gray-100);
  color: var(--color-text-secondary);
  font-size: 10px;
}
.role-card-description {
  display: block;
  margin: 5px 0 8px;
  color: var(--color-text-secondary);
  font-size: 11px;
}
.role-list-help {
  display: flex;
  gap: 7px;
  margin-top: 18px;
  padding: 12px 5px 0;
  border-top: 1px solid var(--gray-200);
  color: var(--color-text-secondary);
  font-size: 11px;
  line-height: 1.5;
}
.role-list-help p {
  margin: 0;
}
.help-icon {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  flex: 0 0 auto;
  width: 16px;
  height: 16px;
  border-radius: 50%;
  background: var(--main-30);
  color: var(--main-700);
}
.role-detail-panel {
  min-width: 0;
  padding: 24px;
}
.role-detail-heading {
  align-items: flex-start;
}
.role-detail-heading h3 {
  margin: 6px 0 4px;
  font-size: 20px;
  font-weight: 600;
}
.role-detail-heading p {
  margin: 0;
  color: var(--color-text-secondary);
  font-size: 12px;
}
.member-summary {
  color: var(--color-text-secondary);
  font-size: 11px;
  white-space: nowrap;
}
.permission-tabs {
  display: flex;
  gap: 2px;
  margin: 22px 0 18px;
  border-bottom: 1px solid var(--gray-200);
  overflow-x: auto;
}
.permission-tabs button {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  border: 0;
  border-radius: 6px 6px 0 0;
  color: var(--color-text-secondary);
  white-space: nowrap;
}
.permission-tabs button.active {
  background: var(--main-700);
  color: var(--gray-0);
}
.permission-view {
  min-height: 450px;
}
.view-heading {
  align-items: flex-end;
  margin-bottom: 14px;
}
.view-heading h4 {
  margin: 0 0 4px;
  font-size: 16px;
  font-weight: 600;
}
.view-heading p {
  margin: 0;
  color: var(--color-text-secondary);
  font-size: 12px;
}
.permission-legend {
  color: var(--color-text-secondary);
  font-size: 11px;
}
.permission-group,
.scope-card,
.system-permissions {
  border: 1px solid var(--gray-200);
  border-radius: 7px;
  overflow: hidden;
}
.permission-group {
  margin-bottom: 10px;
}
.group-heading {
  display: flex;
  align-items: center;
  justify-content: space-between;
  width: 100%;
  border: 0;
  border-radius: 0;
  color: var(--main-800);
  background: var(--gray-25);
  font-size: 12px;
}
.group-heading span {
  display: inline-flex;
  align-items: center;
  gap: 6px;
}
.group-heading small {
  color: var(--color-text-secondary);
}
.permission-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  min-height: 50px;
  padding: 8px 12px;
  border-top: 1px solid var(--gray-150);
}
.permission-label {
  display: flex;
  align-items: center;
  gap: 7px;
  min-width: 0;
}
.permission-label strong,
.assignment-person strong {
  display: block;
  font-size: 13px;
  font-weight: 500;
}
.permission-label small,
.assignment-person small {
  display: block;
  margin-top: 2px;
  color: var(--color-text-secondary);
  font-size: 11px;
}
.permission-row select {
  width: 118px;
  flex: 0 0 auto;
}
.expand-button {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 22px;
  min-height: 25px;
  border: 0;
  padding: 0;
  background: transparent;
}
.tree-spacer {
  display: inline-block;
  width: 22px;
  flex: 0 0 auto;
}
.child-permissions {
  margin-left: 25px;
  border-left: 2px solid var(--gray-200);
  background: var(--gray-25);
}
.child-hint,
.fixed-entry-note {
  margin: 8px 12px 12px;
  color: var(--color-text-secondary);
  font-size: 11px;
}
.system-permissions {
  margin-top: 10px;
}
.system-permissions summary {
  padding: 11px 12px;
  cursor: pointer;
  font-size: 12px;
}
.scope-card {
  padding: 16px;
}
.scope-card h5 {
  margin: 0;
  font-size: 14px;
  font-weight: 600;
}
.scope-disabled {
  color: var(--color-text-secondary);
  font-size: 12px;
}
.scope-option {
  display: flex;
  align-items: center;
  gap: 10px;
  padding-top: 15px;
  cursor: pointer;
}
.scope-option strong,
.scope-option small {
  display: block;
}
.scope-option strong {
  font-size: 13px;
}
.scope-option small {
  margin-top: 2px;
  color: var(--color-text-secondary);
  font-size: 11px;
}
.info-strip {
  display: flex;
  gap: 8px;
  align-items: flex-start;
  margin-top: 14px;
  padding: 11px 12px;
  border-left: 3px solid var(--main-500);
  background: var(--main-30);
  color: var(--color-text-secondary);
  font-size: 11px;
}
.info-strip svg {
  flex: 0 0 auto;
  color: var(--main-700);
}
.info-strip strong,
.info-strip span {
  display: block;
}
.info-strip strong {
  color: var(--main-800);
}
.info-strip span {
  margin-top: 3px;
}
.subheading {
  margin: 18px 0 8px;
  font-size: 13px;
  font-weight: 600;
}
.assignment-list {
  border: 1px solid var(--gray-200);
  border-radius: 7px;
  overflow: hidden;
}
.assignment-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  min-height: 55px;
  padding: 8px 12px;
  border-bottom: 1px solid var(--gray-150);
}
.assignment-row:last-child {
  border-bottom: 0;
}
.assignment-person {
  display: inline-flex;
  align-items: center;
  gap: 9px;
}
.source-tag,
.unassigned-tag {
  padding: 3px 6px;
  border-radius: 4px;
  color: var(--main-700);
  background: var(--main-30);
  font-size: 10px;
  white-space: nowrap;
}
.source-tag.feishu {
  color: var(--color-warning-900);
  background: var(--color-warning-50);
}
.unassigned-tag {
  color: var(--color-text-secondary);
  background: var(--gray-100);
}
.preview-user-select {
  display: flex;
  align-items: center;
  gap: 8px;
  color: var(--color-text-secondary);
  font-size: 11px;
}
.preview-user-select select {
  min-width: 170px;
}
.preview-user {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 14px;
  border: 1px solid var(--gray-200);
  border-radius: 7px;
  background: var(--gray-25);
}
.avatar {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  flex: 0 0 auto;
  width: 34px;
  height: 34px;
  border-radius: 50%;
  color: var(--gray-0);
  background: var(--main-600);
  font-size: 14px;
}
.preview-user strong,
.preview-user span {
  display: block;
}
.preview-user strong {
  font-size: 13px;
}
.preview-user div span {
  margin-top: 2px;
  color: var(--color-text-secondary);
  font-size: 11px;
}
.effective-tag {
  margin-left: auto;
  padding: 4px 6px;
  border-radius: 4px;
  color: var(--color-success-700);
  background: var(--color-success-50);
  font-size: 10px;
  white-space: nowrap;
}
.preview-grid {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 8px;
  margin-top: 12px;
}
.preview-card {
  min-width: 0;
  padding: 12px;
  border: 1px solid var(--gray-200);
  border-radius: 7px;
}
.preview-card-title {
  display: flex;
  justify-content: space-between;
  gap: 7px;
}
.preview-card-title strong {
  font-size: 12px;
}
.preview-card p,
.preview-card small {
  display: block;
  margin: 7px 0 0;
  color: var(--color-text-secondary);
  font-size: 11px;
  line-height: 1.45;
}
.granted-tag,
.none-tag {
  padding: 2px 5px;
  border-radius: 4px;
  color: var(--color-success-700);
  background: var(--color-success-50);
  font-size: 10px;
  white-space: nowrap;
}
.none-tag {
  color: var(--color-text-secondary);
  background: var(--gray-100);
}
.save-bar {
  margin-top: 18px;
  padding-top: 13px;
  border-top: 1px solid var(--gray-200);
  color: var(--color-text-secondary);
  font-size: 11px;
}
.save-bar > div {
  display: flex;
  gap: 7px;
}
.empty-state {
  margin: 12px 0;
  color: var(--color-text-secondary);
  font-size: 12px;
}
@media (max-width: 850px) {
  .permissions-header,
  .role-detail-heading,
  .view-heading,
  .save-bar {
    align-items: flex-start;
    flex-direction: column;
  }
  .header-actions,
  .create-role {
    width: 100%;
  }
  .create-role input {
    flex: 1;
  }
  .permissions-workspace {
    grid-template-columns: 1fr;
  }
  .role-list-panel {
    border-right: 0;
    border-bottom: 1px solid var(--gray-200);
  }
  .role-list {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
  .detail-actions {
    width: 100%;
    justify-content: space-between;
  }
  .preview-grid {
    grid-template-columns: 1fr 1fr;
  }
}
@media (max-width: 560px) {
  .role-detail-panel {
    padding: 15px;
  }
  .role-list {
    grid-template-columns: 1fr;
  }
  .preview-grid {
    grid-template-columns: 1fr;
  }
  .assignment-row {
    align-items: flex-start;
    flex-direction: column;
  }
  .save-bar > div {
    width: 100%;
  }
  .save-bar button {
    flex: 1;
  }
}
.role-permissions {
  padding: 24px;
  overflow-y: auto;
}
.role-list {
  max-height: 60vh;
  overflow-y: auto;
}
.pagination {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin: 12px 0;
  font-size: 12px;
}
.menu-preview {
  display: flex;
  flex-wrap: wrap;
  gap: 12px;
  margin-top: 14px;
  padding: 14px;
  background: var(--gray-25);
  border-radius: 6px;
  font-size: 13px;
}
.menu-preview strong {
  width: 100%;
}
.change-summary {
  margin-top: 14px;
  padding: 12px;
  border: 1px solid var(--gray-200);
  border-radius: 6px;
  font-size: 12px;
}
.change-summary summary {
  cursor: pointer;
}
.change-summary p {
  margin-top: 5px;
}
:focus-visible {
  outline: 2px solid var(--main-500);
  outline-offset: 2px;
}
input[type='checkbox'],
input[type='radio'] {
  width: 16px;
  height: 16px;
  accent-color: var(--main-700);
}
.assignment-person {
  min-width: 0;
  overflow-wrap: anywhere;
}
.preview-user .avatar {
  display: inline-flex;
}
</style>
