// @vitest-environment jsdom
import { flushPromises, mount } from '@vue/test-utils'
import { createMemoryHistory, createRouter } from 'vue-router'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { defineComponent } from 'vue'
import MeetingManagementView from './MeetingManagementView.vue'
import MeetingManagementDetail from './MeetingManagementDetail.vue'
import * as api from '@/apis/meetingManagement'

vi.mock('@/apis/meetingManagement', () => ({
  listMeetings: vi.fn(), meetingMetrics: vi.fn(), meetingQueue: vi.fn(),
  getManagedMeeting: vi.fn(), getMeetingVersion: vi.fn(), archiveMeeting: vi.fn(),
  editMeetingMinutes: vi.fn(), editMeetingTasks: vi.fn(), getMeetingDirectory: vi.fn(),
  decideMeetingKnowledge: vi.fn(), retryManagedMeeting: vi.fn(), cancelManagedMeeting: vi.fn(),
  syncMeetingTasks: vi.fn(), downloadMeeting: vi.fn(), searchFormalKnowledge: vi.fn(),
  previewTaskReconciliation: vi.fn(), confirmTaskReconciliation: vi.fn()
}))
vi.mock('@/components/common/MarkdownPreview.vue', () => ({
  default: { props: ['content'], template: '<div>{{ content }}</div>' }
}))
vi.mock('ant-design-vue', () => ({message: {warning: vi.fn(), error: vi.fn()}, Modal: {confirm: vi.fn()}}))

const button = { props: ['disabled'], template: '<button :disabled="disabled"><slot /></button>' }
const passthrough = { template: '<div><slot /></div>' }
const table = {
  props: ['dataSource', 'columns'],
  template: '<section><div v-for="row in dataSource" :key="row.id"><div v-for="column in columns" :key="column.key"><slot name="bodyCell" :record="row" :column="column" /></div></div></section>'
}
const stubs = {
  'a-button': button, 'a-alert': {props:['message','description'], template:'<p>{{ message }} {{ description }}</p>'},
  'a-table': table, 'a-tag': passthrough, 'a-tabs': {name:'a-tabs', template:'<div><slot /></div>'}, 'a-tab-pane': passthrough,
  'a-spin': passthrough, 'a-modal': true, 'a-drawer': true,
  'a-list': {props:['dataSource'], template:'<div><slot v-for="item in dataSource" name="renderItem" :item="item" /></div>'},
  'a-input': true, 'a-textarea': true, 'a-select': true, 'a-input-search': true,
  'a-empty': true, 'a-checkbox': true, 'a-pagination': true,
  'a-form': true, 'a-form-item': true, 'a-list-item': passthrough, 'a-date-picker': true
}
async function render(path = '/meeting-management', extraStubs = {}) {
  const router = createRouter({history:createMemoryHistory(), routes:[
    {path:'/meeting-management', component:MeetingManagementView},
    {path:'/meeting-management/:id', component:MeetingManagementDetail}
  ]})
  await router.push(path); await router.isReady()
  const wrapper = mount(defineComponent({template:'<router-view />'}), {global:{plugins:[router],stubs:{...stubs,...extraStubs}}})
  await flushPromises()
  return {wrapper,router}
}
beforeEach(() => {
  vi.clearAllMocks()
  api.meetingMetrics.mockResolvedValue({pending:2, overdue:0, knowledge:1, errors:0})
  api.listMeetings.mockResolvedValue({items:[{id:'m1',title:'示例会议',state:'completed'}],total:1})
  api.meetingQueue.mockResolvedValue({items:[],total:0})
})
describe('会议管理入口', () => {
  it('保留分页和筛选，再从指标进入对应工作队列', async () => {
    const {wrapper,router} = await render('/meeting-management?q=产品&page=2')
    expect(api.listMeetings).toHaveBeenCalledWith(expect.objectContaining({q:'产品',offset:20}))
    expect(wrapper.text()).toContain('示例会议')
    await wrapper.findAll('button').find(b => b.text().includes('待核对知识建议')).trigger('click')
    await flushPromises()
    expect(router.currentRoute.value.query).toEqual({tab:'KNOWLEDGE',state:'PENDING_MAINTAINER'})
    expect(api.meetingQueue).toHaveBeenCalledWith('KNOWLEDGE', expect.objectContaining({status:'PENDING_MAINTAINER',offset:0}))
    wrapper.unmount()
  })
  it('快速切换时不让旧请求覆盖当前队列', async () => {
    let finish
    api.listMeetings.mockReturnValueOnce(new Promise(resolve => { finish = resolve }))
    api.meetingQueue.mockResolvedValue({items:[{id:'t1',meetingId:'m1',title:'当前待办'}],total:1})
    const {wrapper,router} = await render()
    await router.replace({query:{tab:'TASK'}}); await flushPromises()
    finish({items:[{id:'old',title:'过期响应'}],total:1}); await flushPromises()
    expect(wrapper.text()).toContain('当前待办')
    expect(wrapper.text()).not.toContain('过期响应')
    wrapper.unmount()
  })
  it('失败重跑保留成功结果，保存失败不关闭编辑器', async () => {
    api.getManagedMeeting.mockResolvedValue({
      meeting:{id:'m1',title:'产品会议',state:'failed',version:3,error:{message:'模型暂不可用'}},
      current:{id:'r1',version:2,sources:[],result:{title:'产品会议',body:'已保存的会议正文',followup:{tasks:[],knowledgeSuggestions:[]}}},
      runs:[],events:[]
    })
    api.editMeetingMinutes.mockRejectedValue(new Error('会议版本已变化，请刷新后重试'))
    const {wrapper} = await render('/meeting-management/m1')
    expect(wrapper.text()).toContain('已保存的会议正文')
    expect(wrapper.text()).toContain('模型暂不可用')
    await wrapper.findAll('button').find(b => b.text() === '编辑纪要').trigger('click')
    await wrapper.findAll('button').find(b => b.text() === '保存修改').trigger('click')
    await flushPromises()
    expect(api.editMeetingMinutes).toHaveBeenCalledWith('m1', expect.objectContaining({version:2,body:'已保存的会议正文'}))
    expect(wrapper.text()).toContain('会议版本已变化')
    expect(wrapper.text()).toContain('保存修改')
    wrapper.unmount()
  })
})

