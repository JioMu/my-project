/**
 * APK 智能回归 API（MVP：APK 上传 → REA 分析 → 人工确认 → Midscene → HTML 报告）
 * 后端：apps.ui_automation.apk_views，前缀 /api/ui-automation/apk-regression/
 */
import request from '../utils/api'

const BASE = '/ui-automation/apk-regression'

// ---- APK 制品 ----
export function uploadApk(formData, onProgress) {
  return request({
    url: `${BASE}/artifacts/upload/`,
    method: 'post',
    data: formData,
    headers: { 'Content-Type': 'multipart/form-data' },
    timeout: 10 * 60 * 1000,
    onUploadProgress: onProgress
  })
}
export function getApkArtifacts(params) {
  return request({ url: `${BASE}/artifacts/`, method: 'get', params })
}
export function deleteApkArtifact(id) {
  return request({ url: `${BASE}/artifacts/${id}/`, method: 'delete' })
}

// ---- 运行 ----
export function getApkRuns(params) {
  return request({ url: `${BASE}/runs/`, method: 'get', params })
}
export function createApkRun(data) {
  return request({ url: `${BASE}/runs/`, method: 'post', data })
}
export function getApkRun(id) {
  return request({ url: `${BASE}/runs/${id}/`, method: 'get' })
}
export function updateApkRun(id, data) {
  return request({ url: `${BASE}/runs/${id}/`, method: 'patch', data })
}
export function deleteApkRun(id) {
  return request({ url: `${BASE}/runs/${id}/`, method: 'delete' })
}
export function analyzeApkRun(id) {
  return request({ url: `${BASE}/runs/${id}/analyze/`, method: 'post' })
}
export function approveApkRun(id, data) {
  return request({ url: `${BASE}/runs/${id}/approve/`, method: 'post', data })
}
export function executeApkRun(id, data) {
  return request({ url: `${BASE}/runs/${id}/execute/`, method: 'post', data })
}
/** 强制中止卡住的执行（超时兜底上线前的遗留任务需要这个出口） */
export function abortApkRun(id) {
  return request({ url: `${BASE}/runs/${id}/abort/`, method: 'post' })
}
export function getApkYamlUrl(id, pathIndex) {
  const q = pathIndex === undefined || pathIndex === null ? '' : `?path=${pathIndex}`
  return `/api${BASE}/runs/${id}/yaml/${q}`
}
export function getApkReportUrl(id, download = false) {
  return `/api${BASE}/runs/${id}/report/${download ? '?download=1' : ''}`
}
