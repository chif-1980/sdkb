<template>
  <main class="meeting-page">
    <button class="meeting-back" @click="back"><ArrowLeft :size="16" /> 返回会议列表</button>
    <a-alert v-if="error" type="error" show-icon :message="error" />
    <a-spin :spinning="loading">
      <template v-if="data">
        <header class="meeting-heading">
          <div><h1>{{ data.meeting.title }}</h1><p>跟进负责人：{{ result?.followup?.coordinator?.displayName || '上传者' }}</p></div>
          <div class="meeting-actions">
            <a-button :disabled="busy || editing" @click="load">刷新</a-button>
            <a-button :disabled="busy || editing" @click="archive">{{ data.meeting.archived ? '恢复会议' : '归档会议' }}</a-button>
            <a-button v-if="current" :disabled="busy || editing" @click="run(() => downloadMeeting(id, current.version), false)">导出 Word</a-button>
          </div>
        </header>
        <div class="meeting-meta">
          <a-tag :color="stateColor(data.meeting.state)">{{ label(data.meeting.state) }}</a-tag>
          <a-tag v-if="data.meeting.archived">已归档</a-tag>
          <span>会议日期：{{ data.meeting.meetingDate || '未提供' }}</span>
          <span>上传人：{{ data.ownerDisplayName || data.meeting.ownerDisplayName || '未提供' }}</span><span>上传于 {{ formatDate(data.meeting.createdAt) }}</span>
          <span>{{ data.meeting.meetingType }}</span>
        </div>
        <a-alert v-if="data.meeting.state === 'failed'" type="warning" show-icon :message="data.meeting.error?.message || '本次分析失败'" :description="current ? '下方保留上次成功的纪要和跟进事项。' : '可重试分析，原始资料仍保留。'" />
        <a-alert v-if="['pending','running'].includes(data.meeting.state)" type="info" show-icon :message="data.meeting.progress?.message || '正在后台处理'" />
        <a-tabs v-model:active-key="tab">
          <a-tab-pane key="minutes" tab="纪要与依据">
            <div class="meeting-actions" style="margin-bottom:16px">
              <a-button v-if="current && !editing && !data.meeting.archived" @click="startEdit">编辑纪要</a-button>
              <a-button v-if="!data.meeting.archived && !['pending','running'].includes(data.meeting.state)" :disabled="busy || editing" @click="run(() => retryManagedMeeting(id))">重新分析</a-button>
              <a-button v-if="['pending','running'].includes(data.meeting.state)" :disabled="busy" @click="run(() => cancelManagedMeeting(id))">取消分析</a-button>
            </div>
            <div v-if="editing" class="meeting-panel meeting-editor">
              <a-input v-model:value="draft.title" aria-label="会议标题" />
              <a-input v-model:value="draft.meetingType" aria-label="会议类型" />
              <a-textarea v-model:value="draft.body" :rows="20" aria-label="会议纪要正文" />
              <div class="meeting-actions"><a-button type="primary" :loading="busy" @click="saveMinutes">保存修改</a-button><a-button :disabled="busy" @click="editing = false">取消</a-button></div>
            </div>
            <section v-else-if="result" class="meeting-panel"><p class="meeting-context-note">纪要保留会议当时的安排；后续负责人、期限和执行进度请查看“待办事项”。</p><MarkdownPreview :content="result.body" /></section>
            <a-empty v-else description="尚无成功的会议纪要" />
            <section v-if="current" class="meeting-panel">
              <h2>原始资料与依据</h2>
              <div class="meeting-source"><a v-for="(source, index) in current.sources" :key="index" :href="safeSourceUrl(source.url)" target="_blank" rel="noopener noreferrer">{{ source.title || `资料 ${index + 1}` }} ↗</a></div>
              <a-input-search v-model:value="evidenceQuery" placeholder="输入依据编号（如 S1-P934）或原文关键词" aria-label="查找原文依据" @search="findEvidence" />
              <p v-if="evidenceNotice">{{ evidenceNotice }}</p>
              <blockquote v-for="item in evidence" :key="item.ref" class="meeting-evidence"><strong>{{ item.ref }} · {{ item.speaker || '未提供发言人' }} · {{ evidenceTime(item) }}</strong><br>{{ item.text }}</blockquote>
            </section>
          </a-tab-pane>
          <a-tab-pane key="TASK" tab="待办事项">
            <div class="meeting-actions" style="margin-bottom:16px">
              <a-button :disabled="!current || busy || data.meeting.archived" @click="addTask">补充待办</a-button>
              <a-button :disabled="!current || busy || data.meeting.archived" @click="run(() => syncMeetingTasks(id))">刷新飞书状态</a-button>
              <span>无期限的事项不计入逾期。</span>
            </div>
            <p class="meeting-context-note">会议识别的负责人和期限需核对后确认；此处显示与助手共用的最新待办，纪要原文保留会议当时的安排。</p>
            <article v-for="task in tasks" :key="task.id" class="meeting-item">
              <div class="meeting-item-header"><div><h3>{{ task.title }}</h3><p>{{ task.content }}</p></div><span :class="['meeting-review-state', `is-${reviewTone(task.reviewStatus || 'PENDING')}`]"><span class="meeting-state-mark" aria-hidden="true" />{{ label(task.reviewStatus || 'PENDING') }}</span></div>
              <div class="meeting-meta"><span>负责人：{{ taskAssigneeLabel(task) }}</span><span>期限：{{ taskDeadlineLabel(task) }}</span><span class="meeting-execution-status"><strong>执行状态：</strong><span :class="['meeting-execution-state', `is-${executionTone(task)}`]"><span class="meeting-state-mark" aria-hidden="true" />{{ taskExecutionLabel(task) }}</span></span></div>
              <small v-if="task.delivery?.feishuTaskId">飞书任务 {{ task.delivery.feishuTaskId }} · {{ label(task.delivery.syncStatus) }} · 最近读取 {{ formatDate(task.delivery.lastSyncedAt) }}</small>
              <small v-if="(task.delivery?.error || task.delivery?.syncError)" class="meeting-error">{{ task.delivery.error || task.delivery.syncError }}</small>
              <div class="meeting-source"><button v-for="ref in task.sourceRefs" :key="ref" class="meeting-title-link" @click="showTaskEvidence(task, ref)">{{ ref }}</button></div>
              <div class="meeting-actions" v-if="!data.meeting.archived">
                <a-button :disabled="busy" @click="editTask(task)">修改</a-button>
                <a-button :disabled="busy" @click="sendTask(task)">{{ task.delivery?.feishuTaskId ? '同步修改' : '确认并发送' }}</a-button>
                <a-button v-if="!task.delivery?.feishuTaskId && task.reviewStatus !== 'IGNORED'" :disabled="busy" @click="taskAction(task, 'IGNORE')">忽略</a-button>
              </div>
            </article>
            <a-empty v-if="!tasks.length" description="暂无待办，可手动补充" />
          </a-tab-pane>
          <a-tab-pane key="KNOWLEDGE" tab="知识建议">
            <p style="margin-bottom:16px">会议表述会先与正式知识比对。修改建议提交后由维护人员更新飞书原文，再进入现有审核/发布流程。</p>
            <article v-for="item in suggestions" :key="item.id" class="meeting-item">
              <div class="meeting-item-header"><h3>{{ item.title }}</h3><a-tag>{{ label(item.status) }}</a-tag></div>
              <p>{{ item.reason }}</p>
              <blockquote class="meeting-evidence"><strong>{{ label(item.comparisonStatus) }}</strong><br>{{ item.comparison }}</blockquote>
              <div v-for="reference in formalReferences(item)" :key="reference.evidence_id" class="meeting-evidence"><strong>{{ reference.title || reference.evidence_id }}</strong><p>{{ reference.excerpt || reference.content || reference.snippet }}</p></div>
              <details v-if="item.draftContent"><summary>查看建议草稿</summary><MarkdownPreview :content="item.draftContent" /></details>
              <small v-if="item.decisionReason">维护人员处理说明：{{ item.decisionReason }}</small>
              <div class="meeting-source"><button v-for="ref in item.sourceRefs" :key="ref" class="meeting-title-link" @click="showTaskEvidence(item, ref)">{{ ref }}</button></div>
              <a-button v-if="!data.meeting.archived" :disabled="busy" @click="openDecision(item)">处理建议</a-button>
            </article>
            <a-empty v-if="!suggestions.length" description="暂无知识更新建议" />
          </a-tab-pane>
          <a-tab-pane key="history" tab="处理记录">
            <section class="meeting-panel"><h2>分析版本</h2>
              <a-list :data-source="data.runs"><template #renderItem="{item}"><a-list-item><span>{{ formatDate(item.createdAt) }} · {{ label(item.state) }} · {{ item.version }} 次修订</span><a-button v-if="item.version" @click="viewVersion(item)">查看</a-button></a-list-item></template></a-list>
            </section>
            <section class="meeting-panel"><h2>最近处理记录</h2><a-list :data-source="data.events"><template #renderItem="{item}"><a-list-item><div>{{ label(item.action) }}<small>{{ item.detail.reason || item.detail.message }}</small></div><small>{{ formatDate(item.createdAt) }}</small></a-list-item></template></a-list></section>
          </a-tab-pane>
        </a-tabs>
      </template>
    </a-spin>
    <a-modal v-model:open="taskModal" title="修改会议待办" :confirm-loading="busy" @ok="saveTask" :mask-closable="false">
      <a-form layout="vertical" v-if="taskDraft">
        <a-form-item label="待办标题"><a-input v-model:value="taskDraft.title" :maxlength="1000" /></a-form-item>
        <a-form-item label="待办内容"><a-textarea v-model:value="taskDraft.content" :rows="3" :maxlength="5000" /></a-form-item>
        <a-form-item label="负责人"><a-select v-model:value="taskDraft.assigneeFeishuUserId" show-search allow-clear :options="directoryOptions" option-filter-prop="label" :loading="directoryLoading" placeholder="从当前飞书企业通讯录选择" /></a-form-item>
        <a-form-item label="期限"><a-date-picker v-model:value="taskDraft.dueDate" value-format="YYYY-MM-DD" /></a-form-item>
        <a-form-item label="执行状态"><a-select v-model:value="taskDraft.status" :options="['OPEN','IN_PROGRESS','DONE'].map(value => ({value,label:label(value)}))" /></a-form-item>
        <a-alert v-if="directoryError" type="warning" :message="directoryError" />
      </a-form>
    </a-modal>
    <a-modal v-model:open="decisionModal" title="处理知识建议" :confirm-loading="busy" @ok="saveDecision" :mask-closable="false">
      <a-form layout="vertical">
        <a-form-item label="处理结论"><a-select v-model:value="decision.action" :options="['COVERED','REQUEST_SOURCE_CHANGE','NEW_SOURCE_DRAFT','PROCESSING','DEFERRED','REJECTED','PENDING_MAINTAINER'].map(value => ({value,label:label(value)}))" /></a-form-item>
        <a-form-item v-if="['COVERED','REQUEST_SOURCE_CHANGE'].includes(decision.action)" :label="decision.action === 'REQUEST_SOURCE_CHANGE' ? '需要修改的正式知识' : '关联正式知识'"><a-select v-model:value="decision.knowledgeUnitId" show-search :filter-option="false" :options="knowledgeOptions" placeholder="输入标题检索正式知识" @search="searchKnowledge" /></a-form-item>
        <a-form-item v-if="['PROCESSING','REQUEST_SOURCE_CHANGE','NEW_SOURCE_DRAFT'].includes(decision.action)" :label="decision.action === 'REQUEST_SOURCE_CHANGE' ? '原文修改建议' : '知识更新建议草稿'"><a-textarea v-model:value="decision.draftContent" :rows="8" :placeholder="decision.action === 'NEW_SOURCE_DRAFT' ? '填写需要维护人员补回飞书原文的内容' : '填写建议新增或修改的知识内容'" /></a-form-item>
        <a-form-item label="处理说明"><a-textarea v-model:value="decision.reason" :rows="4" :maxlength="2000" /></a-form-item>
      </a-form>
    </a-modal>
    <a-drawer v-model:open="previewOpen" title="会议历史版本 / 原文依据" width="min(760px, 95vw)">
      <template v-if="preview"><a-select v-if="preview.versions?.length" :value="preview.meeting.version" :options="preview.versions.map(value => ({value,label:`修订 ${value}`}))" @change="selectVersion" /><MarkdownPreview :content="preview.meeting.result?.body || '此版本无成功结果'" /></template>
      <blockquote v-for="item in previewEvidence" :key="item.ref" class="meeting-evidence"><strong>{{ item.ref }} · {{ item.speaker || '未提供发言人' }} · {{ evidenceTime(item) }}</strong><p>{{ item.text }}</p><a v-if="safeSourceUrl(item.url)" :href="item.url" target="_blank" rel="noopener noreferrer">打开原始会议</a></blockquote>
    </a-drawer>
  </main>
