<template>
  <main class="workbench-page">
    <header class="page-heading">
      <div>
        <h1>知识加工</h1>
        <p>管理飞书素材的扫描、审核与发布状态</p>
      </div>
      <div class="heading-actions">
        <a-button :loading="checkingSource" :disabled="!currentSource" @click="checkCurrentSource">
          <Link2 :size="16" />
          检查连接
        </a-button>
        <a-button
          data-testid="scan-incremental"
          :loading="scanningMode === 'incremental'"
          :disabled="!currentSource || scanLocked"
          @click="startScan('incremental')"
        >
          <RefreshCw :size="16" />
          增量扫描
        </a-button>
        <a-button
          data-testid="scan-full"
          type="primary"
          :loading="scanningMode === 'full'"
          :disabled="!currentSource || scanLocked"
          @click="startScan('full')"
        >
          <ScanSearch :size="16" />
          全量扫描
        </a-button>
      </div>
    </header>

    <a-alert v-if="pageError" class="page-alert" type="error" show-icon :message="pageError" />

    <a-spin :spinning="loadingSources">
      <template v-if="currentSource">
        <section class="source-strip" aria-label="飞书数据源">
          <div class="source-identity">
            <div class="source-icon"><BookOpen :size="20" /></div>
            <div>
              <label>数据源</label>
              <strong>{{ currentSource.name }}</strong>
            </div>
          </div>
          <div class="source-field source-field-wide">
            <label>Wiki 根节点</label>
            <a
              v-if="currentSource.wiki_root_url"
              :href="currentSource.wiki_root_url"
              target="_blank"
              rel="noopener noreferrer"
            >
              {{ currentSource.wiki_root_url }}
            </a>
            <span v-else>{{ currentSource.wiki_root_token || '-' }}</span>
          </div>
          <div class="source-field">
            <label>目标知识库</label>
            <strong>{{ currentSource.target_kb_id || '-' }}</strong>
          </div>
          <div class="source-field">
            <label>最近全量 / 增量</label>
            <span>{{ formatTime(currentSource.last_full_sync_at) }}</span>
            <span>{{ formatTime(currentSource.last_incremental_sync_at) }}</span>
          </div>
        </section>

        <section class="stats-row" aria-label="素材统计">
          <div v-for="stat in stats" :key="stat.label" class="stat-item">
            <span>{{ stat.label }}</span>
            <strong :class="stat.tone">{{ stat.value }}</strong>
          </div>
        </section>

        <section class="workspace-section">
          <div class="section-heading">
            <div>
              <h2>扫描批次</h2>
              <p>选择批次可查看该次扫描产生或变更的素材</p>
            </div>
            <button
              v-if="selectedRunId"
              type="button"
              class="clear-filter"
              @click="selectRun('')"
            >
              清除批次筛选
            </button>
          </div>
          <FeishuSyncRunsTable
            :runs="runs"
            :loading="loadingRuns"
            :selected-run-id="selectedRunId"
            @select="selectRun"
          />
        </section>

        <section class="workspace-section material-section">
          <div class="section-heading material-heading">
            <div>
              <h2>素材队列</h2>
              <p>{{ selectedRunId ? '正在查看所选批次素材' : '显示当前数据源的全部素材版本' }}</p>
            </div>
            <a-button aria-label="刷新素材" @click="loadMaterials">
              <RefreshCw :size="16" />
              刷新
            </a-button>
          </div>

          <form class="filter-bar" @submit.prevent="loadMaterials">
            <a-select
              v-model:value="filters.processing_status"
              allow-clear
              placeholder="加工状态"
              :options="processingOptions"
            />
            <a-select
              v-model:value="filters.review_status"
              allow-clear
              placeholder="审核状态"
              :options="reviewOptions"
            />
            <a-select
              v-model:value="filters.source_validity"
              allow-clear
              placeholder="来源状态"
              :options="validityOptions"
            />
            <a-select
              v-model:value="filters.item_type"
              allow-clear
              placeholder="素材类型"
              :options="typeOptions"
            />
            <a-input v-model:value="filters.directory" allow-clear placeholder="飞书目录路径" />
            <a-range-picker v-model:value="updatedRange" show-time />
            <a-button html-type="submit" type="primary">筛选</a-button>
            <a-button @click="resetFilters">重置</a-button>
          </form>

          <FeishuMaterialTable
            ref="materialTableRef"
            :materials="materials"
            :loading="loadingMaterials"
            :max-selection="MAX_BATCH_SIZE"
            @open-detail="openMaterialDetail"
            @action="handleMaterialAction"
            @batch-action="handleBatchAction"
            @selection-limit="(limit) => message.warning(`单次批量操作最多选择 ${limit} 条素材`)"
          />
        </section>
      </template>

      <a-empty v-else class="empty-source" description="尚未配置飞书知识数据源" />
    </a-spin>

    <FeishuMaterialDetailDrawer
      :open="detailOpen"
      :material="detailMaterial"
      :events="detailEvents"
      :content="detailContent"
      :loading="loadingDetail"
      @close="closeDetail"
    />
  </main>
