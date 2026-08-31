<template>
  <div>
    <div v-if="selectedRowKeys.length" class="batch-bar">
      <span>已选 {{ selectedRowKeys.length }} 条</span>
      <div class="batch-actions">
        <a-button size="small" :disabled="!canBatch('approve')" @click="emitBatch('approve')">
          审核通过
        </a-button>
        <a-button size="small" :disabled="!canBatch('reject')" @click="emitBatch('reject')">
          驳回
        </a-button>
        <a-button size="small" :disabled="!canBatch('retry')" @click="emitBatch('retry')">
          重试
        </a-button>
        <a-button size="small" :disabled="!canBatch('reindex')" @click="emitBatch('reindex')">
          重新解析并重建索引
        </a-button>
        <a-button
          size="small"
          danger
          :disabled="!canBatch('confirm_removal')"
          @click="emitBatch('confirm_removal')"
        >
          确认下架
        </a-button>
      </div>
    </div>
    <div class="table-scroll">
      <a-table
        :columns="columns"
        :data-source="materials"
        :loading="loading"
        :pagination="{ pageSize: 12, showSizeChanger: false }"
        :row-selection="rowSelection"
        row-key="version_id"
        size="small"
        :scroll="{ x: 1260 }"
      >
        <template #emptyText>当前筛选条件下暂无素材</template>
        <template #bodyCell="{ column, record }">
          <template v-if="column.key === 'title'">
            <button class="title-button" type="button" @click="emit('open-detail', record)">
              <span>{{ record.title || '未命名素材' }}</span>
              <span class="path-text">{{ record.wiki_path || '-' }}</span>
            </button>
          </template>
          <template v-else-if="column.key === 'item_type'">
            <span>{{ itemTypeLabel(record.item_type) }}</span>
            <a-tag v-if="isDirectory(record)" color="blue">仅目录，不加工</a-tag>
            <a-tag v-else-if="isUnsupported(record.item_type)" color="default">暂不支持加工</a-tag>
          </template>
          <template v-else-if="column.key === 'processing_status'">
            <a-tag :color="processingStatus(record.processing_status).color">
              {{ processingStatus(record.processing_status).label }}
            </a-tag>
            <a-tooltip v-if="record.error_message" :title="record.error_message">
              <div class="error-text">{{ record.error_message }}</div>
            </a-tooltip>
          </template>
          <template v-else-if="column.key === 'review_status'">
            <a-tag :color="reviewStatus(record.review_status).color">
              {{ reviewStatus(record.review_status).label }}
            </a-tag>
            <a-tag v-if="record.is_directory" color="blue">无需审核</a-tag>
            <a-tag v-else-if="record.content_missing" color="error">正文缺失</a-tag>
            <a-tag
              v-else-if="
                record.content_check_pending &&
                ['parsed', 'awaiting_review'].includes(record.processing_status)
              "
              color="warning"
              >正文待确认</a-tag
            >
          </template>
          <template v-else-if="column.key === 'source_validity'">
            <a-tag :color="record.source_validity === 'valid' ? 'success' : 'warning'">
              {{ record.source_validity === 'valid' ? '有效' : '来源失效' }}
            </a-tag>
          </template>
          <template v-else-if="column.key === 'updated_at'">
            <span>{{ formatTime(record.source_updated_at || record.updated_at) }}</span>
          </template>
          <template v-else-if="column.key === 'action'">
            <div class="row-actions">
              <a-button type="link" size="small" @click="emit('open-detail', record)"
                >详情</a-button
              >
              <a-dropdown :trigger="['click']">
                <a-button type="text" size="small" aria-label="素材操作">
                  <MoreHorizontal :size="17" />
                </a-button>
                <template #overlay>
                  <a-menu @click="({ key }) => emitAction(record, key)">
                    <a-menu-item key="approve" :disabled="!canPerformAction(record, 'approve')">
                      审核通过
                    </a-menu-item>
                    <a-menu-item key="reject" :disabled="!canPerformAction(record, 'reject')">
                      驳回
                    </a-menu-item>
                    <a-menu-item key="retry" :disabled="!canPerformAction(record, 'retry')">
                      重试
                    </a-menu-item>
                    <a-menu-item key="reindex" :disabled="!canPerformAction(record, 'reindex')">
                      重新解析并重建索引
                    </a-menu-item>
                    <a-menu-item
                      key="confirm_removal"
                      danger
                      :disabled="!canPerformAction(record, 'confirm_removal')"
                    >
                      确认下架
                    </a-menu-item>
                  </a-menu>
                </template>
              </a-dropdown>
            </div>
          </template>
        </template>
      </a-table>
    </div>
  </div>
