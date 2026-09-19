const labels = {
  pending: '等待处理', running: '分析中', completed: '已完成', failed: '分析失败', cancelled: '已取消',
  PENDING: '待确认', CONFIRMED: '已确认', IGNORED: '已忽略', DELIVERY_FAILED: '发送异常',
  OPEN: '待开始', IN_PROGRESS: '进行中', DONE: '已完成', OVERDUE: '已逾期',
  PROCESSING: '建议草稿处理中', NEW_SOURCE_DRAFT: '待补回原文', REQUEST_SOURCE_CHANGE: '提交原文修改审核', REVIEW_REQUESTED: '已进入审核流程', PENDING_MAINTAINER: '待维护人员核对', COVERED: '已有知识覆盖', DEFERRED: '暂缓处理', REJECTED: '不采纳',
  UNVERIFIED: '尚未完成比对', NEEDS_UPDATE: '建议修改', NEW_TOPIC: '建议新增', NEW_KNOWLEDGE: '建议新增',
  ANALYSIS_COMPLETED: '分析完成', EDIT: '内容更新', ARCHIVE: '归档会议', RESTORE: '恢复会议',
  KNOWLEDGE_DECISION: '知识建议处理', TASK_SYNC: '读取飞书状态', SYNCED: '已同步', FAILED: '同步失败', SYNC_FAILED: '飞书同步异常', NOT_SENT: '尚未发送'
}
export const label = value => labels[value] || value || '未提供'
export const formatDate = value => value ? new Date(value).toLocaleString('zh-CN', {hour12:false}) : '未提供'
export const stateColor = state => ({completed:'green', failed:'red', running:'blue', pending:'orange'})[state] || 'default'
export const safeSourceUrl = value => /^https?:\/\//i.test(value || '') ? value : undefined
