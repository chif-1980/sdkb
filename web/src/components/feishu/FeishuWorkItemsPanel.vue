<template>
  <section class="work-items-panel" aria-label="运营待办">
    <header class="work-items-heading">
      <div>
        <h2>运营待办</h2>
        <p>聚合审核、来源更新、冲突、加工失败、到期复核和用户反馈</p>
      </div>
      <a-button :loading="loading" aria-label="刷新运营待办" @click="loadAll">
        <RefreshCw :size="15" />
      </a-button>
    </header>

    <div class="work-summary" aria-label="待办摘要">
      <span
        ><b>{{ summary.total || 0 }}</b> 全部待办</span
      >
      <span class="is-danger"
        ><b>{{ summary.overdue || 0 }}</b> 已超期</span
      >
      <span class="is-warning"
        ><b>{{ summary.byRisk?.HIGH || 0 }}</b> 高风险</span
      >
      <span
        ><b>{{ summary.unassigned || 0 }}</b> 公共待办</span
      >
    </div>

    <div class="work-filters" aria-label="待办筛选">
      <a-select v-model:value="filters.assignee" :options="assigneeOptions" aria-label="负责人" />
      <a-select v-model:value="filters.type" :options="typeOptions" aria-label="待办类型" />
      <a-select v-model:value="filters.risk" :options="riskOptions" aria-label="风险" />
      <a-select v-model:value="filters.status" :options="statusOptions" aria-label="状态" />
      <a-checkbox v-model:checked="filters.overdue">仅看超期</a-checkbox>
    </div>

    <a-alert v-if="error" type="error" show-icon :message="error" />
    <a-spin :spinning="loading">
      <div class="work-table-scroll">
        <table v-if="items.length" class="work-table">
          <thead>
            <tr>
              <th>待办与来源</th>
              <th>AI 摘要</th>
              <th>质量 / 风险</th>
              <th>负责人</th>
              <th>状态</th>
              <th><span class="sr-only">操作</span></th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="item in items" :key="item.id" :class="{ overdue: item.overdue }">
              <td>
                <button type="button" class="work-title" @click="emit('navigate', item.navigation)">
                  <span class="work-type">{{ typeLabel(item.type) }}</span>
                  <strong>{{ item.title }}</strong>
                </button>
                <small :title="item.source?.path || ''">{{
                  item.source?.path || '未记录来源路径'
                }}</small>
              </td>
              <td>
                <p>{{ item.aiSummary }}</p>
                <span class="suggestion">建议：{{ item.suggestedAction }}</span>
                <span v-if="item.blockReasons?.length" class="block-reason">
                  <CircleAlert :size="13" /> {{ item.blockReasons.join('；') }}
                </span>
              </td>
              <td>
                <div
                  v-if="item.qualityScore !== null && item.qualityScore !== undefined"
                  class="quality-meter"
                >
                  <span>{{ item.qualityScore }}</span>
                  <i><b :style="{ width: `${item.qualityScore}%` }" /></i>
                </div>
                <span class="risk-label" :class="`risk-${String(item.risk).toLowerCase()}`">
                  {{ riskLabel(item.risk) }}
                </span>
              </td>
              <td>{{ item.assigneeId || '管理员公共待办' }}</td>
              <td>
                <span>{{ statusLabel(item.status) }}</span>
                <small v-if="item.overdue" class="overdue-label">已超期</small>
                <small v-else-if="item.dueAt">截止 {{ formatTime(item.dueAt) }}</small>
              </td>
              <td>
                <a-button
                  size="small"
                  aria-label="打开待办"
                  @click="emit('navigate', item.navigation)"
                >
                  <ArrowRight :size="14" />
                </a-button>
              </td>
            </tr>
          </tbody>
        </table>
        <a-empty v-else-if="!loading" description="当前筛选条件下没有运营待办" />
      </div>
    </a-spin>
  </section>
</template>

<script setup>
import { onMounted, reactive, ref, watch } from 'vue'
import { ArrowRight, CircleAlert, RefreshCw } from 'lucide-vue-next'

