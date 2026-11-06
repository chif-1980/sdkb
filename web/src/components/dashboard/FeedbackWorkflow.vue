<template>
  <a-modal :open="!!feedback" title="反馈治理" :footer="null" width="680px" @cancel="$emit('close')">
    <a-spin :spinning="loading">
      <a-alert v-if="error" type="error" show-icon :message="error" />
      <template v-if="workflow">
        <p><strong>{{ statusName(workflow.status) }}</strong> · {{ feedback.reason || '未填写原因' }}</p>
        <a-alert v-if="workflow.sourceChanged" type="warning" message="用户已修改反馈，请先点击「重新打开」更新工单内容，再继续处理。" show-icon />
        <p v-if="workflow.resolution">处理结果：{{ workflow.resolution }}</p>
        <p v-if="workflow.assigneeId">负责人：{{ assigneeName(workflow.assigneeId) }}</p>
        <template v-if="canManage">
          <a-form layout="vertical">
            <a-form-item v-if="canAssign" label="指派负责人">
              <a-select v-model:value="assigneeId" :options="assignees.map(u => ({value:u.id,label:u.name}))" placeholder="选择有反馈处理权限的用户" />
            </a-form-item>
            <a-form-item label="处理 / 复核说明">
              <a-textarea v-model:value="note" :rows="4" :maxlength="5000" placeholder="填写处理结果、核对依据或退回原因" />
            </a-form-item>
            <a-space wrap>
              <a-button v-if="canAssign" :disabled="!assigneeId || !note.trim()" :loading="busy" @click="act('ASSIGN')">指派</a-button>
              <a-button v-if="!workflow.sourceChanged && workflow.status === 'ASSIGNED' && isAssignee" :disabled="!note.trim()" :loading="busy" @click="act('START')">开始处理</a-button>
              <a-button v-if="!workflow.sourceChanged && workflow.status === 'PROCESSING' && isAssignee" type="primary" :disabled="!note.trim()" :loading="busy" @click="act('SUBMIT_REVIEW')">提交复核</a-button>
              <template v-if="!workflow.sourceChanged && workflow.status === 'IN_REVIEW' && !isProcessor">
                <a-button type="primary" :disabled="!note.trim()" :loading="busy" @click="act('APPROVE')">复核通过</a-button>
                <a-button :disabled="!note.trim()" :loading="busy" @click="act('REJECT')">退回处理</a-button>
              </template>
              <a-button v-if="!workflow.sourceChanged && workflow.status === 'RESOLVED'" :disabled="!note.trim()" :loading="busy" @click="act('CLOSE')">关闭反馈</a-button>
              <a-button v-if="['CLOSED','RESOLVED'].includes(workflow.status) || workflow.sourceChanged" :disabled="!note.trim()" :loading="busy" @click="act('REOPEN')">重新打开</a-button>
            </a-space>
            <p v-if="!workflow.sourceChanged && workflow.status === 'IN_REVIEW' && isProcessor">已提交处理结果，等待另一位有权限的人员复核。</p>
          </a-form>
        </template>
        <h3>操作记录</h3>
        <a-empty v-if="!workflow.history.length" description="尚未指派处理" />
        <ol class="feedback-history">
          <li v-for="(event, index) in [...workflow.history].reverse()" :key="index">
            <strong>{{ event.actorName }} · {{ statusName(event.to) }}</strong>
            <small>{{ formatFullDateTime(event.at) }}</small>
            <p>{{ event.note }}</p>
          </li>
        </ol>
      </template>
    </a-spin>
  </a-modal>
</template>

<script setup>
import { ref, computed, watch } from 'vue'
import { useUserStore } from '@/stores/user'
import { getFeedbackWorkflow, updateFeedbackWorkflow, getFeedbackAssignees } from '@/apis/feedbackGovernance'
import { formatFullDateTime } from '@/utils/time'
import { feedbackStatusName as statusName } from '@/utils/feedbackGovernance'
const props = defineProps({ feedback: Object })
const emit = defineEmits(['close', 'updated'])
const user = useUserStore()
const workflow = ref(null), loading = ref(false), busy = ref(false), error = ref('')
const assignees = ref([]), assigneeId = ref(null), note = ref('')
const canManage = computed(() => user.hasPermission('feedback.manage', 'all'))
const canAssign = computed(() => !workflow.value?.sourceChanged && ['OPEN','ASSIGNED','PROCESSING'].includes(workflow.value?.status))
const isAssignee = computed(() => Number(user.userId) === workflow.value?.assigneeId)
const isProcessor = computed(() => Number(user.userId) === workflow.value?.processorId)
function assigneeName(id) {return assignees.value.find(u => u.id === id)?.name || `用户 ${id}`}
let request = 0
watch(() => props.feedback?.id, async id => {
  const current = ++request
  workflow.value = null; error.value = ''; note.value = ''
  if (id == null) return
  loading.value = true
  try {
    const [result, users] = await Promise.all([getFeedbackWorkflow(id), canManage.value ? getFeedbackAssignees() : Promise.resolve([])])
    if (current !== request) return
    workflow.value = result; assignees.value = users; assigneeId.value = result.assigneeId || null
  } catch(e) {if (current === request) error.value = e.message}
  finally {if (current === request) loading.value = false}
}, {immediate:true})
async function act(action) {
  if (busy.value) return
  busy.value = true; error.value = ''
  const current = request
  try {
    const result = await updateFeedbackWorkflow(props.feedback.id, {version:workflow.value.version, action, assigneeId:assigneeId.value, note:note.value.trim()})
    if (current === request) {workflow.value = result; note.value = ''}
    emit('updated')
  } catch(e) {if (current === request) error.value = e.message}
  finally {busy.value = false}
}
</script>

<style scoped lang="less">
.feedback-history {padding-left:20px; max-height:280px; overflow:auto;
  li {margin-bottom:16px; p {white-space:pre-wrap; margin:4px 0;}}
  small {display:block; color:var(--color-text-secondary);}
}
</style>
