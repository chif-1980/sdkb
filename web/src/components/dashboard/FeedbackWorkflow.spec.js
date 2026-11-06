// @vitest-environment jsdom
import { flushPromises, mount } from '@vue/test-utils'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import FeedbackWorkflow from './FeedbackWorkflow.vue'
import * as api from '@/apis/feedbackGovernance'

const user = vi.hoisted(() => ({ userId: 1, manage: true }))
vi.mock('@/stores/user', () => ({ useUserStore: () => ({
  get userId() { return user.userId }, hasPermission: () => user.manage
}) }))
vi.mock('@/apis/feedbackGovernance', () => ({
  getFeedbackWorkflow: vi.fn(), updateFeedbackWorkflow: vi.fn(), getFeedbackAssignees: vi.fn()
}))
const passthrough = { template: '<div><slot /></div>' }
const stubs = {
  'a-modal': { props: ['open'], template: '<section v-if="open"><slot /></section>' },
  'a-spin': passthrough, 'a-form': passthrough, 'a-form-item': passthrough, 'a-space': passthrough,
  'a-alert': { props: ['message'], template: '<p role="alert">{{ message }}</p>' },
  'a-empty': true,
  'a-button': { props: ['disabled','loading'], template: '<button :disabled="disabled || loading"><slot /></button>' },
  'a-select': { props: ['value','options'], emits: ['update:value'], template: `<select :value="value" @change="$emit('update:value', Number($event.target.value))"><option v-for="u in options" :value="u.value">{{ u.label }}</option></select>` },
  'a-textarea': { props: ['value'], emits: ['update:value'], template: `<textarea :value="value" @input="$emit('update:value', $event.target.value)" />` }
}
let wrapper
const initial = { status: 'OPEN', version: 0, history: [], sourceChanged: false }
async function open(workflow = initial) {
  api.getFeedbackWorkflow.mockResolvedValue(workflow)
  wrapper = mount(FeedbackWorkflow, { props: { feedback: { id: 'product:message-1', reason: '引用错误' } }, global: { stubs } })
  await flushPromises()
}
const button = name => wrapper.findAll('button').find(b => b.text() === name)
beforeEach(() => {
  vi.resetAllMocks(); user.userId = 1; user.manage = true
  api.getFeedbackAssignees.mockResolvedValue([{ id: 1, name: '处理人' }, { id: 2, name: '复核人' }])
})
afterEach(() => wrapper?.unmount())

it('指派、处理、提交复核携带版本，处理人不能复核自己', async () => {
  await open()
  expect(button('指派').attributes('disabled')).toBeDefined()
  await wrapper.find('select').setValue('1')
  await wrapper.find('textarea').setValue('核对知识来源')
  api.updateFeedbackWorkflow.mockResolvedValue({ ...initial, status: 'ASSIGNED', version: 1, assigneeId: 1 })
  await button('指派').trigger('click'); await flushPromises()
  expect(api.updateFeedbackWorkflow).toHaveBeenLastCalledWith('product:message-1', { version: 0, action: 'ASSIGN', assigneeId: 1, note: '核对知识来源' })
  await wrapper.find('textarea').setValue('开始核对')
  api.updateFeedbackWorkflow.mockResolvedValue({ ...initial, status: 'PROCESSING', version: 2, assigneeId: 1 })
  await button('开始处理').trigger('click'); await flushPromises()
  await wrapper.find('textarea').setValue('已修正引用，请复核')
  api.updateFeedbackWorkflow.mockResolvedValue({ ...initial, status: 'IN_REVIEW', version: 3, assigneeId: 1, processorId: 1 })
  await button('提交复核').trigger('click'); await flushPromises()
  expect(button('复核通过')).toBeUndefined()
  expect(wrapper.text()).toContain('等待另一位有权限的人员复核')
  expect(wrapper.emitted('updated')).toHaveLength(3)
})