import { governanceApi } from '@/apis/governance_api'

const props = defineProps({ sourceId: { type: String, default: '' } })
const emit = defineEmits(['navigate', 'count-change'])

const items = ref([])
const summary = ref({ total: 0, overdue: 0, unassigned: 0, byRisk: {}, byType: {} })
const loading = ref(false)
const error = ref('')
const filters = reactive({ assignee: 'mine', type: '', risk: '', status: '', overdue: false })

const assigneeOptions = [
  { label: '我的及公共待办', value: 'mine' },
  { label: '仅公共待办', value: 'unassigned' },
  { label: '全部负责人', value: '' }
]
const typeOptions = [
  { label: '全部类型', value: '' },
  { label: '待审核', value: 'REVIEW' },
  { label: '来源更新', value: 'SOURCE_CHANGE' },
  { label: '用户反馈', value: 'USER_FEEDBACK' },
  { label: '冲突处理', value: 'CONFLICT' },
  { label: '加工失败', value: 'PROCESSING_FAILURE' },
  { label: '到期复核', value: 'EXPIRY_REVIEW' }
]
const riskOptions = [
  { label: '全部风险', value: '' },
  { label: '高风险', value: 'HIGH' },
  { label: '中风险', value: 'MEDIUM' },
  { label: '低风险', value: 'LOW' }
]
const statusOptions = [
  { label: '全部状态', value: '' },
  { label: '待处理', value: 'OPEN' },
  { label: '等待来源更新', value: 'WAITING_SOURCE_CHANGE' },
  { label: '自动重试中', value: 'RETRYING' },
  { label: '重试已耗尽', value: 'RETRY_EXHAUSTED' },
  { label: '已超期', value: 'OVERDUE' }
]

async function loadAll() {
  if (!props.sourceId) return
  loading.value = true
  error.value = ''
  const params = {
    source_id: props.sourceId,
    assignee: filters.assignee,
    type: filters.type,
    risk: filters.risk,
    status: filters.status,
    overdue: filters.overdue || undefined,
    page_size: 100
  }
  try {
    const [listResponse, summaryResponse] = await Promise.all([
      governanceApi.listWorkItems(params),
      governanceApi.getWorkItemSummary({ source_id: props.sourceId, assignee: filters.assignee })
    ])
    items.value = listResponse.items || []
    summary.value = summaryResponse
    emit('count-change', summaryResponse.total || 0)
  } catch (requestError) {
    error.value = governanceApi.getErrorMessage(requestError, '运营待办加载失败，请重试')
  } finally {
    loading.value = false
  }
}

function typeLabel(type) {
  return (
    {
      REVIEW: '待审核',
      SOURCE_CHANGE: '来源更新',
      USER_FEEDBACK: '用户反馈',
      CONFLICT: '冲突处理',
      PROCESSING_FAILURE: '加工失败',
      EXPIRY_REVIEW: '到期复核'
    }[type] || type
  )
}

function riskLabel(risk) {
  return { HIGH: '高风险', MEDIUM: '中风险', LOW: '低风险' }[risk] || risk
}

function statusLabel(status) {
  return (
    {
      OPEN: '待处理',
      WAITING_SOURCE_CHANGE: '等待来源更新',
      WAITING_BUSINESS_CONFIRMATION: '等待业务确认',
      RETRYING: '自动重试中',
      RETRY_EXHAUSTED: '重试已耗尽',
      DUE_SOON: '即将到期',
      OVERDUE: '已超期'
    }[status] || status
  )
}

function formatTime(value) {
  return value ? new Date(value).toLocaleDateString('zh-CN') : '-'
}

watch(() => props.sourceId, loadAll)
watch(filters, loadAll)
onMounted(loadAll)
</script>

