/**
 * 模型路由（v1.81，`GET/PUT /settings/model-routes`）。
 *
 * 形状是 `{goal: providerId}`；没有配置的 goal 回退平台默认。
 */

import { api } from '@/lib/api'

export interface ModelRoutesSnapshot {
  routes: Record<string, string>
}

export async function loadModelRoutes(): Promise<ModelRoutesSnapshot> {
  return api.get<ModelRoutesSnapshot>('/settings/model-routes')
}

export async function saveModelRoutes(
  routes: Record<string, string>,
): Promise<ModelRoutesSnapshot> {
  return api.put<ModelRoutesSnapshot>('/settings/model-routes', { routes })
}