</template>

<script setup>
import { computed, ref, watch } from 'vue'
import { MoreHorizontal } from 'lucide-vue-next'

const props = defineProps({
  materials: { type: Array, default: () => [] },
  loading: { type: Boolean, default: false },
  maxSelection: { type: Number, default: 100 }
})

const emit = defineEmits(['open-detail', 'action', 'batch-action', 'selection-limit'])
const selectedRowKeys = ref([])
const selectedMaterials = computed(() => {
  const selectedIds = new Set(selectedRowKeys.value)
  return props.materials.filter((item) => selectedIds.has(item.version_id))
})

const columns = [
  { title: '素材', key: 'title', width: 270 },
  { title: '类型', key: 'item_type', width: 140 },
  { title: '加工状态', key: 'processing_status', width: 145 },
  { title: '审核状态', key: 'review_status', width: 110 },
  { title: '来源状态', key: 'source_validity', width: 110 },
  { title: '分块', dataIndex: 'chunk_count', key: 'chunk_count', width: 75 },
  { title: '更新时间', key: 'updated_at', width: 165 },
  { title: '操作', key: 'action', fixed: 'right', width: 115 }
]

const rowSelection = computed(() => ({
  preserveSelectedRowKeys: true,
  selectedRowKeys: selectedRowKeys.value,
  onChange: (nextKeys) => {
    if (nextKeys.length > props.maxSelection) {
      emit('selection-limit', props.maxSelection)
      selectedRowKeys.value = nextKeys.slice(0, props.maxSelection)
      return
    }
    selectedRowKeys.value = nextKeys
  },
  getCheckboxProps: (record) => ({
    disabled: !record.version_id || isDirectory(record) || isUnsupported(record.item_type)
  })
}))

watch(
  () => props.materials,
  () => {
    const visibleIds = new Set(props.materials.map((item) => item.version_id))
    selectedRowKeys.value = selectedRowKeys.value.filter((id) => visibleIds.has(id))
  }
)

const itemTypes = {
  page: '飞书页面',
  directory: '目录节点',
  attachment: '附件',
  doc: '文档',
  docx: '文档',
  sheet: '表格',
  bitable: '多维表格',
  mindnote: '思维导图',
  file: '文件',
  audio: '音频',
  video: '视频'
}

const processingStatuses = {
  discovered: { label: '已发现', color: 'default' },
  syncing: { label: '归档中', color: 'processing' },
  synced: { label: '已归档', color: 'default' },
  processing_queued: { label: '等待加工', color: 'processing' },
  processing: { label: '加工中', color: 'processing' },
  awaiting_review: { label: '待审核', color: 'warning' },
  skipped: { label: '已跳过', color: 'default' },
  parse_failed: { label: '解析失败', color: 'error' },
  publish_queued: { label: '等待发布', color: 'processing' },
  publishing: { label: '发布中', color: 'processing' },
  published: { label: '已发布', color: 'success' },
  publish_failed: { label: '发布失败', color: 'error' },
  replaced: { label: '已替换', color: 'default' },
  removed: { label: '已下架', color: 'default' },
  unsupported: { label: '不支持', color: 'default' },
  removal_pending: { label: '待下架', color: 'warning' },
  removal_failed: { label: '下架失败', color: 'error' }
}

