/**
 * CTF2 组件的统一出口。
 *
 * 有它不是"为了好看"：`pages/Kernel.tsx` 要用其中四个，
 * 逐个 import 会让那一页的头部多出四行路径 —— 而路径本身
 * 是这一页最不需要读者关心的东西。
 *
 * ⚠️ `EnvironmentStatus` 与 `SandboxProfileSelect` 也被
 * `settings/KernelPanel.tsx` 用到（默认镜像选择器），所以这里是
 * 它们唯一的出口，不要在页面里直接深引 `./ctf2/Xxx`。
 */
export { default as ChallengeCard } from './ChallengeCard'
export { default as CompetitionPicker } from './CompetitionPicker'
export { default as EnvironmentStatus } from './EnvironmentStatus'
export { default as FlagConfirmDialog } from './FlagConfirmDialog'
export { default as SandboxProfileSelect } from './SandboxProfileSelect'
/**
 * CTF2 的题解弹窗（v0.36）。
 *
 * ⚠️ 它与 `components/scans/WriteupDialog` **同名但不同物** —— 那条走
 * 扫描报告（strix），这条走 CTF2 自己的解题记录（见各自文件头）。
 * 两个入口共用的是外壳 `components/writeup/WriteupDialogShell`，
 * 不是这个文件。
 */
export { default as WriteupDialog } from './WriteupDialog'
/**
 * 处置建议工单弹窗（v0.41，应急响应复盘）。
 *
 * 与题解**共用外壳** `WriteupDialogShell`（三态、下载按钮、失败说明都一样），
 * 只有取数端点与两句文案不同 —— 见文件头那张对比表。
 */
export { default as WorkOrderDialog } from './WorkOrderDialog'
