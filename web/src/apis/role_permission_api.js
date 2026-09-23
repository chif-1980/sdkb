import { apiGet, apiPut } from './base'

export const rolePermissionApi = {
  mine: () => apiGet('/api/role-permissions/me'),
  list: () => apiGet('/api/role-permissions'),
  setFeedbackScope: (scope) => apiPut('/api/role-permissions/admin/feedback.view', { scope })
}
