<template>
  <section class="governance-card">
    <div class="section-heading">
      <div>
        <h2>跨文档检查</h2>
        <p>先筛选高相关资料，再比较关键差异；检查在后台运行，不影响资料审核。</p>
      </div>
      <div class="heading-actions">
        <a-button size="small" :loading="backfillLoading" @click="startBackfill">
          <ScanSearch :size="15" />补跑检查
        </a-button>
        <a-button size="small" :loading="loading" @click="loadRelations"><RefreshCw :size="15" />刷新</a-button>
      </div>
    </div>
    <div class="comparison-status" :class="`status-${comparisonStatus.status}`">
      <span class="status-dot" />
      <strong>{{ comparisonStatusLabel }}</strong>
      <span v-if="comparisonStatus.total">{{ comparisonStatus.completed }}/{{ comparisonStatus.total }} 份资料</span>
      <span v-if="comparisonStatus.relation_count">· {{ comparisonStatus.relation_count }} 条关系</span>
      <span v-if="comparisonStatus.status === 'completed' || comparisonStatus.issue_count">· {{ comparisonStatus.issue_count || 0 }} 条待处理</span>
      <span v-if="comparisonTask?.message" class="status-message">· {{ comparisonTask.message }}</span>
    </div>
    <div class="toolbar">
      <a-select v-model:value="relationType" size="small" :options="relationOptions" />
      <a-select v-model:value="relationStatus" size="small" :options="statusOptions" />
      <a-input v-model:value="searchText" size="small" placeholder="搜索产品或资料" allow-clear />
    </div>
    <div class="relation-legend" aria-label="关系标记说明">
      <span class="legend-label">关系标记</span>
      <span class="legend-item legend-conflict"><i />冲突：需人工裁决</span>
      <span class="legend-item legend-overlap"><i />重叠：存在共同内容</span>
      <span class="legend-item legend-duplicate"><i />完全重复：不重复建知识</span>
    </div>
    <a-table :columns="columns" :data-source="filteredRelations" :loading="loading" :pagination="{ pageSize: 10 }" row-key="relation_id" size="small" :scroll="{ x: 1140 }" :row-class-name="relationRowClass">
      <template #bodyCell="{ column, record }">
        <template v-if="column.key === 'relation_type'">
          <div class="relation-cell">
            <a-tag :color="relationColor(record.relation_type)">{{ relationLabel(record.relation_type) }}</a-tag>
            <span class="relation-signal" :class="`signal-${record.relation_type?.toLowerCase()}`">{{ relationSignal(record.relation_type) }}</span>
          </div>
        </template>
        <template v-else-if="column.key === 'topic'"><strong class="primary-cell">{{ record.source_title }}</strong><span class="sub-line">{{ record.reasoning || '等待人工确认关系' }}</span></template>
        <template v-else-if="column.key === 'sources'">
          <div class="source-pair">
            <div class="source-entry">
              <span class="source-label">来源一</span>
              <div class="source-info">
                <strong :title="record.source_title">{{ record.source_title }}</strong>
                <span :title="record.source_path || '未记录目录'">{{ record.source_path || '未记录目录' }}</span>
              </div>
            </div>
            <div class="source-entry">
              <span class="source-label">来源二</span>
              <div class="source-info">
                <strong :title="record.target_title">{{ record.target_title }}</strong>
                <span :title="record.target_path || '未记录目录'">{{ record.target_path || '未记录目录' }}</span>
              </div>
            </div>
          </div>
        </template>
        <template v-else-if="column.key === 'evidence'">
          <div class="evidence-inline">
            <span v-if="record.different_content?.length" class="evidence-chip evidence-chip-diff">差异 {{ record.different_content.length }}</span>
            <span v-if="record.same_content?.length" class="evidence-chip evidence-chip-same">相同 {{ record.same_content.length }}</span>
            <span v-if="!record.different_content?.length && !record.same_content?.length" class="sub-line">待补充证据</span>
          </div>
        </template>
        <template v-else-if="column.key === 'scope'"><span>{{ record.scope_difference?.summary || '范围待确认' }}</span></template>
        <template v-else-if="column.key === 'status'"><a-tag :color="relationStatusColor(record)">{{ relationStatusLabel(record) }}</a-tag></template>
        <template v-else-if="column.key === 'action'"><a-button size="small" @click="$emit('open-review', record)">进入审核</a-button></template>
      </template>
      <template #emptyText><a-empty :description="emptyDescription" /></template>
    </a-table>
    <div class="table-footer">当前显示 {{ filteredRelations.length }} 条 · 全部 {{ comparisonStatus.relation_count || 0 }} 条 · 待处理 {{ comparisonStatus.issue_count || 0 }} 条</div>
  </section>
</template>

<script setup>
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { message } from 'ant-design-vue'
import { RefreshCw, ScanSearch } from 'lucide-vue-next'
import { governanceApi } from '@/apis/governance_api'
import { taskerApi } from '@/apis/tasker'

