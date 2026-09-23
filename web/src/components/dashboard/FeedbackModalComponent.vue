<template>
  <a-modal v-model:open="modalVisible" title="用户反馈详情" width="1200px" :footer="null" destroy-on-close>
    <FeedbackListComponent v-if="modalVisible" :agent-id="agentId" />
  </a-modal>
</template>

<script setup>
import { ref } from 'vue'
import FeedbackListComponent from './FeedbackListComponent.vue'

defineProps({ agentId: { type: String, default: null } })
const modalVisible = ref(false)
const show = () => {
  modalVisible.value = true
}
defineExpose({ show })
</script>
