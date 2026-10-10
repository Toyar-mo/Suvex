/**
 * 漏洞严重度定义 —— 全站单一事实来源。
 *
 * 用 CVSS 的语系（critical/high/medium/low），不自创分级：
 * 安全从业者看到 "high" 时的预期是稳定的，自创一套只会增加翻译成本。
 *
 * 配色遵守 design-system §2：signal 色只表达**结论**，只落在徽标、色块、
 * 图元上，不做大面积背景。info（灰蓝）留给 informational。
 */

export type Severity = 'critical' | 'high' | 'medium' | 'low' | 'info'

/** 严重度从高到低的稳定顺序 —— 图表、表格、图例都必须用它，别各写一份 */
export const SEVERITY_ORDER: Severity[] = ['critical', 'high', 'medium', 'low', 'info']

export interface SeverityMeta {
  id: Severity
  label: string
  /** Tailwind 类名（bg-*）。用于图表图元、状态点 */
  bg: string
  /** Tailwind 类名（text-*）。用于徽标文字与图例标签 */
  text: string
  /** 1px 边框（border-*），用于需要轮廓的图元 */
  border: string
  /** 图表里用的实色 hex —— 见下方说明，图元不用透明度堆叠 */
  fill: string
  /** 一句话说明这一级意味着什么 —— 界面上用 title，不铺在页面上 */
  definition: string
}

/**
 * 色值取自 tailwind.config.js 的 signal 系列。
 *
 * 关于 fill 为什么是实色而不是 `bg-signal-risk/65`：
 * 用透明度区分 critical 和 high，在暗底上会把两者都压成接近的暗红，
 * 色觉障碍用户和在暗屏上的人分不出来。这里改用**两个明确的实色**，
 * 亮度差足够大。而且严重度本来就带文字标签（Critical / High），
 * 颜色是冗余编码而不是唯一载体 —— 这是刻意的。
 *
 * low 用中性色而不是新增一个色相：五级五种颜色会让图表变成彩虹，
 * 而 low 本来就是"可以等"的东西，用中性色是信息，不是偷懒。
 */
export const SEVERITY_META: Record<Severity, SeverityMeta> = {
  critical: {
    id: 'critical',
    label: '严重',
    bg: 'bg-signal-risk',
    text: 'text-signal-risk',
    border: 'border-signal-risk',
    fill: '#F07A6A',
    definition: '无需任何前置条件即可利用，应当按已发生的事件处置。',
  },
  high: {
    id: 'high',
    label: '高危',
    bg: 'bg-[#C2544A]',
    text: 'text-[#E08C82]',
    border: 'border-[#C2544A]',
    fill: '#C2544A',
    definition: '在真实条件下可直接利用。',
  },
  medium: {
    id: 'medium',
    label: '中危',
    bg: 'bg-signal-warn',
    text: 'text-signal-warn',
    border: 'border-signal-warn',
    fill: '#E8B454',
    definition: '可被利用，但需要一个攻击者通常无法控制的前置条件。',
  },
  low: {
    id: 'low',
    label: '低危',
    bg: 'bg-ink-faint',
    text: 'text-ink-muted',
    border: 'border-ink-faint',
    // ⚠️ 必须与 bg-ink-faint 同值。这个 fill 是给**图表图元**用的实色，
    //    而列表里的色点走 bg-ink-faint（CSS 变量）—— 两者一旦分叉，
    //    同一个「低危」在列表和总览图表里会是两个灰。
    //    改 ink-faint 时漏改这里的后果就是这样：色点 #8A909C / 图表条 #7A808C。
    fill: '#8A909C',
    definition: '加固类问题，单独看直接影响很小。',
  },
  info: {
    id: 'info',
    label: '提示',
    bg: 'bg-signal-info',
    text: 'text-signal-info',
    border: 'border-signal-info',
    fill: '#6FA8F5',
    definition: '仅为观察项，未证明存在实际影响。',
  },
}

/** 验证状态的人话描述 —— 定义在 data/finding.ts，与漏洞模型放在一起 */
export { VERIFICATION_LABEL, type Verification } from './finding'
