import { apiGet, apiPost, apiPatch } from './base'

const base = '/api/meeting-management'
const path = (id) => `${base}/${encodeURIComponent(id)}`
const query = (params) => new URLSearchParams(Object.entries(params).filter(([, v]) => v !== '' && v != null))
export const listMeetings = (params) => apiGet(`${base}?${query(params)}`)
export const searchFormalKnowledge = (q) => apiGet(`${base}/formal-knowledge?${query({q})}`)
export const meetingMetrics = () => apiGet(`${base}/metrics`)
export const meetingQueue = (kind, params) => apiGet(`${base}/queue/${kind}?${query(params)}`)
export const getManagedMeeting = (id) => apiGet(path(id))
export const getMeetingVersion = (id, run, version) => apiGet(`${path(id)}/runs/${encodeURIComponent(run)}?${query({version})}`)
export const archiveMeeting = (id, data) => apiPatch(`${path(id)}/archive`, data)
export const editMeetingMinutes = (id, data) => apiPatch(`${path(id)}/minutes`, data)
export const editMeetingTasks = (id, data) => apiPatch(`${path(id)}/followup`, data)
export const getMeetingDirectory = (id) => apiGet(`${path(id)}/directory`)
export const decideMeetingKnowledge = (id, item, data) => apiPatch(`${path(id)}/knowledge/${encodeURIComponent(item)}`, data)
export const retryManagedMeeting = (id) => apiPost(`${path(id)}/retry`)
export const cancelManagedMeeting = (id) => apiPost(`${path(id)}/cancel`)
export const syncMeetingTasks = (id) => apiPost(`${path(id)}/sync-tasks`)
export const previewTaskReconciliation = (id, item, data) => apiPost(`${path(id)}/tasks/${encodeURIComponent(item)}/reconcile/preview`, data)
export const confirmTaskReconciliation = (id, item, data) => apiPost(`${path(id)}/tasks/${encodeURIComponent(item)}/reconcile`, data)
export async function downloadMeeting(id, version) {
  const response = await apiGet(`${path(id)}/export?version=${version}`, {}, true, 'blob')
  const url = URL.createObjectURL(await response.blob())
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = `会议纪要-v${version}.docx`
  anchor.click()
  URL.revokeObjectURL(url)
}
