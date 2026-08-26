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
      <span class="toolbar-summary">共 <strong>{{ filteredKnowledge.length }}</strong> 条知识单元</span>
    </div>
    <div v-if="groupedKnowledge.length" class="knowledge-groups">
      <article v-for="group in groupedKnowledge" :key="group.key" class="source-group">
        <header class="source-group-header">
          <div class="source-group-title">
            <span class="source-eyebrow">来源材料</span>
            <strong :title="group.title">{{ group.title }}</strong>
            <span v-if="group.path" class="source-path" :title="group.path">{{ group.path }}</span>
          </div>
          <div class="source-group-summary">
            <span><strong>{{ group.items.length }}</strong> 个知识单元</span>
            <span v-if="group.fragmentCount"><strong>{{ group.fragmentCount }}</strong> 个来源片段</span>
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
                <span class="knowledge-level" :class="{ legacy: record.knowledge_level !== 'UNIT' }">
                  {{ record.knowledge_level === 'UNIT' ? '知识单元' : '整份资料' }}
                </span>
                <span v-if="sourceLocatorLabel(record) !== sourceCountLabel(record)" class="locator-label">
                  {{ sourceLocatorLabel(record) }}
                </span>
              </div>
              <strong class="primary-cell" :title="record.title">{{ record.title }}</strong>
              <span class="sub-line">当前版本 {{ record.revision }} · {{ sourceCountLabel(record) }}</span>
            </div>
            <div class="knowledge-row-status">
              <a-tag color="processing">{{ record.source_role === 'PRIMARY' ? '通用知识' : '条件变体' }}</a-tag>
              <a-tag color="success">已索引</a-tag>
            </div>
            <span class="knowledge-action">查看详情 <ChevronRight :size="14" /></span>
          </article>
        </div>
      </article>
    </div>
    <div v-else class="knowledge-empty"><a-empty description="暂无已发布知识单元" /></div>
    <div class="table-footer">共 {{ filteredKnowledge.length }} 条正式知识单元</div>

    <a-modal v-model:open="versionModalOpen" title="知识单元详情" :footer="null" width="760px">
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
          <div><span>知识 ID</span><code :title="selectedKnowledge.knowledge_id">{{ selectedKnowledge.knowledge_id }}</code></div>
          <div><span>知识类型</span><strong>{{ selectedKnowledge.source_role === 'PRIMARY' ? '通用知识' : '条件变体' }}</strong></div>
          <div><span>来源片段</span><strong>{{ sourceCountLabel(selectedKnowledge) }}</strong></div>
          <div><span>索引状态</span><a-tag color="success">已索引</a-tag></div>
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
          </template>
        </a-table>
      </a-spin>
    </a-modal>
  </section>
</template>

<script setup>
import { computed, ref, watch } from 'vue'
import { message } from 'ant-design-vue'
import { ChevronDown, ChevronRight, ExternalLink, RefreshCw } from 'lucide-vue-next'
import { governanceApi } from '@/apis/governance_api'

const props = defineProps({ sourceId: { type: String, default: '' } })
const emit = defineEmits(['count-change'])
const knowledge = ref([])
const versions = ref([])
const loading = ref(false)
const loadingVersions = ref(false)
const versionModalOpen = ref(false)
const selectedKnowledge = ref(null)
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
  { title: '发布时间', dataIndex: 'published_at', key: 'published_at' }
]
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
        items: [],
        fragmentCount: 0
      })
    }
    const group = groups.get(key)
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
    collapsedGroups.value = new Set()
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

async function openVersions(record) {
  selectedKnowledge.value = record
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
  min-width: 0;
  gap: 3px;
}
.source-eyebrow {
  color: var(--main-700);
  font-size: 10px;
  font-weight: 650;
  letter-spacing: 0.04em;
}
.source-group-title strong {
  overflow: hidden;
  color: var(--color-text);
  font-size: 14px;
  font-weight: 650;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.source-path {
  overflow: hidden;
  color: var(--color-text-tertiary);
  font-size: 11px;
  text-overflow: ellipsis;
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
  transition: background 140ms ease, box-shadow 140ms ease;
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
}
</style>
