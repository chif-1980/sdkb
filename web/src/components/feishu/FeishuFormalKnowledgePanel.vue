<template>
  <section class="governance-card">
    <div class="section-heading">
      <div>
        <h2>正式知识单元</h2>
        <p>按原始材料分组展示，标题和状态优先；完整来源信息可在详情中查看。</p>
      </div>
      <a-button size="small" :loading="loading" @click="loadKnowledge"
        ><RefreshCw :size="15" />刷新</a-button
      >
    </div>
    <div class="toolbar">
      <a-select v-model:value="knowledgeType" size="small" :options="typeOptions" />
      <a-input
        v-model:value="searchText"
        size="small"
        placeholder="搜索知识标题、产品或版本"
        allow-clear
      />
      <span class="toolbar-summary"
        >共 <strong>{{ filteredKnowledge.length }}</strong> 条知识单元</span
      >
    </div>
    <div v-if="groupedKnowledge.length" class="knowledge-groups">
      <article v-for="group in groupedKnowledge" :key="group.key" class="source-group">
        <header class="source-group-header">
          <div class="source-group-title">
            <span class="source-eyebrow">来源材料</span>
            <div class="source-title-row">
              <strong :title="group.title">{{ group.title }}</strong>
              <a-tag v-if="group.pendingUpdate" class="source-update-tag" color="orange">
                有更新
              </a-tag>
              <button
                v-if="group.pendingUpdate"
                type="button"
                class="source-update-action"
                @click.stop="openUpdateReview(group)"
              >
                查看变更 <ChevronRight :size="13" />
              </button>
            </div>
            <div class="source-detail-row">
              <span v-if="group.path" class="source-path" :title="group.path">{{ group.path }}</span>
              <span v-if="group.pendingUpdate" class="source-update-meta">
                版本 {{ group.currentRevision }} → {{ group.pendingUpdate.revision }} · 检测于
                {{ formatDetectedAt(group.pendingUpdate.detected_at) }}
              </span>
            </div>
          </div>
          <div class="source-group-summary">
            <a-tag :color="sourceStatusColor(group.sourcePublicationStatus)">
              {{ sourceStatusLabel(group.sourcePublicationStatus) }}
            </a-tag>
            <span
              ><strong>{{ group.items.length }}</strong> 个知识单元</span
            >
            <span v-if="group.fragmentCount"
              ><strong>{{ group.fragmentCount }}</strong> 个来源片段</span
            >
            <a-button
              v-if="group.sourcePublicationStatus === 'ACTIVE'"
              size="small"
              danger
              @click.stop="startAction('source-offline', group)"
            >
              <Archive :size="13" />整篇下架
            </a-button>
            <a-button
              v-else-if="group.sourcePublicationStatus === 'OFFLINE'"
              size="small"
              @click.stop="startAction('source-restore', group)"
            >
              <ArchiveRestore :size="13" />整篇恢复
            </a-button>
            <a
              v-if="group.sourceUrl"
              class="source-link"
              :href="group.sourceUrl"
              target="_blank"
              rel="noopener noreferrer"
              @click.stop
            >
              查看原始材料 <ExternalLink :size="12" />
            </a>
            <button
              type="button"
              class="group-toggle"
              :aria-expanded="isGroupExpanded(group.key)"
              @click="toggleGroup(group.key)"
            >
              {{ isGroupExpanded(group.key) ? '收起' : '展开' }}
              <ChevronDown :size="14" :class="{ rotated: !isGroupExpanded(group.key) }" />
            </button>
          </div>
        </header>

        <div v-if="isGroupExpanded(group.key)" class="knowledge-list">
          <div class="knowledge-list-head" aria-hidden="true">
            <span>知识单元</span>
            <span>类型与索引</span>
            <span>操作</span>
          </div>
          <article
            v-for="record in group.items"
            :key="record.knowledge_id"
            class="knowledge-row"
            role="button"
            tabindex="0"
            @click="openVersions(record)"
            @keydown.enter="openVersions(record)"
          >
            <div class="knowledge-main">
              <div class="knowledge-kicker">
                <span
                  class="knowledge-level"
                  :class="{ legacy: record.knowledge_level !== 'UNIT' }"
                >
                  {{ record.knowledge_level === 'UNIT' ? '知识单元' : '整份资料' }}
                </span>
                <span
                  v-if="sourceLocatorLabel(record) !== sourceCountLabel(record)"
                  class="locator-label"
                >
                  {{ sourceLocatorLabel(record) }}
                </span>
              </div>
              <strong class="primary-cell" :title="record.title">{{ record.title }}</strong>
              <span class="sub-line"
                >当前版本 {{ record.revision }} · {{ sourceCountLabel(record) }}</span
              >
            </div>
            <div class="knowledge-row-status">
              <a-tag color="processing">{{
                record.source_role === 'PRIMARY' ? '通用知识' : '条件变体'
              }}</a-tag>
              <a-tag :color="lifecycleStatusColor(record.lifecycle_status)">
                {{ lifecycleStatusLabel(record.lifecycle_status) }}
              </a-tag>
              <a-tag :color="record.index_status === 'INDEXED' ? 'success' : 'default'">
                {{ record.index_status === 'INDEXED' ? '已索引' : '未参与检索' }}
              </a-tag>
            </div>
            <span class="knowledge-action">查看详情 <ChevronRight :size="14" /></span>
          </article>
        </div>
      </article>
    </div>
    <div v-else class="knowledge-empty"><a-empty description="暂无已发布知识单元" /></div>
    <div class="table-footer">共 {{ filteredKnowledge.length }} 条正式知识单元</div>

    <a-modal v-model:open="versionModalOpen" title="知识单元详情" :footer="null" width="860px">
      <a-spin :spinning="loadingVersions">
        <div v-if="selectedKnowledge" class="source-trace" aria-label="正式知识来源层级">
          <div class="trace-step">
            <span>原始材料</span>
            <strong>{{ selectedKnowledge.source_title || selectedKnowledge.title }}</strong>
            <small>{{ selectedKnowledge.wiki_path || '未记录目录路径' }}</small>
          </div>
          <ChevronRight :size="15" />
          <div class="trace-step active">
            <span>{{
              selectedKnowledge.knowledge_level === 'UNIT' ? '知识单元' : '整份资料知识'
            }}</span>
            <strong>{{ selectedKnowledge.title }}</strong>
            <small>{{ sourceLocatorLabel(selectedKnowledge) }}</small>
          </div>
          <ChevronRight :size="15" />
          <div class="trace-step">
            <span>来源片段</span>
            <strong>{{ sourceCountLabel(selectedKnowledge) }}</strong>
            <small>当前有效版本 {{ selectedKnowledge.revision }}</small>
          </div>
          <a
            v-if="selectedKnowledge.source_url"
            class="source-link"
            :href="selectedKnowledge.source_url"
            target="_blank"
            rel="noopener noreferrer"
          >
            查看原始材料 <ExternalLink :size="13" />
          </a>
        </div>
        <div v-if="selectedKnowledge" class="detail-facts">
          <div>
            <span>知识 ID</span
            ><code :title="selectedKnowledge.knowledge_id">{{
              selectedKnowledge.knowledge_id
            }}</code>
          </div>
          <div>
            <span>知识类型</span
            ><strong>{{
              selectedKnowledge.source_role === 'PRIMARY' ? '通用知识' : '条件变体'
            }}</strong>
          </div>
          <div>
            <span>来源片段</span><strong>{{ sourceCountLabel(selectedKnowledge) }}</strong>
          </div>
          <div>
            <span>当前状态</span
            ><a-tag :color="lifecycleStatusColor(selectedKnowledge.lifecycle_status)">{{
              lifecycleStatusLabel(selectedKnowledge.lifecycle_status)
            }}</a-tag>
          </div>
        </div>
        <div v-if="selectedKnowledge?.unit_id" class="lifecycle-actions">
          <div>
            <strong>知识单元治理</strong>
            <span>正文仍以飞书原文为准；修订将生成待处理任务。</span>
          </div>
          <a-button
            v-if="selectedKnowledge.stored_lifecycle_status !== 'OFFLINE'"
            size="small"
            danger
            @click="startAction('unit-offline', selectedKnowledge)"
          >
            <Archive :size="14" />下架单元
          </a-button>
          <a-button v-else size="small" @click="startAction('unit-restore', selectedKnowledge)">
            <ArchiveRestore :size="14" />恢复单元
          </a-button>
          <a-button size="small" @click="startAction('unit-revision', selectedKnowledge)">
            <PencilLine :size="14" />发起修订
          </a-button>
        </div>
        <div v-if="selectedKnowledge?.unit_id" class="metadata-editor">
          <div class="metadata-heading">
            <div><strong>治理信息</strong><span>负责人、有效期和复核日期</span></div>
            <a-button size="small" :loading="savingMetadata" @click="saveMetadata">
              <Save :size="14" />保存
            </a-button>
          </div>
          <div class="metadata-fields">
            <label
              ><span>负责人姓名</span
              ><a-input v-model:value="metadataForm.owner_name" placeholder="未指定"
            /></label>
            <label
              ><span>负责人 ID</span
              ><a-input v-model:value="metadataForm.owner_id" placeholder="可选"
            /></label>
            <label
              ><span>生效日期</span><a-input v-model:value="metadataForm.valid_from" type="date"
            /></label>
            <label
              ><span>失效日期</span><a-input v-model:value="metadataForm.valid_until" type="date"
            /></label>
            <label
              ><span>复核日期</span><a-input v-model:value="metadataForm.review_due_at" type="date"
            /></label>
          </div>
        </div>
        <div class="version-heading">
          <strong>历史版本</strong><span>仅已审核且有归档原件的版本可回滚</span>
        </div>
        <a-table
          :columns="versionColumns"
          :data-source="versions"
          row-key="version_id"
          size="small"
          :pagination="false"
        >
          <template #bodyCell="{ column, record }">
            <template v-if="column.key === 'status'"
              ><a-tag :color="record.active ? 'success' : 'default'">{{
                record.active ? '当前有效' : record.processing_status
              }}</a-tag></template
            >
            <template v-else-if="column.key === 'review'"
              ><span>{{
                record.review_status === 'approved' ? '已审核' : record.review_status
              }}</span></template
            >
            <template v-else-if="column.key === 'operation'">
              <a-button
                v-if="record.rollback_available"
                type="link"
                size="small"
                @click="startAction('source-rollback', record)"
              >
                <RotateCcw :size="13" />回滚到此版本
              </a-button>
              <span v-else class="operation-muted">—</span>
            </template>
          </template>
        </a-table>
      </a-spin>
    </a-modal>

    <a-modal
      v-model:open="actionModalOpen"
      :title="actionTitle"
      :ok-text="actionConfirmText"
      cancel-text="取消"
      :confirm-loading="submittingAction"
      @ok="confirmAction"
    >
      <p class="action-description">{{ actionDescription }}</p>
      <label class="reason-field">
        <span>处理原因</span>
        <a-textarea
          v-model:value="actionReason"
          :rows="4"
          :maxlength="4000"
          placeholder="请填写原因，便于后续追溯"
        />
      </label>
    </a-modal>
  </section>
