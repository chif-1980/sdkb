<template>
  <main class="feishu-account">
    <h1>飞书账号与授权</h1>
    <a-alert v-if="error" type="error" :message="error" show-icon />
    <a-spin :spinning="loading">
      <a-descriptions v-if="identity" bordered :column="1">
        <a-descriptions-item label="当前账号">{{ user.username }}</a-descriptions-item>
        <a-descriptions-item label="飞书身份">{{ identity.linked ? identity.name : '未关联（本地账号）' }}</a-descriptions-item>
        <a-descriptions-item v-if="identity.linked" label="企业标识">{{ identity.tenantKey }}</a-descriptions-item>
        <a-descriptions-item label="系统角色">{{ user.isSuperAdmin ? '超级管理员' : user.isAdmin ? '管理员' : '普通用户' }}</a-descriptions-item>
      </a-descriptions>
      <p>助手与知枢共用飞书身份，管理权限由管理员分配。飞书登录不会自动获得文档读取权限。</p>
      <a-alert v-if="identity && !identity.linked" type="info" show-icon message="建议使用已有飞书账号登录"
        description="请先在用户管理中为对应飞书账号授予管理权限，再退出并使用飞书登录；本地账号与飞书账号不会自动合并。" />
      <a-button v-if="user.isAdmin" @click="router.push('/feishu-knowledge')">管理知识访问授权</a-button>
      <p>每个知识源会显示授权提供者；新增知识源可复用当前飞书身份的有效授权，权限不足时再补充授权。</p>
    </a-spin>
  </main>
</template>
<script setup>
import { onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { useUserStore } from '@/stores/user'
import { feishuIdentity } from '@/apis/feishuAuth'
const user = useUserStore(), router = useRouter()
const identity = ref(null), loading = ref(true), error = ref('')
onMounted(async () => { try { identity.value = await feishuIdentity() } catch(e) { error.value = e.message } finally { loading.value = false } })
</script>
<style scoped lang="less">
.feishu-account { max-width: 800px; margin: 0 auto; padding: 32px; }
p { color: var(--gray-700); line-height: 1.8; margin: 20px 0; }
</style>
