/**
 * APK 回归「业务资料」API：操作说明文档 + 项目知识库 + 人工补充
 *
 * 资料是 REA 分析的输入源之一：
 *   只有 APK 清单  → 只能生成"启动/探索"类通用路径
 *   有业务资料     → 能生成针对性路径，且每条标注依据
 */
import request from '../utils/api'

const BASE = '/ui-automation/apk-regression'

export function getProjectOptions() {
  return request({ url: `${BASE}/projects/`, method: 'get' })
}
export function getProjectKnowledge(projectId) {
  return request({ url: `${BASE}/projects/${projectId}/knowledge/`, method: 'get' })
}
export function getRunContext(runId) {
  return request({ url: `${BASE}/runs/${runId}/context/`, method: 'get' })
}
export function updateRunContext(runId, data) {
  return request({ url: `${BASE}/runs/${runId}/context/`, method: 'patch', data })
}
export function uploadContextDoc(runId, file) {
  const fd = new FormData()
  fd.append('file', file)
  return request({
    url: `${BASE}/runs/${runId}/context/upload/`,
    method: 'post',
    data: fd,
    headers: { 'Content-Type': 'multipart/form-data' },
    timeout: 5 * 60 * 1000
  })
}
export function deleteContextDoc(runId, idx) {
  return request({ url: `${BASE}/runs/${runId}/context/docs/${idx}/`, method: 'delete' })
}
export function previewRunContext(runId) {
  return request({ url: `${BASE}/runs/${runId}/context/preview/`, method: 'get' })
}