</template>

<script setup>
import { computed, ref, watch } from 'vue'
import { message } from 'ant-design-vue'
import {
  Archive,
  ArchiveRestore,
  ChevronDown,
  ChevronRight,
  ExternalLink,
  PencilLine,
  RefreshCw,
  RotateCcw,
  Save
} from 'lucide-vue-next'
import { governanceApi } from '@/apis/governance_api'

const props = defineProps({ sourceId: { type: String, default: '' } })
const emit = defineEmits(['count-change', 'open-update-review'])
const knowledge = ref([])
const versions = ref([])
const loading = ref(false)
const loadingVersions = ref(false)
const versionModalOpen = ref(false)
const selectedKnowledge = ref(null)
const actionModalOpen = ref(false)
const actionType = ref('')
const actionTarget = ref(null)
const actionReason = ref('')
const submittingAction = ref(false)
const savingMetadata = ref(false)
const metadataForm = ref(emptyMetadataForm())
const knowledgeType = ref('')
const searchText = ref('')
const collapsedGroups = ref(new Set())
const typeOptions = [
  { label: '全部类型', value: '' },
  { label: '通用知识', value: 'PRIMARY' },
  { label: '条件变体', value: 'VARIANT' }
]
const versionColumns = [
  { title: '版本', dataIndex: 'revision', key: 'revision', width: 100 },
  { title: '状态', key: 'status', width: 120 },
  { title: '审核', key: 'review', width: 120 },
  { title: '发布时间', dataIndex: 'published_at', key: 'published_at' },
  { title: '操作', key: 'operation', width: 130 }
]
const actionCopy = {
  'unit-offline': {
    title: '下架知识单元',
    confirm: '确认下架',
    description: '下架后该知识单元不再参与检索，来源材料中的其他知识单元不受影响。',
    success: '知识单元下架任务已提交'
  },
  'unit-restore': {
    title: '恢复知识单元',
    confirm: '确认恢复',
    description: '恢复后将重新构建索引；索引完成后该知识单元才会重新参与检索。',
    success: '知识单元恢复任务已提交'
  },
  'unit-revision': {
    title: '发起知识修订',
    confirm: '发起修订',
    description: '系统将生成飞书源文档修改任务，正文仍在原始飞书文档中修订。',
    success: '知识修订任务已创建'
  },
  'source-offline': {
    title: '整篇下架',
    confirm: '确认整篇下架',
    description: '该来源材料下的全部知识单元都将停止参与检索。',
    success: '来源材料已下架'
  },
  'source-restore': {
    title: '整篇恢复',
    confirm: '确认整篇恢复',
    description: '系统将从归档原件重建索引，完成后整篇知识恢复检索。',
    success: '来源材料恢复任务已提交'
  },
  'source-rollback': {
    title: '回滚历史版本',
    confirm: '确认回滚',
    description: '系统将以所选历史版本重建索引，成功后该版本成为当前有效版本。',
    success: '历史版本回滚任务已提交'
  }
}
const currentActionCopy = computed(() => actionCopy[actionType.value] || {})
const actionTitle = computed(() => currentActionCopy.value.title || '知识治理操作')
const actionConfirmText = computed(() => currentActionCopy.value.confirm || '确认')
const actionDescription = computed(() => currentActionCopy.value.description || '')
const filteredKnowledge = computed(() =>
  knowledge.value.filter((item) => {
    const matchType =
      !knowledgeType.value ||
      (knowledgeType.value === 'PRIMARY'
        ? item.source_role === 'PRIMARY'
        : item.source_role !== 'PRIMARY')
    const keyword = searchText.value.trim().toLowerCase()
    return (
      matchType &&
      (!keyword ||
        `${item.title} ${item.knowledge_id} ${item.source_title || ''} ${item.wiki_path || ''} ${scopeSummary(item.applicability_scope)}`
          .toLowerCase()
          .includes(keyword))
    )
  })
)
const groupedKnowledge = computed(() => {
  const groups = new Map()
  filteredKnowledge.value.forEach((record) => {
    const key = record.source_item_id || record.source_title || record.knowledge_id
    if (!groups.has(key)) {
      groups.set(key, {
        key,
        title: record.source_title || record.title || '未命名原始材料',
        path: record.wiki_path || '',
        sourceUrl: record.source_url || '',
        sourceItemId: record.source_item_id || '',
        sourcePublicationStatus: record.source_publication_status || 'ACTIVE',
        currentRevision: record.revision || '',
        pendingUpdate: record.pending_update || null,
        items: [],
        fragmentCount: 0
      })
    }
    const group = groups.get(key)
    if (!group.pendingUpdate && record.pending_update) group.pendingUpdate = record.pending_update
    group.items.push(record)
    group.fragmentCount += record.source_segment_count || record.chunk_count || 0
  })
  return Array.from(groups.values())
})