</template>

<script setup>
import { computed, h, onBeforeUnmount, onMounted, reactive, ref } from 'vue'
import { Input, message, Modal } from 'ant-design-vue'
import { BookOpen, Link2, RefreshCw, ScanSearch } from 'lucide-vue-next'

import { feishuKnowledgeApi, MAX_BATCH_SIZE } from '@/apis/feishu_knowledge_api'
import { documentApi } from '@/apis/knowledge_api'
import FeishuMaterialDetailDrawer from '@/components/feishu/FeishuMaterialDetailDrawer.vue'
import FeishuMaterialTable from '@/components/feishu/FeishuMaterialTable.vue'
import FeishuSyncRunsTable from '@/components/feishu/FeishuSyncRunsTable.vue'

const TERMINAL_RUN_STATUSES = new Set(['succeeded', 'failed', 'cancelled'])

const sources = ref([])
const currentSourceId = ref('')
const runs = ref([])
const materials = ref([])
const selectedRunId = ref('')
const activeRunId = ref('')
const scanningMode = ref('')
const checkingSource = ref(false)
const loadingSources = ref(false)
const loadingRuns = ref(false)
const loadingMaterials = ref(false)
const pageError = ref('')
const detailOpen = ref(false)
const detailMaterial = ref(null)
const detailEvents = ref([])
const detailContent = ref(emptyDetailContent())
const loadingDetail = ref(false)
const updatedRange = ref([])
const materialTableRef = ref(null)
let pollTimer = null
let detailRequestSeq = 0
let isAlive = true

function emptyDetailContent() {
  return { content: '', lines: [], loading: false, error: '' }
}

const filters = reactive({
  processing_status: undefined,
  review_status: undefined,
  source_validity: undefined,
  item_type: undefined,
  directory: ''
})

const processingOptions = [
  { label: '已发现', value: 'discovered' },
  { label: '归档中', value: 'syncing' },
  { label: '已归档', value: 'synced' },
  { label: '等待加工', value: 'processing_queued' },
  { label: '加工中', value: 'processing' },
  { label: '待审核', value: 'awaiting_review' },
  { label: '解析失败', value: 'parse_failed' },
  { label: '等待发布', value: 'publish_queued' },
  { label: '发布中', value: 'publishing' },
  { label: '已发布', value: 'published' },
  { label: '发布失败', value: 'publish_failed' },
  { label: '已替换', value: 'replaced' },
  { label: '不支持', value: 'unsupported' },
  { label: '待下架', value: 'removal_pending' },
  { label: '下架失败', value: 'removal_failed' },
  { label: '已下架', value: 'removed' }
]
const reviewOptions = [
  { label: '待审核', value: 'pending' },
  { label: '已通过', value: 'approved' },
  { label: '已驳回', value: 'rejected' }
]
const validityOptions = [
  { label: '来源有效', value: 'valid' },
  { label: '来源失效', value: 'invalid' }
]
const typeOptions = [
  { label: '飞书页面', value: 'page' },
  { label: '附件', value: 'attachment' },
  { label: '音频', value: 'audio' },
  { label: '视频', value: 'video' }
]

const currentSource = computed(() =>
  sources.value.find((item) => item.source_id === currentSourceId.value)
)

const hasActiveRun = computed(() =>
  runs.value.some((run) => run.status === 'queued' || run.status === 'running')
)
const scanLocked = computed(() => Boolean(scanningMode.value || activeRunId.value || hasActiveRun.value))

const stats = computed(() => [
  { label: '素材总数', value: currentSource.value?.total_count ?? 0 },
  { label: '待审核', value: currentSource.value?.awaiting_review_count ?? 0, tone: 'warning' },
  { label: '失败', value: currentSource.value?.failed_count ?? 0, tone: 'error' },
  { label: '来源失效', value: currentSource.value?.source_invalid_count ?? 0, tone: 'muted' }
])

