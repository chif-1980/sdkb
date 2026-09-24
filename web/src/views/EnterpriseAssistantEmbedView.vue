<template>
  <main class="assistant-embed-page" aria-label="企业知识助手">
    <div v-if="loading" class="assistant-embed-state">
      <a-spin tip="正在打开企业知识助手…" />
    </div>
    <a-result v-else-if="error" status="warning" title="企业知识助手暂时无法打开" :sub-title="error">
      <template #extra>
        <a-button type="primary" @click="loadAssistant">重试</a-button>
      </template>
    </a-result>
    <iframe
      v-else-if="handoffUrl"
      class="assistant-embed-frame"
      :src="handoffUrl"
      title="企业知识助手"
      allow="clipboard-read; clipboard-write"
    />
  </main>
</template>

<script setup>
import { onMounted, ref } from 'vue'
import { assistantHandoffApi } from '@/apis/assistant_handoff_api'

const handoffUrl = ref('')
const loading = ref(false)
const error = ref('')

async function loadAssistant() {
  loading.value = true
  error.value = ''
  handoffUrl.value = ''
  try {
    const result = await assistantHandoffApi.createLink()
    if (!result?.url) throw new Error('未获取到企业知识助手地址')
    handoffUrl.value = result.url
  } catch (err) {
    error.value = err?.message || '请稍后重试'
  } finally {
    loading.value = false
  }
}

onMounted(loadAssistant)
</script>

<style scoped lang="less">
.assistant-embed-page {
  width: 100%;
  height: 100%;
  min-width: 0;
  background: var(--gray-0);
}

.assistant-embed-frame {
  display: block;
  width: 100%;
  height: 100%;
  border: 0;
}

.assistant-embed-state {
  display: grid;
  min-height: 100%;
  place-items: center;
}

.assistant-embed-page :deep(.ant-result) {
  padding-top: 18vh;
}
</style>