const props = defineProps({ sourceId: { type: String, default: '' } })
const emit = defineEmits(['open-review', 'count-change'])
const relations = ref([])
const loading = ref(false)
const backfillLoading = ref(false)
const comparisonTask = ref(null)
const comparisonStatus = ref({ status: 'not_started', total: 0, completed: 0, relation_count: 0, issue_count: 0 })
let taskPollTimer = null
const relationType = ref('')
const relationStatus = ref('')
const searchText = ref('')
const relationOptions = [
  { label: '全部关系', value: '' },
  { label: '内容冲突', value: 'CONFLICT' },
  { label: '完全重复', value: 'EXACT_DUPLICATE' },
  { label: '内容重叠', value: 'OVERLAP' },
  { label: '条件变体', value: 'CONDITIONAL_VARIANT' },
  { label: '证据不足', value: 'INSUFFICIENT' }
]
const statusOptions = [{ label: '全部状态', value: '' }, { label: '待裁决', value: 'open' }, { label: '已处理', value: 'resolved' }]
const columns = [
  { title: '关系', key: 'relation_type', width: 100 },
  { title: '比较主题', key: 'topic', width: 270 },
  { title: '对比文件', key: 'sources', width: 270 },
  { title: '证据标记', key: 'evidence', width: 150 },
  { title: '适用范围', key: 'scope', width: 150 },
  { title: '处理状态', key: 'status', width: 100 },
  { title: '操作', key: 'action', width: 100 }
]
const filteredRelations = computed(() => {
  const keyword = searchText.value.trim().toLowerCase()
  if (!keyword) return relations.value
  return relations.value.filter((item) => `${item.source_title} ${item.target_title} ${item.reasoning || ''}`.toLowerCase().includes(keyword))
})
const comparisonStatusLabel = computed(() => ({
  not_started: '尚未执行跨文档检查',
  queued: '跨文档检查排队中',
  running: '跨文档检查进行中',
  completed: '跨文档检查已完成',
  failed: '跨文档检查失败'
}[comparisonStatus.value.status] || '跨文档检查状态未知'))
const emptyDescription = computed(() => {
  if (comparisonStatus.value.status === 'not_started') return '尚未执行跨文档检查'
  if (comparisonStatus.value.status === 'queued' || comparisonStatus.value.status === 'running') return '检查进行中，完成后会显示关系'
  if (comparisonStatus.value.status === 'failed') return '检查失败，请点击“补跑检查”重试'
  return '已完成检查，暂未发现需要人工裁决的问题'
})

watch(() => [props.sourceId, relationType.value, relationStatus.value], loadRelations, { immediate: true })
onBeforeUnmount(() => clearTimeout(taskPollTimer))

async function loadRelations() {
  if (!props.sourceId) return
  loading.value = true
  try {
    const [response, status] = await Promise.all([
      governanceApi.listRelations(props.sourceId, { relation_type: relationType.value || undefined, status: relationStatus.value || undefined }),
      governanceApi.getComparisonStatus(props.sourceId)
    ])
    relations.value = response.items || []
    comparisonStatus.value = status || comparisonStatus.value
    emit('count-change', comparisonStatus.value.issue_count || 0)
  } catch (error) {
    message.error(governanceApi.getErrorMessage(error, '加载跨文档问题失败'))
  } finally {
    loading.value = false
  }
}
async function startBackfill() {
  if (!props.sourceId || backfillLoading.value) return
  backfillLoading.value = true
  try {
    const response = await governanceApi.backfillComparisons(props.sourceId)
    comparisonTask.value = { id: response.task_id, status: response.status, message: '已提交后台检查' }
    comparisonStatus.value = { ...comparisonStatus.value, status: 'queued' }
    pollComparisonTask()
  } catch (error) {
    message.error(governanceApi.getErrorMessage(error, '提交跨文档检查失败'))
    backfillLoading.value = false
  }
}
function pollComparisonTask() {
  clearTimeout(taskPollTimer)
  if (!comparisonTask.value?.id) return
  taskPollTimer = setTimeout(async () => {
    try {
      const response = await taskerApi.fetchTaskDetail(comparisonTask.value.id)
      comparisonTask.value = response.task || comparisonTask.value
      if (['success', 'failed', 'cancelled'].includes(comparisonTask.value.status)) {
        backfillLoading.value = false
        await loadRelations()
        if (comparisonTask.value.status === 'failed') message.error('跨文档检查失败，请查看任务详情后重试')
      } else {
        pollComparisonTask()
      }
    } catch {
      backfillLoading.value = false
    }
  }, 1200)
}
function relationLabel(value) { return { CONFLICT: '内容冲突', EXACT_DUPLICATE: '完全重复', OVERLAP: '内容重叠', CONDITIONAL_VARIANT: '条件变体', COMPLEMENTARY: '互补内容', INSUFFICIENT: '证据不足' }[value] || value || '待比较' }
function relationColor(value) { return { CONFLICT: 'error', EXACT_DUPLICATE: 'success', OVERLAP: 'warning', CONDITIONAL_VARIANT: 'processing', INSUFFICIENT: 'warning' }[value] || 'default' }
function relationSignal(value) { return { CONFLICT: '需裁决', OVERLAP: '有共同内容', EXACT_DUPLICATE: '已识别重复', CONDITIONAL_VARIANT: '范围不同', INSUFFICIENT: '证据不足' }[value] || '待确认' }
function relationRowClass(record) { return `relation-row-${String(record.relation_type || '').toLowerCase()}` }
function relationNeedsAction(record) { return record.status === 'open' && ['CONFLICT', 'INSUFFICIENT'].includes(record.relation_type) }
function relationStatusLabel(record) { return record.status !== 'open' ? '已处理' : relationNeedsAction(record) ? '待处理' : '已识别' }
function relationStatusColor(record) { return record.status !== 'open' ? 'success' : relationNeedsAction(record) ? 'error' : 'default' }
</script>