async function loadSources() {
  const response = await feishuKnowledgeApi.listSources()
  sources.value = response.items || []
  if (!sources.value.some((item) => item.source_id === currentSourceId.value)) {
    currentSourceId.value = sources.value[0]?.source_id || ''
  }
}

async function loadRuns() {
  if (!currentSourceId.value) return
  loadingRuns.value = true
  try {
    const response = await feishuKnowledgeApi.listRuns(currentSourceId.value)
    runs.value = response.items || []
    const activeRun = runs.value.find((run) => run.status === 'queued' || run.status === 'running')
    if (activeRun && !activeRunId.value) {
      activeRunId.value = activeRun.run_id
      schedulePoll()
    }
  } finally {
    loadingRuns.value = false
  }
}

function materialQuery() {
  return {
    ...filters,
    run_id: selectedRunId.value || undefined,
    updated_from: updatedRange.value?.[0]?.toISOString?.(),
    updated_to: updatedRange.value?.[1]?.toISOString?.()
  }
}

async function loadMaterials() {
  if (!currentSourceId.value) return
  loadingMaterials.value = true
  try {
    const response = await feishuKnowledgeApi.listMaterials(currentSourceId.value, materialQuery())
    materials.value = response.items || []
  } catch (error) {
    message.error(feishuKnowledgeApi.getErrorMessage(error, '加载素材失败'))
  } finally {
    loadingMaterials.value = false
  }
}

async function refreshAll() {
  await loadSources()
  if (currentSourceId.value) await Promise.all([loadRuns(), loadMaterials()])
}

async function initialize() {
  loadingSources.value = true
  pageError.value = ''
  try {
    await refreshAll()
  } catch (error) {
    pageError.value = feishuKnowledgeApi.getErrorMessage(error, '加载知识加工工作台失败')
  } finally {
    loadingSources.value = false
  }
}

async function checkCurrentSource() {
  if (!currentSource.value) return
  checkingSource.value = true
  try {
    await feishuKnowledgeApi.checkSource(currentSource.value.source_id)
    message.success('飞书数据源连接正常')
  } catch (error) {
    message.error(feishuKnowledgeApi.getErrorMessage(error, '连接检查失败'))
  } finally {
    checkingSource.value = false
  }
}

async function startScan(mode) {
  if (!currentSource.value || scanLocked.value) return
  scanningMode.value = mode
  try {
    const result = await feishuKnowledgeApi.scanSource(currentSource.value.source_id, mode)
    activeRunId.value = result.run_id
    if (!runs.value.some((run) => run.run_id === result.run_id)) {
      runs.value = [
        {
          run_id: result.run_id,
          source_id: currentSource.value.source_id,
          run_type: mode,
          status: result.status || 'queued'
        },
        ...runs.value
      ]
    }
    message.success(`${mode === 'full' ? '全量' : '增量'}扫描已提交`)
    schedulePoll()
  } catch (error) {
    message.error(feishuKnowledgeApi.getErrorMessage(error, '扫描提交失败'))
  } finally {
    scanningMode.value = ''
  }
}

function schedulePoll() {
  if (!isAlive || !activeRunId.value) return
  clearTimeout(pollTimer)
  pollTimer = setTimeout(pollActiveRun, 2000)
}

async function pollActiveRun() {
  const runId = activeRunId.value
  if (!isAlive || !runId) return
  try {
    const run = await feishuKnowledgeApi.getRun(runId)
    if (!isAlive || activeRunId.value !== runId) return
    const index = runs.value.findIndex((item) => item.run_id === run.run_id)
    const runType = run.run_type || (index >= 0 ? runs.value[index].run_type : '')
    if (index >= 0) runs.value.splice(index, 1, run)
    else runs.value.unshift(run)
    if (TERMINAL_RUN_STATUSES.has(run.status)) {
      activeRunId.value = ''
      await refreshAll()
      if (!isAlive) return
      if (run.status === 'succeeded') {
        message.success(`${runType === 'full' ? '全量' : '增量'}扫描完成`)
      } else {
        message.error(run.error_summary || '扫描任务未成功完成')
      }
      return
    }
    schedulePoll()
  } catch (error) {
    if (!isAlive || activeRunId.value !== runId) return
    message.error(feishuKnowledgeApi.getErrorMessage(error, '获取扫描进度失败'))
    try {
      await loadRuns()
    } catch {
      // 仅 getRun 返回明确终态时才结束当前轮询。
    }
    if (isAlive && activeRunId.value === runId) schedulePoll()
  }
}