watch(() => [props.sourceId, knowledgeType.value], loadKnowledge, { immediate: true })

async function loadKnowledge() {
  if (!props.sourceId) return
  loading.value = true
  try {
    const response = await governanceApi.listFormalKnowledge(props.sourceId)
    knowledge.value = response.items || []
    collapsedGroups.value = new Set(groupedKnowledge.value.map((group) => group.key))
    emit('count-change', knowledge.value.length)
  } catch (error) {
    message.error(governanceApi.getErrorMessage(error, '加载正式知识失败'))
  } finally {
    loading.value = false
  }
}

function isGroupExpanded(key) {
  return !collapsedGroups.value.has(key)
}

function toggleGroup(key) {
  const next = new Set(collapsedGroups.value)
  if (next.has(key)) next.delete(key)
  else next.add(key)
  collapsedGroups.value = next
}

function openUpdateReview(group) {
  emit('open-update-review', {
    packageId: group.pendingUpdate?.review_package_id || '',
    sourceVersionId: group.pendingUpdate?.version_id || ''
  })
}

async function openVersions(record) {
  selectedKnowledge.value = record
  metadataForm.value = metadataFormFromRecord(record)
  versionModalOpen.value = true
  loadingVersions.value = true
  try {
    const response = await governanceApi.listKnowledgeVersions(
      record.source_item_id || record.knowledge_id
    )
    versions.value = response.items || []
  } catch (error) {
    message.error(governanceApi.getErrorMessage(error, '加载知识版本失败'))
  } finally {
    loadingVersions.value = false
  }
}

