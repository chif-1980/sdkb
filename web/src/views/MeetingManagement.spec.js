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
  syncMeetingTasks: vi.fn(), downloadMeeting: vi.fn(), searchFormalKnowledge: vi.fn()
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
  'a-table': table, 'a-tag': passthrough, 'a-tabs': passthrough, 'a-tab-pane': passthrough,
  'a-spin': passthrough, 'a-modal': true, 'a-drawer': true, 'a-list': true,
  'a-input': true, 'a-textarea': true, 'a-select': true, 'a-input-search': true,
  'a-empty': true, 'a-checkbox': true, 'a-pagination': true,
  'a-form': true, 'a-form-item': true, 'a-list-item': true, 'a-date-picker': true
}
async function render(path = '/meeting-management') {
  const router = createRouter({history:createMemoryHistory(), routes:[
    {path:'/meeting-management', component:MeetingManagementView},
    {path:'/meeting-management/:id', component:MeetingManagementDetail}
  ]})
  await router.push(path); await router.isReady()
  const wrapper = mount(defineComponent({template:'<router-view />'}), {global:{plugins:[router],stubs}})
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
