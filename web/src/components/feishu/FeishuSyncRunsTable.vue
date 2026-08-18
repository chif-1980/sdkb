<template>
  <div class="table-scroll">
    <a-table
      :columns="columns"
      :data-source="runs"
      :loading="loading"
      :pagination="{ pageSize: 6, hideOnSinglePage: true }"
      :row-class-name="rowClassName"
      :custom-row="customRow"
      row-key="run_id"
      size="small"
      :scroll="{ x: 830 }"
    >
      <template #emptyText>暂无扫描批次</template>
      <template #bodyCell="{ column, record }">
        <template v-if="column.key === 'run_type'">
          <span class="primary-cell">{{ runTypeLabel(record.run_type) }}</span>
        </template>
        <template v-else-if="column.key === 'time'">
          <div>{{ formatTime(record.started_at) }}</div>
          <span class="muted">{{ record.finished_at ? `完成 ${formatTime(record.finished_at)}` : '尚未完成' }}</span>
        </template>
        <template v-else-if="column.key === 'counts'">
          <div class="count-line">
            <span>扫描 {{ record.scanned_count || 0 }}</span>
            <span>新增 {{ record.new_count || 0 }}</span>
            <span>变更 {{ record.changed_count || 0 }}</span>
            <span>跳过 {{ record.unchanged_count || 0 }}</span>
          </div>
          <div class="count-line muted">
            <span>失败 {{ record.failed_count || 0 }}</span>
            <span>失效 {{ record.invalidated_count || 0 }}</span>
            <span>不支持 {{ record.unsupported_count || 0 }}</span>
          </div>
        </template>
        <template v-else-if="column.key === 'status'">
          <a-tag :color="runStatus(record.status).color">{{ runStatus(record.status).label }}</a-tag>
          <div v-if="isActive(record.status)" class="progress-track" aria-label="扫描进行中">
            <span class="progress-bar"></span>
          </div>
          <a-tooltip v-if="record.error_summary" :title="record.error_summary">
            <div class="error-summary">{{ record.error_summary }}</div>
          </a-tooltip>
        </template>
      </template>
    </a-table>
  </div>
</template>

<script setup>
const props = defineProps({
  runs: { type: Array, default: () => [] },
  loading: { type: Boolean, default: false },
  selectedRunId: { type: String, default: '' }
})

const emit = defineEmits(['select'])

const columns = [
  { title: '类型', key: 'run_type', width: 84 },
  { title: '处理结果', key: 'counts', width: 300 },
  { title: '状态', key: 'status', width: 140 },
  { title: '操作者', dataIndex: 'operator_id', key: 'operator_id', width: 120, ellipsis: true },
  { title: '执行时间', key: 'time', width: 185 }
]

const runTypes = { full: '全量', incremental: '增量' }
const statusMap = {
  queued: { label: '等待中', color: 'processing' },
  running: { label: '扫描中', color: 'processing' },
  succeeded: { label: '已完成', color: 'success' },
  failed: { label: '失败', color: 'error' },
  cancelled: { label: '已取消', color: 'default' }
}

function runTypeLabel(value) {
  return runTypes[value] || value || '-'
}

function runStatus(value) {
  return statusMap[value] || { label: value || '未知', color: 'default' }
}

function isActive(status) {
  return status === 'queued' || status === 'running'
}

function formatTime(value) {
  if (!value) return '-'
  return new Intl.DateTimeFormat('zh-CN', {
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit'
  }).format(new Date(value))
}

function rowClassName(record) {
  return record.run_id === props.selectedRunId ? 'selected-run-row' : ''
}

function customRow(record) {
  return {
    onClick: () => emit('select', record.run_id)
  }
}
</script>

<style scoped lang="less">
.table-scroll {
  overflow-x: auto;
}

.primary-cell {
  color: var(--color-text);
  font-weight: 500;
}

.muted {
  color: var(--color-text-tertiary);
  font-size: 12px;
}

.count-line {
  display: flex;
  flex-wrap: wrap;
  gap: 4px 12px;
  line-height: 20px;
}

.progress-track {
  width: 88px;
  height: 3px;
  margin-top: 7px;
  overflow: hidden;
  border-radius: 2px;
  background: var(--main-30);
}

.progress-bar {
  display: block;
  width: 35%;
  height: 100%;
  background: var(--main-color);
  animation: progress 1.4s linear infinite;
}

.error-summary {
  max-width: 150px;
  margin-top: 4px;
  overflow: hidden;
  color: var(--color-error-700);
  font-size: 12px;
  text-overflow: ellipsis;
  white-space: nowrap;
}

:deep(.ant-table-tbody > tr) {
  cursor: pointer;
}

:deep(.ant-table-tbody > .selected-run-row > td) {
  background: var(--main-30) !important;
}

@keyframes progress {
  from { transform: translateX(-100%); }
  to { transform: translateX(300%); }
}

@media (prefers-reduced-motion: reduce) {
  .progress-bar { animation: none; }
}
</style>