const reviewStatuses = {
  pending: { label: '待审核', color: 'warning' },
  approved: { label: '已通过', color: 'success' },
  rejected: { label: '已驳回', color: 'error' },
  not_required: { label: '无需审核', color: 'default' }
}

function itemTypeLabel(value) {
  return itemTypes[value] || value || '-'
}

function isUnsupported(value) {
  return ['audio', 'video', 'unsupported'].includes(value)
}

function isDirectory(material) {
  return material?.is_directory === true || material?.item_type === 'directory'
}

function canPerformAction(material, action) {
  const parsedReady =
    material.review_status === 'pending' &&
    ['parsed', 'awaiting_review'].includes(material.processing_status) &&
    Boolean(material.yuxi_file_id) &&
    material.content_quality?.checked === true &&
    material.content_quality?.has_body === true

  if (action === 'approve') return parsedReady
  if (action === 'reject') return material.review_status === 'pending'
  if (action === 'retry') {
    return ['parse_failed', 'publish_failed'].includes(material.processing_status)
  }
  if (action === 'reindex') {
    return (
      material.processing_status === 'published' &&
      material.review_status === 'approved' &&
      material.source_validity === 'valid' &&
      material.publication_status === 'ACTIVE' &&
      material.active === true &&
      Boolean(material.source_object_path) &&
      Boolean(material.yuxi_file_id)
    )
  }
  if (action === 'confirm_removal') {
    return (
      material.source_validity === 'invalid' &&
      material.active === true &&
      ['published', 'removal_failed'].includes(material.processing_status) &&
      Boolean(material.yuxi_file_id)
    )
  }
  return false
}

function canBatch(action) {
  return (
    selectedMaterials.value.length > 0 &&
    selectedMaterials.value.length === selectedRowKeys.value.length &&
    selectedMaterials.value.every((material) => canPerformAction(material, action))
  )
}

function processingStatus(value) {
  return processingStatuses[value] || { label: value || '未知', color: 'default' }
}

function reviewStatus(value) {
  return reviewStatuses[value] || { label: value || '未知', color: 'default' }
}

function formatTime(value) {
  if (!value) return '-'
  return new Intl.DateTimeFormat('zh-CN', {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit'
  }).format(new Date(value))
}

function emitBatch(action) {
  if (!canBatch(action)) return
  emit('batch-action', { action, versionIds: [...selectedRowKeys.value] })
}

function emitAction(material, action) {
  if (!canPerformAction(material, action)) return
  emit('action', { action, material })
}

defineExpose({ clearSelection: () => (selectedRowKeys.value = []) })
</script>

<style scoped lang="less">
.batch-bar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  min-height: 44px;
  margin-bottom: 8px;
  padding: 6px 10px;
  border-radius: 6px;
  background: var(--main-30);
  color: var(--main-700);
  font-size: 13px;
  font-weight: 500;
}

.batch-actions,
.row-actions {
  display: flex;
  align-items: center;
  gap: 4px;
}

.table-scroll {
  overflow-x: auto;
}

.title-button {
  display: flex;
  max-width: 100%;
  flex-direction: column;
  align-items: flex-start;
  gap: 2px;
  padding: 0;
  border: 0;
  background: transparent;
  color: var(--color-text);
  font: inherit;
  font-weight: 500;
  text-align: left;
  cursor: pointer;
}

.title-button:hover,
.title-button:focus-visible {
  color: var(--main-color);
}

.path-text,
.error-text {
  display: block;
  max-width: 240px;
  overflow: hidden;
  color: var(--color-text-tertiary);
  font-size: 12px;
  font-weight: 400;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.error-text {
  margin-top: 3px;
  color: var(--color-error-700);
}

@media (max-width: 720px) {
  .batch-bar {
    align-items: flex-start;
    flex-direction: column;
  }

  .batch-actions {
    width: 100%;
    overflow-x: auto;
  }
}
</style>
