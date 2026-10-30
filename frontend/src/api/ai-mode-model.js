/**
 * AI 智能模式专属模型配置 API
 *
 * ⚠️ 与「配置中心 → AI 模式」(/configuration/ai-mode) 是**两套独立数据**：
 *   - 这里读写 ui_ai_mode_model_configs，仅供 AI 智能模式（APK 回归 / Midscene）使用
 *   - 那边读写 ai_model_config（AIModelConfig），供需求分析等其它功能使用
 * 互不影响：在这里启用某模型不会禁用那边正在用的 analyzer。
 */
import request from '../utils/api'

const BASE = '/ui-automation/ai-mode-models'

export function getRoleMeta() {
  return request({ url: `${BASE}/roles/`, method: 'get' })
}
export function listConfigs(params) {
  return request({ url: `${BASE}/`, method: 'get', params })
}
export function createConfig(data) {
  return request({ url: `${BASE}/`, method: 'post', data })
}
export function updateConfig(id, data) {
  return request({ url: `${BASE}/${id}/`, method: 'patch', data })
}
export function deleteConfig(id) {
  return request({ url: `${BASE}/${id}/`, method: 'delete' })
}
export function testUnsaved(data) {
  return request({ url: `${BASE}/test/`, method: 'post', data, validateStatus: () => true })
}
export function testSaved(id) {
  return request({ url: `${BASE}/${id}/test/`, method: 'post', validateStatus: () => true })
}
export function getActiveConfigs() {
  return request({ url: `${BASE}/active/`, method: 'get' })
}
