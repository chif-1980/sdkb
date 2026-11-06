<template>
  <div class="user-feedback-page">
    <header class="page-heading">
      <h1>用户反馈</h1>
      <p>指派、处理和复核用户反馈，关闭后仍保留完整操作记录。</p>
    </header>
    <FeedbackListComponent />
  </div>
</template>

<script setup>
import FeedbackListComponent from '@/components/dashboard/FeedbackListComponent.vue'
</script>

<style scoped lang="less">
.user-feedback-page {
  padding: var(--page-padding);
  overflow-y: auto;
  height: 100%;
  min-width: 0;
  background: var(--gray-25);
}
.page-heading {
  margin-bottom: 24px;
  h1 {
    margin: 0 0 8px;
    font-size: 22px;
    font-weight: 600;
    color: var(--color-text);
  }
  p {
    margin: 0;
    color: var(--color-text-secondary);
  }
}
:deep(.feedback-cards-container) {
  max-height: none;
  overflow: visible;
  padding-right: 0;
}
</style>