const extractedTask = {id:'task-1', title:'核对手册', assigneeSuggestion:'程峰', dueDateSuggestion:'2026年9月21日前', status:'OPEN', reviewStatus:'PENDING'}
function meetingData(tasks) {
  return {meeting:{id:'m1',title:'验收会议',state:'completed'}, current:{id:'m1',version:1,sources:[],result:{body:'原始会议安排',followup:{tasks}}},runs:[],events:[]}
}
const reconciliationStubs = {
  'a-modal': {props:['open','title','okButtonProps'], emits:['ok'], template:'<section v-if="open" role="dialog"><h2>{{ title }}</h2><slot /><button :disabled="okButtonProps?.disabled" @click="$emit(\'ok\')">确认恢复关联</button></section>'},
  'a-form': passthrough, 'a-form-item': passthrough,
  'a-input': {props:['value'], emits:['update:value'], template:'<input :value="value" @input="$emit(\'update:value\', $event.target.value)" />'},
  'a-textarea': {props:['value'], emits:['update:value'], template:'<textarea :value="value" @input="$emit(\'update:value\', $event.target.value)" />'}
}
const reconciliationPreview = {matchBasis:'来源标记', pendingUpdate:true, remote:{id:'remote-1',title:'飞书原任务',content:'原始说明',assigneeIds:['member'],dueDate:'2026-09-21',status:'OPEN'}}
it('先核对再恢复，改编号清除旧核对结果，核对期间不刷新覆盖版本', async () => {
  api.getManagedMeeting.mockResolvedValue(meetingData([{...extractedTask,delivery:{error:'创建结果待核对'}}]))
  api.previewTaskReconciliation.mockResolvedValue(reconciliationPreview)
  api.confirmTaskReconciliation.mockResolvedValue({})
  const {wrapper} = await render('/meeting-management/m1?tab=TASK', reconciliationStubs)
  const click = async text => {await wrapper.findAll('button').find(b => b.text() === text).trigger('click'); await flushPromises()}
  await click('核对飞书原任务')
  expect(wrapper.text()).not.toContain('确认并发送')
  const confirm = () => wrapper.findAll('button').find(b => b.text() === '确认恢复关联')
  expect(confirm().element.disabled).toBe(true)
  const calls = api.getManagedMeeting.mock.calls.length
  window.dispatchEvent(new Event('focus')); await flushPromises()
  expect(api.getManagedMeeting).toHaveBeenCalledTimes(calls)
  await wrapper.get('input[aria-label="飞书原任务链接或编号"]').setValue('remote-1')
  await click('读取核对')
  expect(wrapper.text()).toContain('本地安排与飞书任务有差异')
  expect(confirm().element.disabled).toBe(true)
  await wrapper.get('textarea[aria-label="核对说明"]').setValue('已核对来源和负责人')
  expect(confirm().element.disabled).toBe(false)
  await wrapper.get('input[aria-label="飞书原任务链接或编号"]').setValue('remote-2')
  expect(confirm().element.disabled).toBe(true)
  expect(wrapper.text()).not.toContain('来源核验通过')
  await click('读取核对')
  await click('确认恢复关联')
  expect(api.confirmTaskReconciliation).toHaveBeenCalledWith('m1','task-1',{version:1,feishuTaskId:'remote-1',reason:'已核对来源和负责人'})
  expect(wrapper.find('[role="dialog"]').exists()).toBe(false)
  expect(api.editMeetingTasks).not.toHaveBeenCalled()
  wrapper.unmount()
})
it('确认时版本冲突保留核对说明并要求重新读取，不自动重发', async () => {
  api.getManagedMeeting.mockResolvedValue(meetingData([{...extractedTask,delivery:{error:'待核对'}}]))
  api.previewTaskReconciliation.mockResolvedValue(reconciliationPreview)
  api.confirmTaskReconciliation.mockRejectedValue(new Error('会议版本已变化，请刷新后重新核对'))
  const {wrapper} = await render('/meeting-management/m1?tab=TASK', reconciliationStubs)
  const click = async text => {await wrapper.findAll('button').find(b => b.text() === text).trigger('click'); await flushPromises()}
  await click('核对飞书原任务')
  await wrapper.get('input[aria-label="飞书原任务链接或编号"]').setValue('remote-1')
  await click('读取核对')
  await wrapper.get('textarea[aria-label="核对说明"]').setValue('核对说明')
  await click('确认恢复关联')
  expect(wrapper.get('[role="dialog"]').text()).toContain('会议版本已变化')
  expect(wrapper.findAll('button').find(b => b.text() === '确认恢复关联').element.disabled).toBe(true)
  expect(api.editMeetingTasks).not.toHaveBeenCalled()
  wrapper.unmount()
})
it('待办详情和队列均保留原文识别的安排，不冒充已分配或已执行', async () => {
  api.getManagedMeeting.mockResolvedValue(meetingData([extractedTask]))
  api.meetingQueue.mockResolvedValue({items:[{...extractedTask,meetingId:'m1'}],total:1})
  for (const path of ['/meeting-management/m1?tab=TASK', '/meeting-management?tab=TASK']) {
    const {wrapper} = await render(path)
    expect(wrapper.text()).toContain('程峰（会议识别，待核对）')
    expect(wrapper.text()).toContain('2026年9月21日前（会议识别，待核对）')
    expect(wrapper.text()).toContain('待确认后跟进')
    expect(wrapper.text()).not.toContain('未设期限')
    wrapper.unmount()
  }
})
it('切回页面读取助手保存的最新安排，编辑期间不刷新覆盖草稿', async () => {
  api.getManagedMeeting.mockResolvedValue(meetingData([extractedTask]))
  const {wrapper} = await render('/meeting-management/m1?tab=TASK')
  api.getManagedMeeting.mockResolvedValue(meetingData([{...extractedTask, assignee:{displayName:'实际负责人'}, dueDate:'2026-09-22',status:'IN_PROGRESS',reviewStatus:'CONFIRMED'}]))
  window.dispatchEvent(new Event('focus')); await flushPromises()
  expect(wrapper.text()).toContain('负责人：实际负责人')
  expect(wrapper.text()).toContain('期限：2026-09-22')
  expect(wrapper.text()).toContain('执行状态：进行中')
  expect(wrapper.text()).not.toContain('程峰（会议识别')
  await wrapper.findAll('button').find(b => b.text() === '编辑纪要').trigger('click')
  const calls = api.getManagedMeeting.mock.calls.length
  window.dispatchEvent(new Event('focus')); await flushPromises()
  expect(api.getManagedMeeting).toHaveBeenCalledTimes(calls)
  expect(wrapper.text()).toContain('保存修改')
  wrapper.unmount()
  window.dispatchEvent(new Event('focus')); await flushPromises()
  expect(api.getManagedMeeting).toHaveBeenCalledTimes(calls)
})
it('从纪要进入待办时读取最新状态，刷新仅查询而不发送飞书任务', async () => {
  api.getManagedMeeting.mockResolvedValue(meetingData([extractedTask]))
  const {wrapper} = await render('/meeting-management/m1')
  api.getManagedMeeting.mockResolvedValue(meetingData([{...extractedTask,status:'DONE',reviewStatus:'CONFIRMED'}]))
  wrapper.findComponent(MeetingManagementDetail).findComponent({name:'a-tabs'}).vm.$emit('update:activeKey','TASK')
  await flushPromises()
  expect(wrapper.text()).toContain('执行状态：已完成')
  expect(api.editMeetingTasks).not.toHaveBeenCalled()
  expect(api.syncMeetingTasks).not.toHaveBeenCalled()
  wrapper.unmount()
})
it('处理记录显示待办操作、操作人和失败结果', async () => {
  api.getManagedMeeting.mockResolvedValue({...meetingData([]),events:[{
    action:'TASK_RESEND', actorUserId:7, createdAt:'2026-09-20T00:00:00Z',
    detail:{actorName:'测试管理员',outcome:'FAILED',message:'飞书待办同步失败，请重试。'}
  }]})
  const {wrapper} = await render('/meeting-management/m1')
  expect(wrapper.text()).toContain('重试飞书同步')
  expect(wrapper.text()).toContain('操作人：测试管理员')
  expect(wrapper.text()).toContain('处理失败')
  expect(wrapper.text()).toContain('飞书待办同步失败，请重试。')
  wrapper.unmount()
})

