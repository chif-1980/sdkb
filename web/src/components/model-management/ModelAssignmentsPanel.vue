<script setup>
import { computed, onMounted, reactive, ref } from 'vue'
import { message } from 'ant-design-vue'
import { configApi } from '@/apis/system_api'
import { useConfigStore } from '@/stores/config'

const props = defineProps({ providers: { type: Array, default: () => [] } })
const configStore = useConfigStore()
const loading = ref(true)
const saving = ref(false)
const loadError = ref(false)
const draft = reactive({})
const roles = [
  { key: 'default_model', type: 'chat', title: '统一业务模型', help: '知识加工（图谱、思维导图等）、知识问答、会议纪要、方案和智能体' },
  { key: 'fast_model', type: 'chat', title: '快速响应', help: '标题生成等轻量任务' },
  { key: 'embed_model', type: 'embedding', title: '向量化', help: '新建知识库的默认模型；已有索引保持原模型' },
  { key: 'reranker', type: 'rerank', title: '检索重排', help: '对检索结果进行相关性排序' },
  { key: 'content_guard_llm_model', type: 'chat', title: '内容审查', help: '仅在系统设置中启用模型审查后使用' }
]
const changed = computed(() => roles.some(role => draft[role.key] !== configStore.config?.[role.key]))
const optionsFor = type => props.providers.filter(p => p.is_enabled && p.credential_status !== 'warning')
  .flatMap(p => (p.enabled_models || []).filter(m => m.type === type).map(m => ({
    value: `${p.provider_id}:${m.id}`,
    label: `${p.display_name} / ${m.display_name || m.id}`
  })))
const load = async () => {
  loading.value = true
  loadError.value = false
  try {
    const config = await configStore.refreshConfig()
    roles.forEach(role => { draft[role.key] = config[role.key] })
  } catch { loadError.value = true }
  finally { loading.value = false }
}
const save = async () => {
  saving.value = true
  try {
    const changes = Object.fromEntries(roles.filter(role => draft[role.key] !== configStore.config[role.key])
      .map(role => [role.key, draft[role.key]]))
    const config = await configApi.updateConfigBatch(changes)
    configStore.setConfig(config)
    message.success('已保存，善达知枢和企业知识助手的新任务将使用统一配置')
  } catch (error) { message.error(error.message || '保存失败，请重试') }
  finally { saving.value = false }
}
onMounted(load)
</script>

<template>
  <section class="model-assignments" aria-label="统一模型配置">
    <div class="assignment-heading">
      <div><h3>统一模型配置</h3><p>善达知枢与企业知识助手共用。下方维护接口和凭证，这里按用途分配模型。</p></div>
      <a-button type="primary" :loading="saving" :disabled="loading || loadError || !changed" @click="save">保存配置</a-button>
    </div>
    <a-alert v-if="loadError" type="error" message="模型配置加载失败">
      <template #action><a-button size="small" @click="load">重试</a-button></template>
    </a-alert>
    <a-spin v-else :spinning="loading">
      <div class="assignment-grid">
        <div v-for="role in roles" :key="role.key" class="assignment-field">
          <label :for="`assignment-${role.key}`">{{ role.title }}</label>
          <a-select :id="`assignment-${role.key}`" v-model:value="draft[role.key]" :aria-label="role.title"
            :options="optionsFor(role.type)" show-search option-filter-prop="label" :disabled="loading || saving"
            placeholder="选择已配置的模型" />
          <p>{{ role.help }}</p>
        </div>
      </div>
    </a-spin>
  </section>
</template>

<style scoped lang="less">
.model-assignments { margin-bottom: 20px; padding: 20px; border: 1px solid var(--gray-150); border-radius: 8px; background: var(--gray-0); }
.assignment-heading { display: flex; align-items: flex-start; justify-content: space-between; gap: 16px; margin-bottom: 20px;
  h3 { margin: 0 0 6px; color: var(--gray-900); font-size: 16px; font-weight: 600; }
  p { margin: 0; color: var(--gray-500); font-size: 13px; line-height: 1.6; }
}
.assignment-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 20px 24px; }
.assignment-field { display: flex; flex-direction: column; min-width: 0; gap: 8px;
  label { color: var(--gray-900); font-size: 14px; font-weight: 500; }
  p { margin: 0; color: var(--gray-500); font-size: 12px; line-height: 1.5; }
}
@media (max-width: 640px) { .assignment-grid { grid-template-columns: 1fr; } .assignment-heading { flex-wrap: wrap; } }
</style>