function selectRun(runId) {
  selectedRunId.value = runId
  loadMaterials()
}

function resetFilters() {
  Object.assign(filters, {
    processing_status: undefined,
    review_status: undefined,
    source_validity: undefined,
    item_type: undefined,
    directory: ''
  })
  updatedRange.value = []
  loadMaterials()
}

async function openMaterialDetail(material) {
  const requestId = ++detailRequestSeq
  detailOpen.value = true
  detailMaterial.value = material
  detailEvents.value = []
  detailContent.value = emptyDetailContent()
  loadingDetail.value = true
  let detail
  try {
    const [materialDetail, events] = await Promise.all([
      feishuKnowledgeApi.getMaterial(material.version_id),
      feishuKnowledgeApi.listMaterialEvents(material.version_id)
    ])
    if (requestId !== detailRequestSeq) return
    detail = materialDetail
    detailMaterial.value = detail
    detailEvents.value = events.items || []
  } catch (error) {
    if (requestId !== detailRequestSeq) return
    message.error(feishuKnowledgeApi.getErrorMessage(error, '加载素材详情失败'))
  } finally {
    if (requestId === detailRequestSeq) loadingDetail.value = false
  }

  if (!detail?.target_kb_id || !detail?.yuxi_file_id || requestId !== detailRequestSeq) return

  detailContent.value = { ...emptyDetailContent(), loading: true }
  try {
    const response = await documentApi.getDocumentContent(detail.target_kb_id, detail.yuxi_file_id)
    if (requestId !== detailRequestSeq) return
    if (response?.status === 'failed') {
      throw new Error(response.message || '加载解析内容失败')
    }
    detailContent.value = {
      content: response?.content || '',
      lines: response?.lines || [],
      loading: false,
      error: ''
    }
  } catch (error) {
    if (requestId !== detailRequestSeq) return
    detailContent.value = {
      ...emptyDetailContent(),
      error: feishuKnowledgeApi.getErrorMessage(error, '加载解析内容失败')
    }
  }
}

function closeDetail() {
  detailRequestSeq += 1
  detailOpen.value = false
  detailMaterial.value = null
  detailEvents.value = []
  detailContent.value = emptyDetailContent()
  loadingDetail.value = false
}

function askRejectReason(title, onConfirm) {
  let reason = ''
  Modal.confirm({
    title,
    content: h('div', { class: 'reject-reason' }, [
      h('label', { for: 'reject-reason-input' }, '驳回原因'),
      h(Input.TextArea, {
        id: 'reject-reason-input',
        placeholder: '请说明需要修改的内容',
        maxlength: 500,
        'onUpdate:value': (value) => (reason = value)
      })
    ]),
    okText: '确认驳回',
    okButtonProps: { danger: true },
    cancelText: '取消',
    onOk: () => {
      if (!reason.trim()) {
        message.warning('请输入驳回原因')
        return Promise.reject(new Error('驳回原因不能为空'))
      }
      return onConfirm(reason.trim())
    }
  })
}

function confirmAction(title, content, onConfirm, danger = false) {
  Modal.confirm({
    title,
    content,
    okText: '确认',
    cancelText: '取消',
    okButtonProps: danger ? { danger: true } : {},
    onOk: onConfirm
  })
}

async function executeSingle(action, material, reason) {
  try {
    if (action === 'approve') await feishuKnowledgeApi.approveMaterial(material.version_id)
    if (action === 'reject') await feishuKnowledgeApi.rejectMaterial(material.version_id, reason)
    if (action === 'retry') await feishuKnowledgeApi.retryMaterial(material.version_id)
    if (action === 'confirm_removal') await feishuKnowledgeApi.confirmRemoval(material.version_id)
    message.success(actionSuccessLabel(action))
    await Promise.all([loadSources(), loadMaterials()])
  } catch (error) {
    message.error(feishuKnowledgeApi.getErrorMessage(error, '素材操作失败'))
    throw error
  }
}

function handleMaterialAction({ action, material }) {
  if (action === 'reject') {
    askRejectReason(`驳回“${material.title || '未命名素材'}”？`, (reason) =>
      executeSingle(action, material, reason)
    )
    return
  }
  const titles = {
    approve: `确认审核通过“${material.title || '未命名素材'}”？`,
    retry: `确认重试“${material.title || '未命名素材'}”？`,
    confirm_removal: `确认下架“${material.title || '未命名素材'}”？`
  }
  const descriptions = {
    approve: '通过后将进入正式知识库发布流程。',
    retry: '系统将根据当前状态重新执行加工或发布。',
    confirm_removal: '确认后将从正式知识库移除该素材。'
  }
  confirmAction(
    titles[action],
    descriptions[action],
    () => executeSingle(action, material),
    action === 'confirm_removal'
  )
}

