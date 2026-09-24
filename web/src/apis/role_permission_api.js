import { apiDelete, apiGet, apiPost, apiPut } from './base'

export const rolePermissionApi = {
  preview: (userId) => apiGet(`/api/role-permissions/users/${userId}/preview`),
  mine: () => apiGet('/api/role-permissions/me'),
  list: () => apiGet('/api/role-permissions'),
  create: (data) => apiPost('/api/role-permissions', data),
  update: (roleKey, data) => apiPut(`/api/role-permissions/${roleKey}`, data),
  remove: (roleKey) => apiDelete(`/api/role-permissions/${roleKey}`),
  setRolePermissions: (roleKey, data) =>
    apiPut(`/api/role-permissions/${roleKey}/permissions`, data),
  setUserRoles: (userId, role_keys, expected_role_keys) =>
    apiPut(`/api/role-permissions/users/${userId}/roles`, { role_keys, expected_role_keys }),
  setDepartmentRoles: (departmentId, role_keys, expected_role_keys) =>
    apiPut(`/api/role-permissions/feishu-departments/${departmentId}/roles`, {
      role_keys,
      expected_role_keys
    })
}
