<template>
  <main class="feishu-login-result">
    <a-spin v-if="!error" tip="正在登录善达知枢…" />
    <a-result v-else status="warning" title="飞书登录未完成" :sub-title="error">
      <template #extra><a-button type="primary" @click="router.replace('/login')">返回登录页</a-button></template>
    </a-result>
  </main>
</template>
<script setup>
import { onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { useUserStore } from '@/stores/user'
import { finishFeishuLogin } from '@/apis/feishuAuth'
const router = useRouter(), user = useUserStore(), error = ref('')
onMounted(async () => {
  let tokenAssigned = false
  const params = new URLSearchParams(window.location.hash.slice(1))
  window.history.replaceState(null, '', window.location.pathname)
  try {
    if (params.has('error')) throw new Error(params.get('error') === 'MANAGEMENT_ACCESS_REQUIRED'
      ? '此飞书账号尚无知枢管理权限，请联系管理员在用户管理中授权。你仍可使用企业知识助手。'
      : '飞书授权未完成或已过期，请重新登录')
    if (!params.get('code')) throw new Error('缺少登录凭证，请重新发起飞书登录')
    const result = await finishFeishuLogin(params.get('code'))
    user.logout()
    user.token = result.access_token
    localStorage.setItem('user_token', result.access_token)
    tokenAssigned = true
    await user.getCurrentUser()
    await router.replace('/meeting-management')
  } catch (e) {
    if (tokenAssigned) user.logout()
    error.value = e.message
  } finally {
    sessionStorage.removeItem('feishu_login_verifier')
  }
})
</script>
<style scoped lang="less">
.feishu-login-result { min-height: 100vh; display: grid; place-items: center; background: var(--gray-10); }
</style>
