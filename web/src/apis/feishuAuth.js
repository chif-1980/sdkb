import { apiGet } from './base'

async function request(path, data) {
  const response = await fetch(`/api/auth/feishu/manager/${path}`, data ? {
    method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(data)
  } : {})
  const body = await response.json()
  if (!response.ok) throw new Error(body.detail || '飞书登录暂不可用')
  return body
}
export const feishuLoginConfig = () => request('config')
export const feishuIdentity = () => apiGet('/api/auth/feishu/manager/identity')
export async function startFeishuLogin() {
  if (!globalThis.crypto?.subtle) throw new Error('飞书登录需要 HTTPS 安全地址；本机开发请使用 localhost 或 127.0.0.1')
  const verifier = Array.from(crypto.getRandomValues(new Uint8Array(32)), b => b.toString(16).padStart(2, '0')).join('')
  const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(verifier))
  const challenge = Array.from(new Uint8Array(digest), b => b.toString(16).padStart(2, '0')).join('')
  const result = await request('login', {origin: window.location.origin, challenge})
  sessionStorage.setItem('feishu_login_verifier', verifier)
  window.location.assign(result.login_url)
}
export async function finishFeishuLogin(code) {
  const verifier = sessionStorage.getItem('feishu_login_verifier')
  sessionStorage.removeItem('feishu_login_verifier')
  if (!verifier) throw new Error('登录浏览器已变化，请从知枢登录页重新发起')
  return request('exchange', {code, verifier})
}