<style scoped lang="less">
.work-items-panel {
  min-height: 420px;
  border-top: 1px solid var(--border-color, #e5e7eb);
  background: #fff;
}
.work-items-heading {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  padding: 18px 20px 12px;
}
.work-items-heading h2 {
  margin: 0;
  color: var(--text-color, #1f2937);
  font-size: 17px;
}
.work-items-heading p {
  margin: 4px 0 0;
  color: var(--text-color-secondary, #6b7280);
  font-size: 12px;
}
.work-summary {
  display: flex;
  gap: 24px;
  padding: 10px 20px;
  border-block: 1px solid var(--border-color, #e5e7eb);
  background: #f8fafc;
}
.work-summary span {
  color: #64748b;
  font-size: 12px;
}
.work-summary b {
  margin-right: 4px;
  color: #1e293b;
  font-size: 16px;
}
.work-summary .is-danger b {
  color: #b42318;
}
.work-summary .is-warning b {
  color: #b45309;
}
.work-filters {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 12px 20px;
}
.work-filters :deep(.ant-select) {
  width: 150px;
}
.work-table-scroll {
  overflow-x: auto;
  padding: 0 20px 20px;
}
.work-table {
  width: 100%;
  min-width: 960px;
  border-collapse: collapse;
  table-layout: fixed;
}
.work-table th {
  padding: 9px 10px;
  border-bottom: 1px solid #dfe5ec;
  color: #64748b;
  background: #f8fafc;
  font-size: 12px;
  font-weight: 600;
  text-align: left;
}
.work-table td {
  padding: 11px 10px;
  border-bottom: 1px solid #edf0f4;
  color: #334155;
  font-size: 12px;
  vertical-align: top;
}
.work-table th:nth-child(1) {
  width: 24%;
}
.work-table th:nth-child(2) {
  width: 36%;
}
.work-table th:nth-child(3) {
  width: 12%;
}
.work-table th:nth-child(4) {
  width: 12%;
}
.work-table th:nth-child(5) {
  width: 12%;
}
.work-table th:nth-child(6) {
  width: 44px;
}
.work-table tr.overdue {
  background: #fffafa;
}
.work-title {
  display: flex;
  align-items: center;
  gap: 7px;
  max-width: 100%;
  padding: 0;
  border: 0;
  color: #1f2937;
  background: transparent;
  text-align: left;
  cursor: pointer;
}
.work-title strong {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.work-type {
  flex: none;
  padding: 2px 5px;
  border-radius: 3px;
  color: #315fba;
  background: #eaf2ff;
  font-size: 10px;
}
.work-table small {
  display: block;
  overflow: hidden;
  margin-top: 5px;
  color: #8491a3;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.work-table p {
  display: -webkit-box;
  overflow: hidden;
  margin: 0;
  -webkit-box-orient: vertical;
  -webkit-line-clamp: 2;
}
.suggestion {
  display: block;
  margin-top: 5px;
  color: #49647f;
}
.block-reason {
  display: flex;
  align-items: center;
  gap: 4px;
  margin-top: 5px;
  color: #b42318;
}
.quality-meter {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-bottom: 6px;
}
.quality-meter span {
  width: 24px;
  color: #1d4ed8;
  font-size: 15px;
  font-weight: 700;
}
.quality-meter i {
  display: block;
  width: 48px;
  height: 5px;
  overflow: hidden;
  border-radius: 2px;
  background: #e2e8f0;
}
.quality-meter b {
  display: block;
  height: 100%;
  background: #2f6fd5;
}
.risk-label {
  display: inline-block;
  padding: 2px 5px;
  border-radius: 3px;
}
.risk-high {
  color: #b42318;
  background: #fff0ef;
}
.risk-medium {
  color: #9a5a08;
  background: #fff7df;
}
.risk-low {
  color: #247548;
  background: #ecf8f1;
}
.overdue-label {
  color: #b42318 !important;
}
.sr-only {
  position: absolute;
  width: 1px;
  height: 1px;
  overflow: hidden;
  clip: rect(0, 0, 0, 0);
}
@media (max-width: 760px) {
  .work-summary {
    gap: 12px;
    overflow-x: auto;
  }
  .work-filters {
    flex-wrap: wrap;
  }
  .work-filters :deep(.ant-select) {
    width: calc(50% - 4px);
  }
}
</style>