async function executeBatch(action, versionIds, reason) {
  try {
    const result = await feishuKnowledgeApi.batchAction(action, versionIds, reason)
    const failed = result.failed || 0
    if (failed) message.warning(`批量操作完成，${failed} 条处理失败`)
    else message.success(`已处理 ${result.succeeded ?? versionIds.length} 条素材`)
    materialTableRef.value?.clearSelection()
    await Promise.all([loadSources(), loadMaterials()])
  } catch (error) {
    message.error(feishuKnowledgeApi.getErrorMessage(error, '批量操作失败'))
    throw error
  }
}

function handleBatchAction({ action, versionIds }) {
  if (!versionIds.length) return
  if (versionIds.length > MAX_BATCH_SIZE) {
    message.warning(`单次批量操作最多选择 ${MAX_BATCH_SIZE} 条素材`)
    return
  }
  if (action === 'reject') {
    askRejectReason(`驳回所选 ${versionIds.length} 条素材？`, (reason) =>
      executeBatch(action, versionIds, reason)
    )
    return
  }
  const labels = { approve: '审核通过', retry: '重试', confirm_removal: '确认下架' }
  confirmAction(
    `确认${labels[action]}所选 ${versionIds.length} 条素材？`,
    action === 'confirm_removal' ? '这些素材将从正式知识库移除。' : '操作将应用到全部所选素材。',
    () => executeBatch(action, versionIds),
    action === 'confirm_removal'
  )
}

function actionSuccessLabel(action) {
  return {
    approve: '已审核通过，等待发布',
    reject: '已驳回素材',
    retry: '已提交重试',
    confirm_removal: '已确认下架'
  }[action]
}

function formatTime(value) {
  if (!value) return '暂无记录'
  return new Intl.DateTimeFormat('zh-CN', {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit'
  }).format(new Date(value))
}

onMounted(initialize)
onBeforeUnmount(() => {
  isAlive = false
  clearTimeout(pollTimer)
})
</script>

<style scoped lang="less">
.workbench-page {
  min-width: 0;
  min-height: 100vh;
  overflow-x: hidden;
  background: var(--gray-25);
  color: var(--color-text);
}

.page-heading {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 20px;
  min-height: 72px;
  padding: 12px var(--page-padding);
  background: color-mix(in srgb, var(--gray-0) 94%, transparent);
}

.page-heading h1,
.section-heading h2 {
  margin: 0;
  color: var(--color-text);
  font-weight: 600;
  letter-spacing: 0;
}

.page-heading h1 {
  font-size: 22px;
}

.page-heading p,
.section-heading p {
  margin: 3px 0 0;
  color: var(--color-text-tertiary);
  font-size: 13px;
}

.heading-actions {
  display: flex;
  align-items: center;
  gap: 8px;
}

.heading-actions :deep(.ant-btn),
.material-heading :deep(.ant-btn) {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 6px;
}

.page-alert {
  margin: 12px var(--page-padding) 0;
}

.source-strip {
  display: grid;
  grid-template-columns: minmax(170px, 0.8fr) minmax(280px, 1.6fr) minmax(150px, 0.8fr) minmax(180px, 0.9fr);
  gap: 0;
  margin: 12px var(--page-padding) 0;
  overflow: hidden;
  border: 1px solid color-mix(in srgb, var(--gray-150) 45%, transparent);
  border-radius: 8px;
  background: var(--gray-0);
}

.source-identity,
.source-field {
  display: flex;
  min-width: 0;
  min-height: 78px;
  flex-direction: column;
  justify-content: center;
  padding: 13px 16px;
  border-right: 1px solid color-mix(in srgb, var(--gray-150) 42%, transparent);
}

.source-identity {
  flex-direction: row;
  align-items: center;
  justify-content: flex-start;
  gap: 10px;
}

.source-field:last-child {
  border-right: 0;
}

.source-icon {
  display: grid;
  width: 38px;
  height: 38px;
  flex-shrink: 0;
  place-items: center;
  border-radius: 7px;
  background: var(--main-30);
  color: var(--main-color);
}

.source-strip label {
  display: block;
  margin-bottom: 4px;
  color: var(--color-text-tertiary);
  font-size: 12px;
}