<style scoped lang="less">
.governance-card { margin: 10px var(--page-padding) 24px; padding: 14px 16px; border: 1px solid color-mix(in srgb, var(--gray-150) 45%, transparent); border-radius: 8px; background: var(--gray-0); }
.section-heading { display: flex; align-items: center; justify-content: space-between; gap: 16px; margin-bottom: 11px; }
.heading-actions { display: flex; align-items: center; gap: 7px; flex-shrink: 0; }
.section-heading h2 { margin: 0; font-size: 16px; font-weight: 600; }
.section-heading p { margin: 3px 0 0; color: var(--color-text-tertiary); font-size: 12px; }
.comparison-status { display: flex; align-items: center; gap: 6px; min-height: 28px; margin-bottom: 10px; padding: 0 9px; border-radius: 5px; background: var(--gray-25); color: var(--color-text-tertiary); font-size: 12px; }
.comparison-status strong { color: var(--color-text-secondary); font-weight: 550; }
.status-dot { width: 6px; height: 6px; border-radius: 50%; background: var(--gray-300); }
.status-queued .status-dot, .status-running .status-dot { background: var(--color-primary); }
.status-running .status-dot { animation: pulse 1.2s ease-in-out infinite; }
.status-completed .status-dot { background: #52a36a; }
.status-failed .status-dot { background: #cf4b4b; }
.status-message { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
@keyframes pulse { 50% { opacity: .35; } }
.toolbar { display: flex; gap: 8px; margin-bottom: 10px; padding: 9px; background: var(--gray-25); }
.toolbar :deep(.ant-input), .toolbar :deep(.ant-select) { width: 170px; }
.relation-legend { display: flex; align-items: center; flex-wrap: wrap; gap: 8px 14px; min-height: 28px; margin: -2px 0 9px; color: var(--color-text-tertiary); font-size: 11px; }
.legend-label { color: var(--color-text-secondary); font-weight: 600; }
.legend-item { display: inline-flex; align-items: center; gap: 5px; }
.legend-item i { width: 8px; height: 8px; border-radius: 2px; }
.legend-conflict i { background: #d45a5f; }
.legend-overlap i { background: #d6aa36; }
.legend-duplicate i { background: #68a57b; }
.relation-cell { display: flex; flex-direction: column; align-items: flex-start; gap: 4px; }
.relation-signal { font-size: 11px; line-height: 1; }
.signal-conflict { color: #bd3f45; font-weight: 600; }
.signal-overlap { color: #9b7116; }
.signal-exact_duplicate { color: #43875c; }
.source-pair { display: grid; gap: 7px; }
.source-entry { display: grid; grid-template-columns: 38px minmax(0, 1fr); gap: 6px; min-width: 0; align-items: start; }
.source-label { display: inline-flex; min-height: 17px; align-items: center; justify-content: center; border: 1px solid var(--gray-150); border-radius: 3px; color: var(--color-text-tertiary); font-size: 10px; line-height: 17px; }
.source-info { min-width: 0; }
.source-info strong, .source-info span { display: block; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.source-info strong { color: var(--color-text); font-size: 12px; font-weight: 550; line-height: 1.35; }
.source-info span { margin-top: 2px; color: var(--color-text-tertiary); font-size: 10px; line-height: 1.3; }
.evidence-inline { display: flex; flex-wrap: wrap; gap: 4px; }
.evidence-chip { display: inline-flex; align-items: center; min-height: 20px; padding: 0 6px; border-radius: 4px; font-size: 11px; line-height: 20px; }
.evidence-chip-diff { color: #a63840; background: #fff1f1; }
.evidence-chip-same { color: #8b6712; background: #fff8df; }
.governance-card :deep(.relation-row-conflict > td) { background: #fff8f8; }
.governance-card :deep(.relation-row-conflict > td:first-child) { box-shadow: inset 3px 0 0 #d45a5f; }
.governance-card :deep(.relation-row-overlap > td) { background: #fffdf4; }
.governance-card :deep(.relation-row-overlap > td:first-child) { box-shadow: inset 3px 0 0 #d6aa36; }
.governance-card :deep(.relation-row-exact_duplicate > td:first-child) { box-shadow: inset 3px 0 0 #68a57b; }
.primary-cell { display: block; color: var(--color-text); font-weight: 550; }
.sub-line { display: block; margin-top: 3px; color: var(--color-text-tertiary); font-size: 11px; line-height: 1.45; }
.table-footer { margin-top: 9px; color: var(--color-text-tertiary); font-size: 12px; }
</style>