function startAction(type, target) {
  actionType.value = type
  actionTarget.value = target
  actionReason.value = ''
  actionModalOpen.value = true
}

async function confirmAction() {
  const reason = actionReason.value.trim()
  if (!reason) {
    message.warning('请填写处理原因')
    return
  }
  const target = actionTarget.value
  submittingAction.value = true
  try {
    if (actionType.value === 'unit-offline') {
      await governanceApi.offlineKnowledgeUnit(target.unit_id, reason)
    } else if (actionType.value === 'unit-restore') {
      await governanceApi.restoreKnowledgeUnit(target.unit_id, reason)
    } else if (actionType.value === 'unit-revision') {
      await governanceApi.createKnowledgeRevision(target.unit_id, reason)
    } else if (actionType.value === 'source-offline') {
      await governanceApi.offlineKnowledgeSource(target.sourceItemId, reason)
    } else if (actionType.value === 'source-restore') {
      await governanceApi.restoreKnowledgeSource(target.sourceItemId, reason)
    } else if (actionType.value === 'source-rollback') {
      await governanceApi.rollbackKnowledgeSource(
        selectedKnowledge.value.source_item_id,
        target.version_id,
        reason
      )
    } else {
      throw new Error('Unsupported governance action')
    }
    message.success(currentActionCopy.value.success)
    actionModalOpen.value = false
    if (actionType.value !== 'unit-revision') versionModalOpen.value = false
    await loadKnowledge()
  } catch (error) {
    message.error(governanceApi.getErrorMessage(error, '知识治理操作失败'))
  } finally {
    submittingAction.value = false
  }
}