it('独立复核通过后才能关闭，保留操作记录', async () => {
  user.userId = 2
  await open({ ...initial, status: 'IN_REVIEW', version: 3, processorId: 1, assigneeId: 1 })
  expect(button('关闭反馈')).toBeUndefined()
  await wrapper.find('textarea').setValue('对照原文一致')
  const approved = { ...initial, status: 'RESOLVED', version: 4, reviewerId: 2, history: [{ actorName: '复核人', to: 'RESOLVED', note: '对照原文一致', at: '2026-09-27T01:00:00Z' }] }
  api.updateFeedbackWorkflow.mockResolvedValue(approved)
  await button('复核通过').trigger('click'); await flushPromises()
  await wrapper.find('textarea').setValue('确认关闭')
  api.updateFeedbackWorkflow.mockResolvedValue({ ...approved, status: 'CLOSED', version: 5 })
  await button('关闭反馈').trigger('click'); await flushPromises()
  expect(wrapper.text()).toContain('已关闭')
  expect(wrapper.text()).toContain('复核人 · 复核通过')
  expect(button('重新打开')).toBeDefined()
})

it('只读用户不能指派，非负责人不能提交处理结果', async () => {
  user.manage = false
  await open()
  expect(wrapper.find('textarea').exists()).toBe(false)
  expect(api.getFeedbackAssignees).not.toHaveBeenCalled()
  wrapper.unmount(); user.manage = true; user.userId = 2
  await open({ ...initial, status: 'PROCESSING', version: 2, assigneeId: 1 })
  expect(button('提交复核')).toBeUndefined()
})

it('版本冲突时保留说明和状态，供重新核对', async () => {
  await open({ ...initial, status: 'PROCESSING', version: 2, assigneeId: 1 })
  await wrapper.find('textarea').setValue('已修复')
  api.updateFeedbackWorkflow.mockRejectedValue(new Error('反馈已被其他人处理，请刷新后重试'))
  await button('提交复核').trigger('click'); await flushPromises()
  expect(wrapper.find('textarea').element.value).toBe('已修复')
  expect(wrapper.find('[role=alert]').text()).toContain('反馈已被其他人处理')
  expect(wrapper.emitted('updated')).toBeUndefined()
})

it('用户修改反馈后只允许重新打开，避免复核旧内容', async () => {
  user.userId = 2
  await open({ ...initial, status: 'IN_REVIEW', version: 3, processorId: 1, sourceChanged: true })
  expect(button('复核通过')).toBeUndefined()
  expect(button('退回处理')).toBeUndefined()
  expect(button('重新打开')).toBeDefined()
  await wrapper.find('textarea').setValue('根据补充内容重新处理')
  api.updateFeedbackWorkflow.mockResolvedValue({ ...initial, version: 4 })
  await button('重新打开').trigger('click'); await flushPromises()
  expect(wrapper.text()).toContain('待指派')
  expect(wrapper.text()).not.toContain('用户已修改反馈')
})

it('复核退回后回到处理中，原负责人可再次提交', async () => {
  user.userId = 2
  await open({ ...initial, status: 'IN_REVIEW', version: 3, processorId: 1, assigneeId: 1 })
  await wrapper.find('textarea').setValue('引用与原文不一致')
  api.updateFeedbackWorkflow.mockResolvedValue({ ...initial, status: 'PROCESSING', version: 4, assigneeId: 1, processorId: 1, reviewerId: 2 })
  await button('退回处理').trigger('click'); await flushPromises()
  expect(api.updateFeedbackWorkflow).toHaveBeenLastCalledWith('product:message-1', { version: 3, action: 'REJECT', assigneeId: 1, note: '引用与原文不一致' })
  expect(wrapper.text()).toContain('处理中')
  // 退回后负责人保持不变，由原处理人（用户 1）重新提交。
  expect(button('提交复核')).toBeUndefined()
  wrapper.unmount(); user.userId = 1
  await open({ ...initial, status: 'PROCESSING', version: 4, assigneeId: 1, processorId: 1, reviewerId: 2 })
  expect(button('提交复核')).toBeDefined()
  expect(button('复核通过')).toBeUndefined()
})

it('复核人不显示开始处理，处理人提交后不再看到复核入口', async () => {
  user.userId = 2
  await open({ ...initial, status: 'ASSIGNED', version: 1, assigneeId: 1 })
  expect(button('开始处理')).toBeUndefined()
  expect(button('指派')).toBeDefined()
  wrapper.unmount(); user.userId = 1
  await open({ ...initial, status: 'IN_REVIEW', version: 3, processorId: 1, assigneeId: 1 })
  expect(button('复核通过')).toBeUndefined()
  expect(button('退回处理')).toBeUndefined()
  expect(button('关闭反馈')).toBeUndefined()
})
