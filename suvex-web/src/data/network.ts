import { api } from '@/lib/api'

/**
 * 对话沙箱出站白名单（v1.90）。
 *
 * 只读：白名单里"运维额外放行"的那部分来自环境变量
 * （`SUPEX_CHAT_SHELL_ALLOWED_HOSTS`），属于部署决策 ——
 * 见后端 `NetworkRestrictionsOut` 的说明。这里只负责把**当前生效值**
 * 摊开，让用户能回答"为什么这个目标能访问 / 为什么不能"。
 */
export interface NetworkRestrictions {
  guardEnabled: boolean
  allowedHosts: string[]
  allowedCidrs: string[]
  builtinHosts: string[]
  builtinCidrs: string[]
}

export async function loadNetworkRestrictions(): Promise<NetworkRestrictions> {
  return api.get<NetworkRestrictions>('/settings/network')
}
