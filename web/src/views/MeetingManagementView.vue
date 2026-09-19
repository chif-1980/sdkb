<template>
  <main class="meeting-page">
    <header class="meeting-heading">
      <div><h1>会议管理</h1><p>集中查看会议结果，跟进待办与知识更新。</p></div>
      <a-button :loading="loading" @click="load"><RefreshCw :size="15" /> 刷新</a-button>
    </header>
    <a-alert v-if="counts.scope" :type="counts.scope === 'OWNED' ? 'warning' : 'info'" show-icon
      :message="counts.scope === 'TENANT' ? '当前范围：所在飞书企业的会议' : '当前范围：仅本人会议'"
      :description="counts.scope === 'OWNED' ? '当前管理账号尚未关联飞书企业，因此看不到助手中其他账号创建的会议。查看企业会议需要为管理账号配置对应企业的管理权限。' : '展示同一飞书企业成员在助手中创建的会议，重跑记录归入原会议。'" />
    <section class="meeting-metrics" aria-label="会议工作概览">
      <button v-for="metric in metrics" :key="metric.key" @click="selectMetric(metric)">
        <span>{{ metric.label }}</span><strong>{{ counts[metric.key] ?? '—' }}</strong><ArrowUpRight :size="16" />
      </button>
    </section>
    <a-tabs :active-key="tab" @change="changeTab">
      <a-tab-pane key="meetings" tab="会议列表" /><a-tab-pane key="TASK" tab="待办跟进" />
      <a-tab-pane key="KNOWLEDGE" tab="知识更新建议" />
    </a-tabs>
    <form class="meeting-filters" @submit.prevent="search">
      <a-input v-model:value="searchText" allow-clear :placeholder="tab === 'meetings' ? '搜索会议标题或纪要正文' : '搜索事项或会议标题'" aria-label="搜索会议" />
      <a-select v-model:value="filterState" :options="statusOptions" aria-label="筛选状态" />
      <a-checkbox v-if="tab === 'meetings'" v-model:checked="archived">已归档</a-checkbox>
      <a-button html-type="submit" type="primary">查询</a-button>
    </form>
    <a-alert v-if="error" type="error" show-icon :message="error" />
    <a-table :columns="columns" :data-source="rows" :loading="loading" :pagination="false" :row-key="rowKey" :scroll="{x: 920}">
      <template #bodyCell="{ column, record }">
        <template v-if="column.key === 'title'">
          <button class="meeting-title-link" @click="open(record)">{{ record.title || '会议纪要' }}</button>
          <small>{{ tab === 'meetings' ? `上传于 ${formatDate(record.createdAt)}` : record.meetingTitle }}</small>
        </template>
        <template v-else-if="column.key === 'state'">
          <a-tag :color="stateColor(record.state)">{{ label(record.state) }}</a-tag>
          <small v-if="record.state === 'failed' && record.successfulRunId">上次成功结果仍可查看</small>
        </template>
        <template v-else-if="column.key === 'ownerDisplayName'">{{ record.ownerDisplayName || '未提供' }}</template>
        <template v-else-if="column.key === 'progress'">{{ record.completedTasks }} / {{ record.taskCount }} 已完成<small>{{ record.pendingTasks }} 待确认</small></template>
        <template v-else-if="column.key === 'meetingDate'">{{ record.meetingDate || '未提供' }}</template>
        <template v-else-if="column.key === 'knowledge'">{{ record.pendingKnowledge }} 待核对</template>
        <template v-else-if="column.key === 'assignee'">{{ record.assignee?.displayName || '待分配' }}</template>
        <template v-else-if="column.key === 'review'">{{ label(record.reviewStatus || 'PENDING') }}<small>{{ label(record.status) }}</small></template>
        <template v-else-if="column.key === 'knowledgeState'">{{ label(record.status) }}<small>{{ label(record.comparisonStatus) }}</small></template>
        <template v-else-if="column.key === 'sync'">{{ label(record.delivery?.syncStatus || 'NOT_SENT') }}</template>
        <template v-else-if="column.key === 'dueDate'">{{ record.dueDate || '未设期限' }}</template>
        <template v-else-if="column.key === 'updatedAt'">{{ formatDate(record.updatedAt) }}</template>
      </template>
      <template #emptyText><a-empty :description="tab === 'meetings' ? '当前范围内暂无符合条件的会议' : '暂无待处理事项'" /></template>
    </a-table>
    <footer class="meeting-pagination"><span>共 {{ total }} 条</span><a-pagination :current="page" :total="total" :page-size="20" :show-size-changer="false" @change="goPage" /></footer>
  </main>
