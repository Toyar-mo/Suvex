/**
 * CTF2 组件的统一出口。
 *
 * 有它不是"为了好看"：`pages/Ctf2.tsx` 要用其中四个，
 * 逐个 import 会让那一页的头部多出四行路径 —— 而路径本身
 * 是这一页最不需要读者关心的东西。
 *
 * ⚠️ `EnvironmentStatus` 与 `SandboxProfileSelect` 也被
 * `settings/Ctf2Panel.tsx` 用到（默认镜像选择器），所以这里是
 * 它们唯一的出口，不要在页面里直接深引 `./ctf2/Xxx`。
 */
export { default as ChallengeCard } from './ChallengeCard'
export { default as CompetitionPicker } from './CompetitionPicker'
export { default as EnvironmentStatus } from './EnvironmentStatus'
export { default as FlagConfirmDialog } from './FlagConfirmDialog'
export { default as SandboxProfileSelect } from './SandboxProfileSelect'