</template>
<script setup>
import { computed, onMounted, onBeforeUnmount, ref, watch } from 'vue'
import { useRoute, useRouter, onBeforeRouteLeave } from 'vue-router'
import { message, Modal } from 'ant-design-vue'
import { ArrowLeft } from 'lucide-vue-next'
import MarkdownPreview from '@/components/common/MarkdownPreview.vue'
import { searchFormalKnowledge, getManagedMeeting, archiveMeeting, editMeetingMinutes, editMeetingTasks, getMeetingDirectory,
  decideMeetingKnowledge, retryManagedMeeting, cancelManagedMeeting, syncMeetingTasks, downloadMeeting, getMeetingVersion } from '@/apis/meetingManagement'
import { label, formatDate, stateColor, reviewTone, executionTone, safeSourceUrl, taskAssigneeLabel, taskDeadlineLabel, taskExecutionLabel } from '@/utils/meetingManagement'
const route = useRoute(), router = useRouter(), id = route.params.id
const data = ref(null), loading = ref(false), busy = ref(false), error = ref('')
const tab = ref(['TASK','KNOWLEDGE'].includes(route.query.tab) ? route.query.tab : 'minutes')
const current = computed(() => data.value?.current), result = computed(() => current.value?.result)
const tasks = computed(() => result.value?.followup?.tasks || []), suggestions = computed(() => result.value?.followup?.knowledgeSuggestions || [])
const editing = ref(false), draft = ref({}), taskModal = ref(false), taskDraft = ref(null)
const directoryOptions = ref([]), directoryLoading = ref(false), directoryError = ref('')
const knowledgeOptions = ref([])
const decisionModal = ref(false), decision = ref({}), decisionItem = ref(null)
const evidenceQuery = ref(''), evidence = ref([]), evidenceNotice = ref('')
const previewOpen = ref(false), preview = ref(null), previewEvidence = ref([])
async function load() { loading.value = true; error.value = ''; try {data.value = await getManagedMeeting(id)} catch(e) {error.value = e.message} finally {loading.value = false} }
async function run(action, refresh = true) {busy.value = true; error.value = ''; try {const response = await action(); if(response?.deliveryError) message.warning(response.deliveryError); if(refresh) await load(); return true} catch(e) {error.value = e.message; message.error(e.message); return false} finally {busy.value = false} }
function back() {const path = String(route.query.back || ''); router.push(path.startsWith('/meeting-management?') ? path : '/meeting-management')}
function archive() {Modal.confirm({title: data.value.meeting.archived ? '恢复会议？' : '归档这场会议？', content: '归档仅影响管理列表，不会取消已有飞书待办或归档助手会话。', onOk: () => run(() => archiveMeeting(id, {version:data.value.meeting.version, archived:!data.value.meeting.archived}))})}
function startEdit() {draft.value = {title:result.value.title, meetingType:result.value.meetingType || '未提供', body:result.value.body}; editing.value = true}
async function saveMinutes() {if(await run(() => editMeetingMinutes(id, {...draft.value, version:current.value.version}))) editing.value = false}
function taskPayload(task) {return {id:task.id, title:task.title, content:task.content || '', assigneeUserId:task.assignee?.userId || null, assigneeFeishuUserId:task.assignee?.feishuUserId || null, dueDate:task.dueDate || null, status:task.status || 'OPEN'}}
async function editTask(task) {
  taskDraft.value = taskPayload(task); taskModal.value = true; directoryError.value = ''; directoryLoading.value = true
  try {const response = await getMeetingDirectory(id); directoryOptions.value = response.users.map(u => ({value:u.feishuUserId,label:`${u.displayName}${u.englishName ? ` · ${u.englishName}` : ''}`})); directoryError.value = response.scopeNotice || ''} catch(e) {directoryError.value = e.message} finally {directoryLoading.value = false}
}
function addTask() {editTask({id:`manual-${crypto.randomUUID()}`, title:'', status:'OPEN'})}
async function saveTask() {
  if(!taskDraft.value.title.trim()) {message.warning('请填写待办标题'); return}
  const items = tasks.value.map(task => task.id === taskDraft.value.id ? {...taskDraft.value, assigneeUserId:null} : taskPayload(task))
  if(!tasks.value.some(task => task.id === taskDraft.value.id)) items.push({...taskDraft.value, assigneeUserId:null})
  if(await run(() => editMeetingTasks(id, {version:current.value.version, tasks:items, action:'SAVE'}))) taskModal.value = false
}
function sendTask(task) {Modal.confirm({title: task.delivery?.feishuTaskId ? '同步修改到原飞书任务？' : '确认待办并发送到飞书？', content:`负责人：${task.assignee?.displayName || '待分配'}；期限：${task.dueDate || '未设期限'}`, onOk: () => taskAction(task,'CONFIRM')})}
function taskAction(task, action) {return run(() => editMeetingTasks(id, {version:current.value.version,tasks:tasks.value.map(taskPayload),action,taskId:task.id}))}
function formalReferences(item) {return item.formalEvidence || (result.value?.formalEvidence || []).filter(ref => item.formalEvidenceIds?.includes(ref.evidence_id))}
let knowledgeSearchId = 0
async function searchKnowledge(q = '') {const request = ++knowledgeSearchId; try {const response = await searchFormalKnowledge(q); if(request === knowledgeSearchId) knowledgeOptions.value = response.items.map(item => ({value:item.id,label:item.title}))} catch(e) {message.error(e.message)}}
function openDecision(item) {decisionItem.value = item; decision.value = {action:['PROCESSING','NEW_SOURCE_DRAFT','REVIEW_REQUESTED'].includes(item.status) ? (item.status === 'REVIEW_REQUESTED' ? 'REQUEST_SOURCE_CHANGE' : item.status) : 'DEFERRED', reason:item.decisionReason || '', knowledgeUnitId:item.knowledgeUnitId || undefined, draftContent:item.draftContent || `${item.title}\n\n${item.reason}`}; decisionModal.value = true; searchKnowledge()}
async function saveDecision() {if(await run(() => decideMeetingKnowledge(id, decisionItem.value.id, {...decision.value,version:current.value.version}))) decisionModal.value = false}
function sourceMatches(sources, query) {
  const matches = []
  sources.forEach((source,index) => (source.paragraphs || []).forEach(p => {
    const ref = `S${index+1}-${p.id}`
    if(query && (ref.toLowerCase() === query.toLowerCase() || p.text?.includes(query))) matches.push({...p,ref,url:source.url})
  }))
  return matches
}
function evidenceTime(item) {return item.timestamp || (item.startMs != null ? `${Math.floor(item.startMs/60000)}分${Math.floor(item.startMs/1000)%60}秒` : '按段落定位')}
function findEvidence() {const all = sourceMatches(current.value?.sources || [], evidenceQuery.value.trim()); evidence.value = all.slice(0,30); evidenceNotice.value = all.length > 30 ? `找到 ${all.length} 处，显示前 30 处，请缩小搜索范围。` : all.length ? '' : '未找到对应原文。'}
async function showTaskEvidence(item, ref) {
  await run(async () => {const response = item.sourceMeetingId && item.sourceMeetingId !== current.value.id ? await getMeetingVersion(id,item.sourceMeetingId) : {meeting:current.value}; preview.value = null; previewEvidence.value = sourceMatches(response.meeting.sources,ref); if(!previewEvidence.value.length) throw new Error('此版本未找到该原文依据'); previewOpen.value = true},false)
}
async function viewVersion(item) {await run(async () => {preview.value = await getMeetingVersion(id,item.id); previewEvidence.value = []; previewOpen.value = true},false)}
async function selectVersion(version) {await run(async () => {preview.value = await getMeetingVersion(id,preview.value.meeting.id,version)},false)}
onBeforeRouteLeave(() => {if(editing.value || taskModal.value || decisionModal.value) {message.warning('请先保存或取消正在编辑的内容'); return false}})
function refreshCurrent() {
  if (!document.hidden && !loading.value && !busy.value && !editing.value && !taskModal.value && !decisionModal.value) load()
}
watch(tab, value => { if (value === 'TASK') refreshCurrent() })
onMounted(() => { load(); window.addEventListener('focus', refreshCurrent); document.addEventListener('visibilitychange', refreshCurrent) })
onBeforeUnmount(() => { window.removeEventListener('focus', refreshCurrent); document.removeEventListener('visibilitychange', refreshCurrent) })
</script>
<style lang="less" src="@/assets/css/meeting-management.less"></style>
