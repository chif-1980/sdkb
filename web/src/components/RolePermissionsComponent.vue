<template>
  <section class="role-permissions">
    <h2>角色权限</h2>
    <a-spin :spinning="loading">
      <a-alert v-if="error" type="error" :message="error" show-icon />
      <a-form v-else layout="vertical" @finish="save">
        <a-form-item label="功能"><span>用户反馈 · 查看</span></a-form-item>
        <a-form-item label="超级管理员"><span>全企业</span></a-form-item>
        <a-form-item label="管理员" name="feedbackScope">
          <a-select v-model:value="scope" :options="scopeOptions" :disabled="saving" />
        </a-form-item>
        <a-form-item label="普通用户"><span>无权限</span></a-form-item>
        <a-button type="primary" html-type="submit" :loading="saving" :disabled="loading">
          <template #icon><Save :size="16" /></template>
          保存权限
        </a-button>
      </a-form>
    </a-spin>
  </section>
</template>

<script setup>
import { onMounted, ref } from 'vue'
import { message } from 'ant-design-vue'
import { Save } from 'lucide-vue-next'
import { rolePermissionApi } from '@/apis/role_permission_api'
import { useUserStore } from '@/stores/user'

const loading = ref(true)
const saving = ref(false)
const error = ref('')
const scope = ref('none')
const userStore = useUserStore()
const scopeOptions = [
  { label: '无权限', value: 'none' },
  { label: '本部门', value: 'department' },
  { label: '全企业', value: 'all' }
]
onMounted(async () => {
  try {
    scope.value = (await rolePermissionApi.list()).admin
  } catch {
    error.value = '角色权限加载失败，请重新打开设置。'
  } finally {
    loading.value = false
  }
})
async function save() {
  saving.value = true
  try {
    await rolePermissionApi.setFeedbackScope(scope.value)
    await userStore.refreshPermissions()
    message.success('角色权限已保存')
  } catch {
    message.error('权限保存失败，请重试')
  } finally {
    saving.value = false
  }
}
</script>

<style scoped lang="less">
.role-permissions {
  max-width: 560px;
  h2 { font-size: 20px; color: var(--color-text); margin: 0 0 24px; }
  :deep(.ant-btn) { display: inline-flex; align-items: center; gap: 6px; }
}
</style>