async function saveMetadata() {
  if (!selectedKnowledge.value?.unit_id) return
  const payload = {
    owner_name: normalizedOptionalText(metadataForm.value.owner_name),
    owner_id: normalizedOptionalText(metadataForm.value.owner_id),
    valid_from: datePayload(metadataForm.value.valid_from),
    valid_until: datePayload(metadataForm.value.valid_until),
    review_due_at: datePayload(metadataForm.value.review_due_at)
  }
  if (payload.valid_from && payload.valid_until && payload.valid_from > payload.valid_until) {
    message.warning('失效日期不能早于生效日期')
    return
  }
  savingMetadata.value = true
  try {
    await governanceApi.updateKnowledgeUnitMetadata(selectedKnowledge.value.unit_id, payload)
    Object.assign(selectedKnowledge.value, payload)
    const record = knowledge.value.find((item) => item.unit_id === selectedKnowledge.value.unit_id)
    if (record) Object.assign(record, payload)
    message.success('治理信息已保存')
  } catch (error) {
    message.error(governanceApi.getErrorMessage(error, '治理信息保存失败'))
  } finally {
    savingMetadata.value = false
  }
}

function emptyMetadataForm() {
  return { owner_name: '', owner_id: '', valid_from: '', valid_until: '', review_due_at: '' }
}

function metadataFormFromRecord(record) {
  return {
    owner_name: record.owner_name || '',
    owner_id: record.owner_id || '',
    valid_from: dateInputValue(record.valid_from),
    valid_until: dateInputValue(record.valid_until),
    review_due_at: dateInputValue(record.review_due_at)
  }
}

function dateInputValue(value) {
  return value ? String(value).slice(0, 10) : ''
}

function datePayload(value) {
  return value ? `${value}T00:00:00Z` : null
}

function normalizedOptionalText(value) {
  const normalized = String(value || '').trim()
  return normalized || null
}

function lifecycleStatusLabel(status) {
  return (
    {
      ACTIVE: '正常',
      OFFLINE: '已下架',
      EXPIRED: '已过有效期',
      REVIEW_DUE: '待复核'
    }[status] || '状态未知'
  )
}

function lifecycleStatusColor(status) {
  return { ACTIVE: 'success', OFFLINE: 'default', EXPIRED: 'error', REVIEW_DUE: 'warning' }[status]
}

