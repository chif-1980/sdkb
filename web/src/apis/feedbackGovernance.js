import { apiGet, apiPatch } from './base'

const path = id => `/api/dashboard/feedbacks/${encodeURIComponent(id)}/workflow`
export const getFeedbackWorkflow = id => apiGet(path(id))
export const updateFeedbackWorkflow = (id, patch) => apiPatch(path(id), patch)
export const getFeedbackAssignees = () => apiGet('/api/dashboard/feedbacks/assignees')
