<template>
  <a-drawer
    :open="open"
    title="素材详情"
    placement="right"
    :width="drawerWidth"
    @close="emit('close')"
  >
    <a-skeleton v-if="loading" active :paragraph="{ rows: 10 }" />
    <template v-else-if="material">
      <div class="detail-heading">
        <div>
          <h2>{{ material.title || '未命名素材' }}</h2>
          <p>{{ material.wiki_path || '未记录飞书目录' }}</p>
        </div>
        <a
          v-if="material.source_url"
          class="source-link"
          :href="material.source_url"
          target="_blank"
          rel="noopener noreferrer"
        >
          查看飞书原文
          <ExternalLink :size="15" />
        </a>
      </div>

      <a-descriptions bordered size="small" :column="1">
        <a-descriptions-item label="来源对象">{{ material.item_id || '-' }}</a-descriptions-item>
        <a-descriptions-item label="版本">{{ material.revision || '-' }}</a-descriptions-item>
        <a-descriptions-item label="内容哈希">{{ material.content_hash || '-' }}</a-descriptions-item>
        <a-descriptions-item label="知识库文件">{{ material.yuxi_file_id || '尚未发布' }}</a-descriptions-item>
        <a-descriptions-item label="分块 / Token">
          {{ material.chunk_count ?? 0 }} / {{ material.token_count ?? 0 }}
        </a-descriptions-item>
      </a-descriptions>

      <a-tabs class="detail-tabs" default-active-key="markdown">
        <a-tab-pane key="markdown" tab="Markdown">
          <div class="data-panel">
            <div class="field-label">解析对象路径</div>
            <code>{{ material.parsed_object_path || '尚未生成解析对象' }}</code>
            <a-alert
              class="availability-note"
              type="info"
              show-icon
              message="当前接口暂未提供 Markdown 正文读取能力"
              description="此处仅展示解析产物的位置，不拼接或推测正文内容。"
            />
          </div>
        </a-tab-pane>
        <a-tab-pane key="chunks" tab="Chunks">
          <div class="data-panel">
            <div class="metric-line">
              <span>分块数量</span>
              <strong>{{ material.chunk_count ?? 0 }}</strong>
            </div>
            <div class="field-label">加工参数</div>
            <pre>{{ processingParams }}</pre>
            <a-alert
              class="availability-note"
              type="info"
              show-icon
              message="当前接口暂未提供逐块内容读取能力"
              description="分块正文将在后端提供读取接口后展示。"
            />
          </div>
        </a-tab-pane>
        <a-tab-pane key="source" tab="来源">
          <div class="data-panel source-fields">
            <div>
              <span>原始归档</span>
              <code>{{ material.source_object_path || '-' }}</code>
            </div>
            <div>
              <span>飞书更新时间</span>
              <strong>{{ formatTime(material.source_updated_at) }}</strong>
            </div>
            <div>
              <span>同步批次</span>
              <strong>{{ material.sync_run_id || '-' }}</strong>
            </div>
            <div>
              <span>来源有效性</span>
              <strong>{{ material.source_validity === 'valid' ? '有效' : '来源失效' }}</strong>
            </div>
          </div>
        </a-tab-pane>
        <a-tab-pane key="events" tab="事件时间线">
          <a-timeline v-if="events.length" class="event-timeline">
            <a-timeline-item v-for="event in events" :key="event.id">
              <div class="event-title">{{ eventLabel(event.event_type) }}</div>
              <div v-if="event.from_status || event.to_status" class="event-status">
                {{ event.from_status || '-' }} → {{ event.to_status || '-' }}
              </div>
              <p v-if="event.message">{{ event.message }}</p>
              <time>{{ formatTime(event.created_at) }} · {{ event.operator_id || '系统' }}</time>
            </a-timeline-item>
          </a-timeline>
          <a-empty v-else description="暂无加工事件" />
        </a-tab-pane>
      </a-tabs>
    </template>
  </a-drawer>
</template>

<script setup>
import { computed } from 'vue'
import { useWindowSize } from '@vueuse/core'
import { ExternalLink } from 'lucide-vue-next'

const props = defineProps({
  open: { type: Boolean, default: false },
  material: { type: Object, default: null },
  events: { type: Array, default: () => [] },
  loading: { type: Boolean, default: false }
})

const emit = defineEmits(['close'])
const { width } = useWindowSize()
const drawerWidth = computed(() => (width.value < 720 ? '100%' : 640))
const processingParams = computed(() =>
  props.material?.processing_params
    ? JSON.stringify(props.material.processing_params, null, 2)
    : '未记录加工参数'
)

const eventLabels = {
  discovered: '发现素材',
  archived: '完成归档',
  processing_started: '开始加工',
  processing_completed: '加工完成',
  processing_failed: '加工失败',
  approved: '审核通过',
  rejected: '审核驳回',
  published: '发布完成',
  removed: '确认下架'
}

function eventLabel(value) {
  return eventLabels[value] || value || '状态更新'
}

function formatTime(value) {
  if (!value) return '-'
  return new Intl.DateTimeFormat('zh-CN', {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit'
  }).format(new Date(value))
}
</script>

<style scoped lang="less">
.detail-heading {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
  margin-bottom: 20px;
}

.detail-heading h2 {
  margin: 0;
  color: var(--color-text);
  font-size: 18px;
  font-weight: 600;
}

.detail-heading p {
  margin: 4px 0 0;
  color: var(--color-text-tertiary);
  font-size: 13px;
}

.source-link {
  display: inline-flex;
  flex-shrink: 0;
  align-items: center;
  gap: 5px;
  color: var(--main-color);
}

.detail-tabs {
  margin-top: 18px;
}

.data-panel {
  min-height: 180px;
  padding: 4px 0;
}

.field-label,
.source-fields span {
  display: block;
  margin-bottom: 6px;
  color: var(--color-text-tertiary);
  font-size: 12px;
}

code,
pre {
  display: block;
  max-width: 100%;
  overflow: auto;
  padding: 10px 12px;
  border-radius: 6px;
  background: var(--gray-25);
  color: var(--color-text-secondary);
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 12px;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}

.availability-note {
  margin-top: 16px;
}

.metric-line {
  display: flex;
  justify-content: space-between;
  margin-bottom: 18px;
  padding: 10px 12px;
  border-radius: 6px;
  background: var(--main-30);
  color: var(--color-text-secondary);
}

.metric-line strong {
  color: var(--main-700);
  font-size: 18px;
}

.source-fields {
  display: grid;
  gap: 18px;
}

.source-fields strong {
  color: var(--color-text);
  font-weight: 500;
}

.event-timeline {
  padding: 8px 4px;
}

.event-title {
  color: var(--color-text);
  font-weight: 500;
}

.event-status,
.event-timeline time {
  color: var(--color-text-tertiary);
  font-size: 12px;
}

.event-timeline p {
  margin: 4px 0;
  color: var(--color-text-secondary);
}

@media (max-width: 560px) {
  .detail-heading {
    flex-direction: column;
  }
}
</style>