function sourceStatusLabel(status) {
  return (
    {
      ACTIVE: '正常',
      OFFLINE: '整篇已下架',
      OFFLINE_PENDING: '整篇下架中',
      RESTORE_PENDING: '整篇恢复中'
    }[status] || '状态处理中'
  )
}

function sourceStatusColor(status) {
  return {
    ACTIVE: 'success',
    OFFLINE: 'default',
    OFFLINE_PENDING: 'warning',
    RESTORE_PENDING: 'processing'
  }[status]
}

function formatDetectedAt(value) {
  const date = value ? new Date(value) : null
  if (!date || Number.isNaN(date.getTime())) return '时间未知'
  const pad = (part) => String(part).padStart(2, '0')
  return `${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`
}

function scopeSummary(scope) {
  if (!scope || !Object.keys(scope).length) return '未限定范围'
  return Object.values(scope).filter(Boolean).join(' · ')
}

function sourceCountLabel(record) {
  if (record.knowledge_level === 'UNIT') return `${record.source_segment_count || 0} 个来源片段`
  return `${record.chunk_count || 0} 个分块`
}

function sourceLocatorLabel(record) {
  const locator = record.source_locator || {}
  if (locator.slide) return `第 ${locator.slide} 页幻灯片`
  if (locator.page) return `第 ${locator.page} 页`
  if (locator.sheet) {
    if (locator.row_start && locator.row_end) {
      return `${locator.sheet} · 第 ${locator.row_start}-${locator.row_end} 行`
    }
    return `工作表 ${locator.sheet}`
  }
  return sourceCountLabel(record)
}
</script>