.source-strip strong,
.source-strip span,
.source-strip a {
  min-width: 0;
  overflow: hidden;
  color: var(--color-text);
  font-size: 13px;
  font-weight: 500;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.source-strip a {
  color: var(--main-color);
}

.source-field span + span {
  margin-top: 2px;
  color: var(--color-text-tertiary);
  font-size: 12px;
  font-weight: 400;
}

.stats-row {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  margin: 10px var(--page-padding) 0;
  overflow: hidden;
  border: 1px solid color-mix(in srgb, var(--gray-150) 40%, transparent);
  border-radius: 8px;
  background: var(--gray-0);
}

.stat-item {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  min-height: 56px;
  padding: 12px 16px;
  border-right: 1px solid color-mix(in srgb, var(--gray-150) 40%, transparent);
}

.stat-item:last-child {
  border-right: 0;
}

.stat-item span {
  color: var(--color-text-secondary);
  font-size: 13px;
}

.stat-item strong {
  color: var(--main-700);
  font-size: 22px;
  font-weight: 600;
}

.stat-item strong.warning { color: var(--color-warning-900); }
.stat-item strong.error { color: var(--color-error-700); }
.stat-item strong.muted { color: var(--gray-600); }

.workspace-section {
  margin: 10px var(--page-padding) 0;
  padding: 14px 16px 4px;
  border: 1px solid color-mix(in srgb, var(--gray-150) 40%, transparent);
  border-radius: 8px;
  background: var(--gray-0);
}

.material-section {
  margin-bottom: 24px;
  padding-bottom: 14px;
}

.section-heading {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  margin-bottom: 12px;
}

.section-heading h2 {
  font-size: 16px;
}

.clear-filter {
  padding: 4px 0;
  border: 0;
  background: transparent;
  color: var(--main-color);
  font: inherit;
  font-size: 13px;
  cursor: pointer;
}

.filter-bar {
  display: grid;
  grid-template-columns: repeat(4, minmax(112px, 0.7fr)) minmax(160px, 1fr) minmax(260px, 1.4fr) auto auto;
  gap: 8px;
  margin-bottom: 12px;
  padding: 10px;
  border-radius: 6px;
  background: var(--gray-25);
}

.empty-source {
  padding-top: 20vh;
}

:deep(.ant-table-wrapper .ant-table-thead > tr > th) {
  border-bottom-color: color-mix(in srgb, var(--gray-150) 45%, transparent);
  background: var(--gray-25);
  color: var(--color-text-secondary);
  font-size: 12px;
  font-weight: 500;
}

:deep(.ant-table-wrapper .ant-table-tbody > tr > td) {
  border-bottom-color: color-mix(in srgb, var(--gray-150) 34%, transparent);
}

:global(.reject-reason) {
  display: grid;
  gap: 8px;
  padding-top: 8px;
}

@media (max-width: 1180px) {
  .source-strip {
    grid-template-columns: 1fr 1.5fr;
  }

  .source-field:nth-child(2) {
    border-right: 0;
  }

  .source-identity,
  .source-field:nth-child(2) {
    border-bottom: 1px solid color-mix(in srgb, var(--gray-150) 42%, transparent);
  }

  .filter-bar {
    grid-template-columns: repeat(4, minmax(110px, 1fr));
  }
}

@media (max-width: 760px) {
  .page-heading {
    align-items: flex-start;
    flex-direction: column;
    padding-top: 16px;
    padding-bottom: 14px;
  }

  .heading-actions {
    width: 100%;
    overflow-x: auto;
  }

  .heading-actions :deep(.ant-btn) {
    min-height: 40px;
    flex-shrink: 0;
  }

  .source-strip {
    grid-template-columns: 1fr;
  }

  .source-identity,
  .source-field {
    min-height: 66px;
    border-right: 0;
    border-bottom: 1px solid color-mix(in srgb, var(--gray-150) 42%, transparent);
  }

  .source-field:last-child {
    border-bottom: 0;
  }

  .stats-row {
    grid-template-columns: 1fr 1fr;
  }

  .stat-item:nth-child(2) {
    border-right: 0;
  }

  .stat-item:nth-child(-n + 2) {
    border-bottom: 1px solid color-mix(in srgb, var(--gray-150) 40%, transparent);
  }

  .filter-bar {
    grid-template-columns: 1fr;
  }

  .workspace-section {
    padding-inline: 10px;
  }
}
</style>
