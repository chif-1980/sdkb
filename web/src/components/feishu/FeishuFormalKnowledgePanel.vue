<template>
  <section class="governance-card">
    <div class="section-heading">
      <div><h2>正式知识</h2><p>只展示当前生效、已审核、已发布并进入 Yuxi 索引的逻辑知识。</p></div>
      <a-button size="small" :loading="loading" @click="loadKnowledge"><RefreshCw :size="15" />刷新</a-button>
    </div>
    <div class="toolbar">
      <a-select v-model:value="knowledgeType" size="small" :options="typeOptions" />
      <a-input v-model:value="searchText" size="small" placeholder="搜索知识标题、产品或版本" allow-clear />
    </div>
    <a-table :columns="columns" :data-source="filteredKnowledge" :loading="loading" :pagination="{ pageSize: 10 }" row-key="knowledge_id" size="small" :scroll="{ x: 980 }">
      <template #bodyCell="{ column, record }">
        <template v-if="column.key === 'title'"><strong class="primary-cell">{{ record.title }}</strong><span class="sub-line">稳定知识 ID：{{ record.knowledge_id }} · 当前版本 {{ record.revision }}</span></template>
        <template v-else-if="column.key === 'kind'"><a-tag color="processing">{{ record.source_role === 'PRIMARY' ? '通用知识' : '条件变体' }}</a-tag></template>
        <template v-else-if="column.key === 'scope'"><span>{{ scopeSummary(record.applicability_scope) }}</span></template>
        <template v-else-if="column.key === 'sources'"><span class="source-link">1 个主来源</span><span class="sub-line">{{ record.chunk_count || 0 }} 个分块</span></template>
        <template v-else-if="column.key === 'index_status'"><a-tag color="success">已索引</a-tag></template>
        <template v-else-if="column.key === 'action'"><a-button size="small" @click="openVersions(record)">查看版本</a-button></template>
      </template>
      <template #emptyText><a-empty description="暂无正式知识" /></template>
    </a-table>
    <div class="table-footer">共 {{ filteredKnowledge.length }} 条正式知识</div>

    <a-modal v-model:open="versionModalOpen" title="知识版本" :footer="null" width="680px">
      <a-spin :spinning="loadingVersions">
        <a-table :columns="versionColumns" :data-source="versions" row-key="version_id" size="small" :pagination="false">
          <template #bodyCell="{ column, record }">
            <template v-if="column.key === 'status'"><a-tag :color="record.active ? 'success' : 'default'">{{ record.active ? '当前有效' : record.processing_status }}</a-tag></template>
            <template v-else-if="column.key === 'review'"><span>{{ record.review_status === 'approved' ? '已审核' : record.review_status }}</span></template>
          </template>
        </a-table>
      </a-spin>
    </a-modal>
  </section>
</template>

<script setup>
import { computed, ref, watch } from 'vue'
import { message } from 'ant-design-vue'
import { RefreshCw } from 'lucide-vue-next'
import { governanceApi } from '@/apis/governance_api'

const props = defineProps({ sourceId: { type: String, default: '' } })
const emit = defineEmits(['count-change'])
const knowledge = ref([])
const versions = ref([])
const loading = ref(false)
const loadingVersions = ref(false)
const versionModalOpen = ref(false)
const knowledgeType = ref('')
const searchText = ref('')
const typeOptions = [{ label: '全部类型', value: '' }, { label: '通用知识', value: 'PRIMARY' }, { label: '条件变体', value: 'VARIANT' }]
const columns = [
  { title: '正式知识', key: 'title', width: 280 },
  { title: '知识形态', key: 'kind', width: 110 },
  { title: '适用范围', key: 'scope', width: 180 },
  { title: '来源关系', key: 'sources', width: 150 },
  { title: '索引状态', key: 'index_status', width: 100 },
  { title: '操作', key: 'action', width: 100 }
]
const versionColumns = [
  { title: '版本', dataIndex: 'revision', key: 'revision', width: 100 },
  { title: '状态', key: 'status', width: 120 },
  { title: '审核', key: 'review', width: 120 },
  { title: '发布时间', dataIndex: 'published_at', key: 'published_at' }
]
const filteredKnowledge = computed(() => knowledge.value.filter((item) => {
  const matchType = !knowledgeType.value || (knowledgeType.value === 'PRIMARY' ? item.source_role === 'PRIMARY' : item.source_role !== 'PRIMARY')
  const keyword = searchText.value.trim().toLowerCase()
  return matchType && (!keyword || `${item.title} ${item.knowledge_id} ${scopeSummary(item.applicability_scope)}`.toLowerCase().includes(keyword))
}))

watch(() => [props.sourceId, knowledgeType.value], loadKnowledge, { immediate: true })

async function loadKnowledge() {
  if (!props.sourceId) return
  loading.value = true
  try {
    const response = await governanceApi.listFormalKnowledge(props.sourceId)
    knowledge.value = response.items || []
    emit('count-change', knowledge.value.length)
  } catch (error) {
    message.error(governanceApi.getErrorMessage(error, '加载正式知识失败'))
  } finally {
    loading.value = false
  }
}

async function openVersions(record) {
  versionModalOpen.value = true
  loadingVersions.value = true
  try {
    const response = await governanceApi.listKnowledgeVersions(record.knowledge_id)
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
</script>

<style scoped lang="less">
.governance-card { margin: 10px var(--page-padding) 24px; padding: 14px 16px; border: 1px solid color-mix(in srgb, var(--gray-150) 45%, transparent); border-radius: 8px; background: var(--gray-0); }
.section-heading { display: flex; align-items: center; justify-content: space-between; gap: 16px; margin-bottom: 11px; }
.section-heading h2 { margin: 0; font-size: 16px; font-weight: 600; }
.section-heading p { margin: 3px 0 0; color: var(--color-text-tertiary); font-size: 12px; }
.toolbar { display: flex; gap: 8px; margin-bottom: 10px; padding: 9px; background: var(--gray-25); }
.toolbar :deep(.ant-input) { width: 280px; }
.toolbar :deep(.ant-select) { width: 150px; }
.primary-cell { display: block; color: var(--color-text); font-weight: 550; }
.sub-line { display: block; margin-top: 3px; color: var(--color-text-tertiary); font-size: 11px; line-height: 1.45; }
.source-link { color: var(--main-700); }
.table-footer { margin-top: 9px; color: var(--color-text-tertiary); font-size: 12px; }
</style>