it('区分安排差异和旧核对记录，展示两边值且不自动提交', async () => {
  const delivery = {feishuTaskId:'remote-1',syncStatus:'SYNCED',scheduleCheckedAt:'2026-09-20T01:00:00Z',scheduleComparison:{
    status:'DIFFERENT',differences:['assignee','dueDate'],localAssignees:[{id:'local',name:'张工'}],remoteAssignees:[{id:'remote',name:'王工'}],localDueDate:'2026-09-21',remoteDueDate:'2026-09-22'
  }}
  api.getManagedMeeting.mockResolvedValue(meetingData([{...extractedTask, delivery}]))
  const {wrapper} = await render('/meeting-management/m1?tab=TASK')
  const panel = wrapper.get('[aria-label="飞书安排核对"]')
  expect(panel.text()).toContain('安排有差异')
  expect(panel.text()).toContain('本地：张工')
  expect(panel.text()).toContain('飞书：王工')
  expect(panel.text()).toContain('本地：2026-09-21')
  expect(panel.text()).toContain('飞书：2026-09-22')
  expect(panel.get('a').attributes('href')).toBe('https://applink.feishu.cn/client/todo/detail?guid=remote-1')
  api.getManagedMeeting.mockResolvedValue(meetingData([{...extractedTask, delivery:{...delivery,syncStatus:'FAILED',syncError:'读取超时'}}]))
  window.dispatchEvent(new Event('focus')); await flushPromises()
  expect(panel.text()).toContain('上次核对记录')
  expect(panel.text()).toContain('当前尚未重新核实')
  expect(api.editMeetingTasks).not.toHaveBeenCalled()
  wrapper.unmount()
})
