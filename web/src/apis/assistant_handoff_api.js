import { apiPost } from './base'

export const assistantHandoffApi = {
  createLink() {
    return apiPost('/api/auth/assistant/handoff', {})
  }
}