</template>
<script setup>
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ArrowUpRight, RefreshCw } from 'lucide-vue-next'
import { listMeetings, meetingMetrics, meetingQueue } from '@/apis/meetingManagement'
import { label, formatDate, stateColor } from '@/utils/meetingManagement'
const route = useRoute(), router = useRouter()
const rows = ref([]), total = ref(0), loading = ref(false), error = ref(''), counts = ref({})
const searchText = ref(route.query.q || ''), filterState = ref(route.query.state || ''), archived = ref(route.query.archived === 'true')
const tab = computed(() => ['TASK', 'KNOWLEDGE'].includes(route.query.tab) ? route.query.tab : 'meetings')
const page = computed(() => Math.max(1, Number(route.query.page) || 1))
const metrics = [
  {key: 'pending', label: '待确认事项', tab: 'TASK', state: 'PENDING'},
  {key: 'overdue', label: '已逾期事项', tab: 'TASK', state: 'OVERDUE'},
  {key: 'knowledge', label: '待核对知识建议', tab: 'KNOWLEDGE', state: 'PENDING_MAINTAINER'},
  {key: 'errors', label: '分析异常', tab: 'meetings', state: 'failed'}
]
const statusOptions = computed(() => [{value: '', label: '全部状态'}, ...(tab.value === 'meetings'
  ? ['pending','running','completed','failed','cancelled'] : tab.value === 'TASK'
    ? ['PENDING','CONFIRMED','IGNORED','SYNC_FAILED','OVERDUE']
    : ['PENDING_MAINTAINER','PROCESSING','NEW_SOURCE_DRAFT','REVIEW_REQUESTED','COVERED','DEFERRED','REJECTED']).map(value => ({value, label: label(value)}))])
const columns = computed(() => [
  {title: tab.value === 'meetings' ? '会议' : '事项 / 来源会议', key: 'title', width: 330},
        ...(tab.value === 'meetings' ? [
    {title: '上传人', key: 'ownerDisplayName', width: 120}, {title: '会议日期', key: 'meetingDate', width: 130}, {title: '分析状态', key: 'state', width: 150},
    {title: '待办进度', key: 'progress', width: 140}, {title: '知识建议', key: 'knowledge', width: 120}
  ] : tab.value === 'TASK' ? [
    {title: '负责人', key: 'assignee', width: 120}, {title: '确认 / 执行', key: 'review', width: 150}, {title: '期限', key: 'dueDate', width: 120}, {title: '飞书同步', key: 'sync', width: 110}
  ] : [{title: '处理 / 比对', key: 'knowledgeState', width: 220}]),
  {title: '最近更新', key: 'updatedAt', width: 165}
])
const rowKey = row => `${row.meetingId || ''}:${row.id}`
let request = 0
async function load() {
  const current = ++request
  loading.value = true; error.value = ''
  try {
    const params = {q: route.query.q || '', state: route.query.state || '', status: route.query.state || '', archived: route.query.archived === 'true', offset: (page.value - 1) * 20}
    const [data, stats] = await Promise.all([tab.value === 'meetings' ? listMeetings(params) : meetingQueue(tab.value, params), meetingMetrics()])
    if (current !== request) return
    rows.value = data.items; total.value = data.total; counts.value = stats
  } catch (e) { if(current === request) error.value = e.message }
  finally { if(current === request) loading.value = false }
}
function search() { router.replace({query: {tab: tab.value, q: searchText.value, state: filterState.value, archived: String(archived.value)}}) }
function changeTab(value) { router.replace({query: {tab: value}}) }
function selectMetric(metric) { router.replace({query: {tab: metric.tab, state: metric.state}}) }
function goPage(value) { router.replace({query: {...route.query, page: value}}) }
function open(row) { router.push({path: `/meeting-management/${row.meetingId || row.id}`, query: {back: route.fullPath, tab: tab.value === 'meetings' ? 'minutes' : tab.value}}) }
watch(() => route.fullPath, () => { searchText.value = route.query.q || ''; filterState.value = route.query.state || ''; archived.value = route.query.archived === 'true'; load() })
onMounted(load)
</script>
<style lang="less" src="@/assets/css/meeting-management.less"></style>