<style scoped lang="less">
.governance-card {
  margin: 10px var(--page-padding) 24px;
  padding: 14px 16px;
  border: 1px solid color-mix(in srgb, var(--gray-150) 45%, transparent);
  border-radius: 8px;
  background: var(--gray-0);
}
.section-heading {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  margin-bottom: 11px;
}
.section-heading h2 {
  margin: 0;
  font-size: 16px;
  font-weight: 600;
}
.section-heading p {
  margin: 3px 0 0;
  color: var(--color-text-tertiary);
  font-size: 12px;
}
.toolbar {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 10px;
  padding: 9px;
  background: var(--gray-25);
}
.toolbar :deep(.ant-input) {
  flex: 1;
  min-width: 220px;
}
.toolbar :deep(.ant-select) {
  width: 140px;
}
.toolbar-summary {
  margin-left: auto;
  color: var(--color-text-tertiary);
  font-size: 11px;
  white-space: nowrap;
}
.toolbar-summary strong {
  color: var(--color-text-secondary);
  font-weight: 650;
}
.knowledge-groups {
  display: grid;
  gap: 12px;
}
.source-group {
  overflow: hidden;
  border: 1px solid var(--gray-150);
  border-radius: 9px;
  background: var(--gray-0);
}
.source-group-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  min-height: 66px;
  padding: 12px 14px;
  background: linear-gradient(110deg, var(--gray-25), var(--gray-0));
}
.source-group-title {
  display: grid;
  flex: 1 1 360px;
  min-width: 0;
  gap: 3px;
}
.source-eyebrow {
  color: var(--main-700);
  font-size: 10px;
  font-weight: 650;
  letter-spacing: 0.04em;
}
.source-title-row {
  display: flex;
  align-items: center;
  min-width: 0;
  gap: 7px;
}
.source-title-row strong {
  flex: 1 1 auto;
  min-width: 0;
  overflow: hidden;
  color: var(--color-text);
  font-size: 14px;
  font-weight: 650;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.source-update-tag {
  flex: 0 0 auto;
  margin-inline-end: 0;
}
.source-update-action {
  display: inline-flex;
  flex: 0 0 auto;
  align-items: center;
  gap: 2px;
  padding: 2px 0;
  border: 0;
  background: transparent;
  color: var(--main-700);
  cursor: pointer;
  font-size: 11px;
  font-weight: 600;
}
.source-update-action:hover,
.source-update-action:focus-visible {
  color: var(--main-900);
}
.source-update-action:focus-visible {
  outline: 2px solid color-mix(in srgb, var(--main-500) 45%, transparent);
  outline-offset: 2px;
}
.source-detail-row {
  display: flex;
  align-items: center;
  min-width: 0;
  gap: 10px;
}
.source-path {
  min-width: 0;
  overflow: hidden;
  color: var(--color-text-tertiary);
  font-size: 11px;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.source-update-meta {
  flex: 0 0 auto;
  color: var(--color-warning-text, #ad6800);
  font-size: 11px;
  white-space: nowrap;
}
.source-group-summary {
  display: flex;
  flex: 0 0 auto;
  align-items: center;
  gap: 12px;
  color: var(--color-text-tertiary);
  font-size: 11px;
  white-space: nowrap;
}
.source-group-summary > span strong {
  color: var(--color-text-secondary);
  font-size: 13px;
  font-weight: 650;
}
.source-group-summary :deep(.ant-btn) {
  display: inline-flex;
  align-items: center;
  gap: 4px;
}
.group-toggle {
  display: inline-flex;
  align-items: center;
  gap: 3px;
  padding: 3px 0 3px 4px;
  border: 0;
  background: transparent;
  color: var(--main-700);
  cursor: pointer;
  font-size: 11px;
}
.group-toggle svg {
  transition: transform 160ms ease;
}
.group-toggle svg.rotated {
  transform: rotate(-90deg);
}
.knowledge-list {
  border-top: 1px solid var(--gray-100);
}
.knowledge-list-head,
.knowledge-row {
  display: grid;
  grid-template-columns: minmax(0, 1fr) 180px 100px;
  align-items: center;
  gap: 16px;
}
.knowledge-list-head {
  min-height: 32px;
  padding: 0 14px;
  background: var(--gray-10);
  color: var(--color-text-tertiary);
  font-size: 10px;
  font-weight: 600;
}
.knowledge-list-head span:nth-child(2),
.knowledge-list-head span:nth-child(3) {
  text-align: left;
}
.knowledge-row {
  width: 100%;
  min-height: 78px;
  padding: 11px 14px;
  border: 0;
  border-top: 1px solid var(--gray-100);
  background: var(--gray-0);
  text-align: left;
  transition:
    background 140ms ease,
    box-shadow 140ms ease;
}
.knowledge-row:hover,
.knowledge-row:focus-visible {
  background: var(--main-30);
  box-shadow: inset 3px 0 0 var(--main-color);
  outline: none;
}
.knowledge-main {
  display: grid;
  min-width: 0;
  gap: 3px;
}
.knowledge-kicker {
  display: flex;
  align-items: center;
  gap: 7px;
  min-height: 16px;
}
.locator-label {
  color: var(--color-text-tertiary);
  font-size: 10px;
}
.primary-cell {
  overflow: hidden;
  color: var(--color-text);
  font-size: 14px;
  font-weight: 650;
  line-height: 1.35;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.sub-line {
  overflow: hidden;
  margin-top: 1px;
  color: var(--color-text-tertiary);
  font-size: 11px;
  line-height: 1.4;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.knowledge-row-status {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 5px;
}
.knowledge-row-status :deep(.ant-tag) {
  margin: 0;
  font-size: 10px;
}
.knowledge-action {
  display: inline-flex;
  align-items: center;
  justify-content: flex-start;
  gap: 3px;
  color: var(--main-700);
  font-size: 11px;
  white-space: nowrap;
}
.knowledge-empty {
  display: grid;
  min-height: 180px;
  place-items: center;
  border: 1px solid var(--gray-100);
  border-radius: 8px;
  background: var(--gray-10);
}
.knowledge-level {
  display: inline-flex;
  padding: 1px 5px;
  border-radius: 4px;
  background: var(--main-30);
  color: var(--main-700);
  font-size: 9px;
  font-weight: 600;
}
.knowledge-level.legacy {
  background: var(--gray-75);
  color: var(--color-text-tertiary);
}
.source-count {
  display: block;
  color: var(--color-text-secondary);
  font-size: 12px;
  font-weight: 550;
}
.source-link {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  margin-left: auto;
  color: var(--main-700);
  white-space: nowrap;
}
.source-group-summary .source-link {
  margin-left: 0;
}
.detail-facts {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 10px;
  margin-bottom: 14px;
  padding: 11px 13px;
  border: 1px solid var(--gray-100);
  border-radius: 7px;
  background: var(--gray-10);
}
.detail-facts > div {
  display: grid;
  min-width: 0;
  gap: 3px;
}
.detail-facts span {
  color: var(--color-text-tertiary);
  font-size: 10px;
}
.detail-facts strong,
.detail-facts code {
  overflow: hidden;
  color: var(--color-text-secondary);
  font-family: inherit;
  font-size: 11px;
  font-weight: 600;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.detail-facts code {
  color: var(--color-text-tertiary);
  font-size: 10px;
  font-weight: 400;
}
.detail-facts :deep(.ant-tag) {
  width: fit-content;
  margin: 0;
  font-size: 10px;
}
.lifecycle-actions {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 12px;
  padding: 10px 12px;
  border-left: 3px solid var(--main-color);
  background: var(--main-30);
}
.lifecycle-actions > div {
  display: grid;
  min-width: 0;
  margin-right: auto;
  gap: 2px;
}
.lifecycle-actions strong,
.metadata-heading strong,
.version-heading strong {
  color: var(--color-text-secondary);
  font-size: 12px;
  font-weight: 650;
}
.lifecycle-actions span,
.metadata-heading span,
.version-heading span {
  color: var(--color-text-tertiary);
  font-size: 10px;
}
.lifecycle-actions :deep(.ant-btn),
.metadata-heading :deep(.ant-btn) {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  flex: 0 0 auto;
}
.metadata-editor {
  margin-bottom: 14px;
  padding: 11px 12px 12px;
  border: 1px solid var(--gray-100);
  border-radius: 7px;
}
.metadata-heading,
.version-heading {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}
.metadata-heading > div {
  display: grid;
  gap: 2px;
}
.metadata-fields {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 9px;
  margin-top: 10px;
}
.metadata-fields label,
.reason-field {
  display: grid;
  gap: 5px;
}
.metadata-fields label > span,
.reason-field > span {
  color: var(--color-text-secondary);
  font-size: 10px;
  font-weight: 600;
}
.version-heading {
  margin: 2px 0 8px;
}
.operation-muted {
  color: var(--color-text-tertiary);
}
.action-description {
  margin: 0 0 14px;
  color: var(--color-text-secondary);
  font-size: 13px;
  line-height: 1.6;
}
.source-trace {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 14px;
  padding: 11px 13px;
  border: 1px solid var(--gray-100);
  border-radius: 7px;
  background: var(--gray-25);
  color: var(--gray-300);
}
.trace-step {
  display: grid;
  min-width: 0;
  gap: 2px;
}
.trace-step span {
  color: var(--color-text-tertiary);
  font-size: 9px;
}
.trace-step strong {
  overflow: hidden;
  max-width: 180px;
  color: var(--color-text-secondary);
  font-size: 12px;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.trace-step small {
  overflow: hidden;
  max-width: 180px;
  color: var(--color-text-tertiary);
  font-size: 10px;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.trace-step.active {
  position: relative;
  padding-left: 10px;
}
.trace-step.active::before {
  position: absolute;
  top: 1px;
  bottom: 1px;
  left: 0;
  width: 3px;
  border-radius: 2px;
  background: var(--main-color);
  content: '';
}
.trace-step.active span,
.trace-step.active strong {
  color: var(--main-700);
}
.table-footer {
  margin-top: 9px;
  color: var(--color-text-tertiary);
  font-size: 12px;
}
@media (max-width: 760px) {
  .toolbar {
    align-items: stretch;
    flex-wrap: wrap;
  }
  .toolbar-summary {
    width: 100%;
    margin-left: 0;
  }
  .source-group-header {
    align-items: flex-start;
    flex-direction: column;
    gap: 8px;
  }
  .source-group-summary {
    flex-wrap: wrap;
    gap: 8px 12px;
  }
  .source-detail-row {
    align-items: flex-start;
    flex-direction: column;
    gap: 2px;
  }
  .knowledge-list-head {
    display: none;
  }
  .knowledge-row {
    grid-template-columns: minmax(0, 1fr) auto;
    gap: 8px;
  }
  .knowledge-row-status {
    grid-column: 2;
    grid-row: 1;
    justify-content: flex-end;
  }
  .knowledge-action {
    grid-column: 1 / -1;
    grid-row: 2;
  }
  .detail-facts {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
  .source-trace {
    align-items: stretch;
    flex-direction: column;
  }
  .source-trace > svg {
    display: none;
  }
  .source-link {
    margin-left: 0;
  }
  .lifecycle-actions {
    align-items: flex-start;
    flex-wrap: wrap;
  }
  .lifecycle-actions > div {
    width: 100%;
  }
  .metadata-fields {
    grid-template-columns: 1fr;
  }
}
</style>
