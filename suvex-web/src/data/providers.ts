import { useProjectedProviders } from './settings-store'

/**
 * LLM Provider 的**消费侧投影** —— Settings 页与新建扫描向导共用。
 *
 * ★ 这个文件曾经是一份独立的常量（MOCK_PROVIDERS），`useProviders()` 直接返回它。
 *   那在设置页可编辑之后就成了一个 bug：用户新增/修改了 provider，
 *   向导读的还是那份写死的常量，"新配置的模型会自动出现"变成空头承诺。
 *   而这句话是这个文件顶部自己写下的需求。
 *
 *   现在投影的唯一来源是 settings-store 里的账号列表 ——
 *   改一处，设置页与向导同时变（这是 design-system §10
 *   「跨页数据一致性（硬要求）」的直接应用）。
 *
 * ⚠️ 这里**只有投影**，没有凭据。
 *    apiKey / baseUrl 留在 settings 层，不进入这个结构 ——
 *    向导只需要知道"这个能不能用"，把密钥带进它会扩大密钥的流转范围
 *    （它会被渲染、可能被截图、被打印）。分工见 data/settings.ts。
 *
 * ★ `configured` 由"有没有**可用**凭据"判断 —— 只看填没填 key 是不够的，
 *   还要那个 key 真的有效。
 *
 *   任务 7 之前这一条是"用账号上的 `verified` 字段模拟"的；现在 `verified`
 *   是**真的**：它来自 `POST /providers/{id}/verify` 实际打过去的那一次探测
 *   （失败也返回 200，带 `status:'failed'`，见契约 §3.2）。
 *
 *   ⚠️ 因此"改了 key 必须作废 verified"这条规则仍然成立，而且更重要了：
 *      一个从未验证过的新 key 如果沿用旧的 `verified: true`，
 *      向导会把它当成可用模型列出来，错误要到"扫描跑到一半认证失败"才暴露。
 *      落地就是 `saveProviderAccount` 里的 `keyChanged` 判断。
 */

export type ProviderKind = 'cloud' | 'local'

export interface ProviderConfig {
  id: string
  /** 品牌名不翻译 —— 译成中文反而不可检索 */
  label: string
  /** 模型标识符，与配置文件里的值对齐，不翻译 */
  model: string
  kind: ProviderKind
  /** 是否已配置完成（有可用凭据） */
  configured: boolean
  /** 未配置时说明缺什么。configured 为 true 时忽略 */
  missing: string | null
  /** 这个 provider 的特点，帮助用户选 —— 不是营销文案，是取舍说明 */
  note: string
}

/**
 * 读当前可用的 provider 列表。
 *
 * 名字保留 useProviders 而不是改成 useProjectedProviders：
 * 调用方（向导、状态条）不需要知道数据从哪来，改名只会制造一次无意义的改动。
 */
export function useProviders(): ProviderConfig[] {
  return useProjectedProviders()
}

/** 默认选中的 provider —— 第一个可用的 */
export function defaultProviderId(providers: ProviderConfig[]): string | null {
  return providers.find((p) => p.configured)?.id ?? null
}
