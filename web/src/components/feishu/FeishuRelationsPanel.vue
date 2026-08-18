<template>
  <section class="governance-card">
    <div class="section-heading">
      <div><h2>跨文档问题</h2><p>仅展示需要人工裁决的重复、重叠、条件变体和冲突关系。</p></div>
      <a-button size="small" :loading="loading" @click="loadRelations"><RefreshCw :size="15" />刷新</a-button>
    </div>
    <div class="toolbar">
      <a-select v-model:value="relationType" size="small" :options="relationOptions" />
      <a-select v-model:value="relationStatus" size="small" :options="statusOptions" />
      <a-input v-model:value="searchText" size="small" placeholder="搜索产品或资料" allow-clear />
    </div>
    <a-table :columns="columns" :data-source="filteredRelations" :loading="loading" :pagination="{ pageSize: 10 }" row-key="relation_id" size="small" :scroll="{ x: 980 }">
      <template #bodyCell="{ column, record }">
        <template v-if="column.key === 'relation_type'"><a-tag :color="relationColor(record.relation_type)">{{ relationLabel(record.relation_type) }}</a-tag></template>
        <template v-else-if="column.key === 'topic'"><strong class="primary-cell">{{ record.source_title }}</strong><span class="sub-line">{{ record.reasoning || '等待人工确认关系' }}</span></template>
        <template v-else-if="column.key === 'sources'"><span>{{ record.source_title }}</span><span class="sub-line">{{ record.target_title }}</span></template>
        <template v-else-if="column.key === 'scope'"><span>{{ record.scope_difference?.summary || '范围待确认' }}</span></template>
        <template v-else-if="column.key === 'status'"><a-tag :color="record.status === 'open' ? 'warning' : 'success'">{{ record.status === 'open' ? '待裁决' : '已处理' }}</a-tag></template>
        <template v-else-if="column.key === 'action'"><a-button size="small" @click="$emit('open-review', record)">进入审核</a-button></template>
      </template>
      <template #emptyText><a-empty description="暂无跨文档问题" /></template>
    </a-table>
    <div class="table-footer">共 {{ filteredRelations.length }} 条跨文档关系</div>
  </section>
</template>

<script setup>
import { computed, ref, watch } from 'vue'
import { message } from 'ant-design-vue'
import { RefreshCw } from 'lucide-vue-next'
import { governanceApi } from '@/apis/governance_api'

const props = defineProps({ sourceId: { type: String, default: '' } })
const emit = defineEmits(['open-review', 'count-change'])
const relations = ref([])
const loading = ref(false)
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
  { title: '关联来源', key: 'sources', width: 230 },
  { title: '适用范围', key: 'scope', width: 150 },
  { title: '处理状态', key: 'status', width: 100 },
  { title: '操作', key: 'action', width: 100 }
]
const filteredRelations = computed(() => {
  const keyword = searchText.value.trim().toLowerCase()
  if (!keyword) return relations.value
  return relations.value.filter((item) => `${item.source_title} ${item.target_title} ${item.reasoning || ''}`.toLowerCase().includes(keyword))
})

watch(() => [props.sourceId, relationType.value, relationStatus.value], loadRelations, { immediate: true })

async function loadRelations() {
  if (!props.sourceId) return
  loading.value = true
  try {
    const response = await governanceApi.listRelations(props.sourceId, { relation_type: relationType.value || undefined, status: relationStatus.value || undefined })
    relations.value = response.items || []
    emit('count-change', relations.value.filter((item) => item.status === 'open').length)
  } catch (error) {
    message.error(governanceApi.getErrorMessage(error, '加载跨文档问题失败'))
  } finally {
    loading.value = false
  }
}
function relationLabel(value) { return { CONFLICT: '内容冲突', EXACT_DUPLICATE: '完全重复', OVERLAP: '内容重叠', CONDITIONAL_VARIANT: '条件变体', COMPLEMENTARY: '互补内容', INSUFFICIENT: '证据不足' }[value] || value || '待比较' }
function relationColor(value) { return { CONFLICT: 'error', EXACT_DUPLICATE: 'success', OVERLAP: 'warning', CONDITIONAL_VARIANT: 'processing', INSUFFICIENT: 'warning' }[value] || 'default' }
</script>

<style scoped lang="less">
.governance-card { margin: 10px var(--page-padding) 24px; padding: 14px 16px; border: 1px solid color-mix(in srgb, var(--gray-150) 45%, transparent); border-radius: 8px; background: var(--gray-0); }
.section-heading { display: flex; align-items: center; justify-content: space-between; gap: 16px; margin-bottom: 11px; }
.section-heading h2 { margin: 0; font-size: 16px; font-weight: 600; }
.section-heading p { margin: 3px 0 0; color: var(--color-text-tertiary); font-size: 12px; }
.toolbar { display: flex; gap: 8px; margin-bottom: 10px; padding: 9px; background: var(--gray-25); }
.toolbar :deep(.ant-input), .toolbar :deep(.ant-select) { width: 170px; }
.primary-cell { display: block; color: var(--color-text); font-weight: 550; }
.sub-line { display: block; margin-top: 3px; color: var(--color-text-tertiary); font-size: 11px; line-height: 1.45; }
.table-footer { margin-top: 9px; color: var(--color-text-tertiary); font-size: 12px; }
</style>
