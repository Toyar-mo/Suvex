import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from 'react'

/**
 * 极简 i18n（v1.90）。
 *
 * ## 为什么不是 i18next / react-intl
 *
 * 现在要翻的只有**新移植的这批界面**（发现优先级、语言菜单，后续的任务页与
 * 系统设置）；老页面仍是中文，硬塞一套运行时只会让"哪些串没翻"更难看清。
 * 一个 40 行的 Context 足够，需要复数/插值/日期本地化时再换 —— 那时替换面
 * 也只有 `t()` 一个函数。
 *
 * ## 默认行为
 *
 * `auto`（跟随浏览器）：`navigator.language` 命中 `zh-TW`/`zh-Hant` 用繁中，
 * 其余中文用简中，非中文一律回落英文 —— 不猜。
 * 选择存 localStorage，键是 `suvex.locale`。
 */

export const LOCALES = [
  { id: 'zh-CN', name: '简体中文', short: '中文', flag: '🇨🇳' },
  { id: 'zh-TW', name: '繁體中文', short: '繁中', flag: '🇹🇼' },
  { id: 'en', name: 'English', short: 'EN', flag: '🇬🇧' },
  { id: 'ru', name: 'Русский', short: 'RU', flag: '🇷🇺' },
  { id: 'fr', name: 'Français', short: 'FR', flag: '🇫🇷' },
  { id: 'ja', name: '日本語', short: 'JA', flag: '🇯🇵' },
  { id: 'de', name: 'Deutsch', short: 'DE', flag: '🇩🇪' },
] as const

export type LocaleId = (typeof LOCALES)[number]['id']
export type LocaleSetting = LocaleId | 'auto'

const STORAGE_KEY = 'suvex.locale'

/** 只放这批新界面的键；老页面仍走中文字面量。 */
type Dict = Record<string, string>

const zhCN: Dict = {
  'lang.label': '界面语言',
  'lang.auto': '跟随浏览器',
  'priority.title': '发现优先级',
  'priority.total': '共 {n} 条未处置',
  'priority.empty': '暂无未处置发现。',
  'priority.p0': '立即处置',
  'priority.p1': '本周处置',
  'priority.p2': '排期修复',
  'priority.p3': '观察',
  'priority.p4': '记录',
  'priority.open': '查看',
  'severity.critical': '严重',
  'severity.high': '高危',
  'severity.medium': '中危',
  'severity.low': '低危',
  'severity.info': '提示',
  'assetMap.zoomHint': 'Ctrl/⌘ + 滚轮缩放',
  'scanStatus.queued': '排队中',
  'scanStatus.running': '运行中',
  'scanStatus.completed': '已完成',
  'scanStatus.failed': '失败',
  'scanStatus.aborted': '已终止',
  'scanPreview.title': '任务预览',
  'settings.title': '系统设置',
  'settings.general': '常规',
  'settings.data': '数据维护',
  'settings.network': '网络限制',
}

const zhTW: Dict = {
  'lang.label': '介面語言',
  'lang.auto': '跟隨瀏覽器',
  'priority.title': '發現優先級',
  'priority.total': '共 {n} 條未處置',
  'priority.empty': '暫無未處置發現。',
  'priority.p0': '立即處置',
  'priority.p1': '本週處置',
  'priority.p2': '排期修復',
  'priority.p3': '觀察',
  'priority.p4': '記錄',
  'priority.open': '查看',
  'assetMap.zoomHint': 'Ctrl/⌘ + 滾輪縮放',
  'severity.critical': '嚴重',
  'severity.high': '高危',
  'severity.medium': '中危',
  'severity.low': '低危',
  'severity.info': '提示',
  'scanStatus.queued': '排隊中',
  'scanStatus.running': '執行中',
  'scanStatus.completed': '已完成',
  'scanStatus.failed': '失敗',
  'scanStatus.aborted': '已終止',
  'scanPreview.title': '任務預覽',
  'settings.title': '系統設定',
  'settings.general': '一般',
  'settings.data': '資料維護',
  'settings.network': '網路限制',
}

const en: Dict = {
  'lang.label': 'Language',
  'lang.auto': 'Follow browser',
  'priority.title': 'Finding priority',
  'priority.total': '{n} unresolved',
  'priority.empty': 'No unresolved findings.',
  'priority.p0': 'Act now',
  'assetMap.zoomHint': 'Ctrl/⌘ + wheel to zoom',
  'priority.p1': 'This week',
  'priority.p2': 'Scheduled',
  'priority.p3': 'Watch',
  'priority.p4': 'Log',
  'priority.open': 'Open',
  'severity.critical': 'Critical',
  'severity.high': 'High',
  'severity.medium': 'Medium',
  'severity.low': 'Low',
  'severity.info': 'Info',
  'scanStatus.queued': 'Queued',
  'scanStatus.running': 'Running',
  'scanStatus.completed': 'Completed',
  'scanStatus.failed': 'Failed',
  'scanStatus.aborted': 'Aborted',
  'scanPreview.title': 'Task preview',
  'settings.title': 'System settings',
  'settings.general': 'General',
  'settings.data': 'Data maintenance',
  'settings.network': 'Network restrictions',
}

const ru: Dict = {
  'lang.label': 'Язык',
  'lang.auto': 'Как в браузере',
  'priority.title': 'Приоритет находок',
  'priority.total': 'Не устранено: {n}',
  'priority.empty': 'Нет неустранённых находок.',
  'priority.p0': 'Срочно',
  'priority.p1': 'На этой неделе',
  'priority.p2': 'В плане',
  'priority.p3': 'Наблюдать',
  'priority.p4': 'Учесть',
  'priority.open': 'Открыть',
  'settings.title': 'Настройки системы',
  'settings.general': 'Общие',
  'settings.data': 'Обслуживание данных',
  'settings.network': 'Сетевые ограничения',
}

const fr: Dict = {
  'lang.label': 'Langue',
  'lang.auto': 'Suivre le navigateur',
  'priority.title': 'Priorité des findings',
  'priority.total': '{n} non traités',
  'priority.empty': 'Aucun finding non traité.',
  'priority.p0': 'Immédiat',
  'priority.p1': 'Cette semaine',
  'priority.p2': 'Planifié',
  'priority.p3': 'Surveiller',
  'priority.p4': 'Consigner',
  'priority.open': 'Ouvrir',
  'settings.title': 'Paramètres système',
  'settings.general': 'Général',
  'settings.data': 'Maintenance des données',
  'settings.network': 'Restrictions réseau',
}

const ja: Dict = {
  'lang.label': '表示言語',
  'lang.auto': 'ブラウザに従う',
  'priority.title': '検出の優先度',
  'priority.total': '未対応 {n} 件',
  'priority.empty': '未対応の検出はありません。',
  'priority.p0': '即時対応',
  'priority.p1': '今週対応',
  'priority.p2': '計画的に修正',
  'priority.p3': '経過観察',
  'priority.p4': '記録',
  'priority.open': '開く',
  'settings.title': 'システム設定',
  'settings.general': '一般',
  'settings.data': 'データ管理',
  'settings.network': 'ネットワーク制限',
}

const de: Dict = {
  'lang.label': 'Sprache',
  'lang.auto': 'Browser folgen',
  'priority.title': 'Priorität der Findings',
  'priority.total': '{n} offen',
  'priority.empty': 'Keine offenen Findings.',
  'priority.p0': 'Sofort handeln',
  'priority.p1': 'Diese Woche',
  'priority.p2': 'Geplant',
  'priority.p3': 'Beobachten',
  'priority.p4': 'Erfassen',
  'priority.open': 'Öffnen',
  'settings.title': 'Systemeinstellungen',
  'settings.general': 'Allgemein',
  'settings.data': 'Datenwartung',
  'settings.network': 'Netzwerkbeschränkungen',
}

const DICTS: Record<LocaleId, Dict> = {
  'zh-CN': zhCN,
  'zh-TW': zhTW,
  en,
  ru,
  fr,
  ja,
  de,
}

/** 浏览器语言 → 支持的语言；非中文一律回落英文（不猜）。 */
export function resolveLocale(setting: LocaleSetting): LocaleId {
  if (setting !== 'auto') return setting
  const nav = typeof navigator === 'undefined' ? 'en' : navigator.language || 'en'
  const lower = nav.toLowerCase()
  if (lower.startsWith('zh')) return lower.includes('tw') || lower.includes('hk') || lower.includes('hant') ? 'zh-TW' : 'zh-CN'
  if (lower.startsWith('ru')) return 'ru'
  if (lower.startsWith('fr')) return 'fr'
  if (lower.startsWith('ja')) return 'ja'
  if (lower.startsWith('de')) return 'de'
  return 'en'
}


/**
 * 导航文案（v1.90）。
 *
 * `id` 是标识符不翻译，只有显示名进字典 —— 与 `navigation.ts` 的约定一致。
 * `navShort.*` 是窄屏底栏用的短名；缺这条时调用方回落到 `nav.*`。
 *
 * 非中英语言只翻「分组 + 高频入口」，其余回落英文（t 的回落链是
 * 当前语言 → en → zh-CN）：宁可给一句英文，也不要把中文糊进德语界面。
 */
const NAV_LABELS: Record<LocaleId, Dict> = {
  'zh-CN': {
    'nav.overview': '总览', 'nav.chat': '对话', 'nav.scans': '扫描任务', 'nav.activity': '智能体活动',
    'nav.findings': '漏洞发现', 'nav.assets': '资产总览', 'nav.schedules': '定时复查', 'nav.ir': '应急响应',
    'nav.kernel': 'CTF', 'nav.reports': '报告', 'nav.credentials': '凭据', 'nav.files': '文件管理',
    'nav.tools': '工具管理', 'nav.intranet': '内网会话', 'nav.approvals': '审批记录', 'nav.models': '模型管理',
    'nav.agents': 'Agent', 'nav.skills': '技能库', 'nav.settings': '设置',
    'navShort.activity': '活动', 'navShort.assets': '资产', 'navShort.schedules': '复查', 'navShort.kernel': 'CTF',
    'navShort.files': '文件', 'navShort.tools': '工具', 'navShort.intranet': '内网', 'navShort.approvals': '审批',
    'navShort.models': '模型', 'navShort.agents': 'Agent',
    'navGroup.run': '运行', 'navGroup.output': '资产与产出', 'navGroup.automation': '自动化', 'navGroup.config': '能力与配置',
  },
  'zh-TW': {
    'nav.overview': '總覽', 'nav.chat': '對話', 'nav.scans': '掃描任務', 'nav.activity': '智慧體活動',
    'nav.findings': '漏洞發現', 'nav.assets': '資產總覽', 'nav.schedules': '定時複查', 'nav.ir': '應變處理',
    'nav.kernel': 'CTF', 'nav.reports': '報告', 'nav.credentials': '憑證', 'nav.files': '檔案管理',
    'nav.tools': '工具管理', 'nav.intranet': '內網工作階段', 'nav.approvals': '審批紀錄', 'nav.models': '模型管理',
    'nav.agents': 'Agent', 'nav.skills': '技能庫', 'nav.settings': '設定',
    'navShort.activity': '活動', 'navShort.assets': '資產', 'navShort.schedules': '複查', 'navShort.kernel': 'CTF',
    'navShort.files': '檔案', 'navShort.tools': '工具', 'navShort.intranet': '內網', 'navShort.approvals': '審批',
    'navShort.models': '模型', 'navShort.agents': 'Agent',
    'navGroup.run': '執行', 'navGroup.output': '資產與產出', 'navGroup.automation': '自動化', 'navGroup.config': '能力與設定',
  },
  en: {
    'nav.overview': 'Overview', 'nav.chat': 'Chat', 'nav.scans': 'Scans', 'nav.activity': 'Agent activity',
    'nav.findings': 'Findings', 'nav.assets': 'Assets', 'nav.schedules': 'Schedules', 'nav.ir': 'Incident response',
    'nav.kernel': 'CTF', 'nav.reports': 'Reports', 'nav.credentials': 'Credentials', 'nav.files': 'Files',
    'nav.tools': 'Tools', 'nav.intranet': 'Intranet sessions', 'nav.approvals': 'Approvals', 'nav.models': 'Models',
    'nav.agents': 'Agents', 'nav.skills': 'Skills', 'nav.settings': 'Settings',
    'navShort.activity': 'Activity', 'navShort.assets': 'Assets', 'navShort.schedules': 'Schedules', 'navShort.kernel': 'CTF',
    'navShort.files': 'Files', 'navShort.tools': 'Tools', 'navShort.intranet': 'Intranet', 'navShort.approvals': 'Approvals',
    'navShort.models': 'Models', 'navShort.agents': 'Agents',
    'navGroup.run': 'Run', 'navGroup.output': 'Output', 'navGroup.automation': 'Automation', 'navGroup.config': 'Config',
  },
  ru: {
    'navGroup.run': 'Работа', 'navGroup.output': 'Результаты', 'navGroup.automation': 'Автоматизация', 'navGroup.config': 'Настройки',
    'nav.overview': 'Обзор', 'nav.chat': 'Чат', 'nav.scans': 'Сканирования', 'nav.findings': 'Находки',
    'nav.assets': 'Активы', 'nav.settings': 'Настройки',
  },
  fr: {
    'navGroup.run': 'Exécution', 'navGroup.output': 'Résultats', 'navGroup.automation': 'Automatisation', 'navGroup.config': 'Configuration',
    'nav.overview': 'Vue d’ensemble', 'nav.chat': 'Discussion', 'nav.scans': 'Analyses', 'nav.findings': 'Findings',
    'nav.assets': 'Actifs', 'nav.settings': 'Paramètres',
  },
  ja: {
    'navGroup.run': '実行', 'navGroup.output': '成果物', 'navGroup.automation': '自動化', 'navGroup.config': '設定',
    'nav.overview': '概要', 'nav.chat': 'チャット', 'nav.scans': 'スキャン', 'nav.findings': '検出',
    'nav.assets': '資産', 'nav.settings': '設定',
  },
  de: {
    'navGroup.run': 'Betrieb', 'navGroup.output': 'Ergebnisse', 'navGroup.automation': 'Automatisierung', 'navGroup.config': 'Konfiguration',
    'nav.overview': 'Übersicht', 'nav.chat': 'Chat', 'nav.scans': 'Scans', 'nav.findings': 'Findings',
    'nav.assets': 'Assets', 'nav.settings': 'Einstellungen',
  },
}

for (const [locale, labels] of Object.entries(NAV_LABELS)) {
  Object.assign(DICTS[locale as LocaleId], labels)
}


/** 新移植界面的文案（阶段 5）。非中英语言回落英文。 */
const UI_LABELS: Record<LocaleId, Dict> = {
  'zh-CN': {
    'scanPreview.aria': '任务预览',
    'scanPreview.empty': '从左侧选择一个任务',
    'scanPreview.emptyHint': '这里会显示它的目标、进度与发现数；完整的执行步骤在「详情」里。',
    'scanPreview.findings': '发现',
    'scanPreview.duration': '用时',
    'scanPreview.kind': '目标类型',
    'scanPreview.open': '查看执行详情',
    'assetMap.hosts': '台主机',
    'assetMap.bleeding': '台有未处置高危',
    'assetMap.ports': '个端口',
    'assetMap.high': '高危',
    'assetMap.center': '工作空间',
    'assetMap.zoomOut': '缩小',
    'assetMap.zoomIn': '放大',
    'assetMap.reset': '重置视图',
    'network.title': '网络限制',
    'network.desc': '对话沙箱的出站目标守卫。命令里出现的目标必须落在这里列出的范围内，否则执行前就被拒绝。',
    'network.loadFailed': '网络限制加载失败',
    'network.enabled': '守卫已启用',
    'network.disabled': '守卫已关闭',
    'network.disableHint': '关闭方式：SUPEX_CHAT_SHELL_SCOPE_GUARD=0（改完重启 API）',
    'network.extra': '运维额外放行',
    'network.extraHint': '来自 SUPEX_CHAT_SHELL_ALLOWED_HOSTS，只该放固定设施（镜像站、内部包仓）',
    'network.extraEmpty': '没有额外放行项',
    'network.builtin': '内置基础设施',
    'network.builtinHint': '包管理镜像、代码托管、公共 DNS 与环回 —— 代码常量，不可在此修改',
    'network.textLevel': '⚠️ 这是文本级守卫：目标藏在变量/base64 里、或域名解析到内网仍能绕过。网络层边界需要按容器源 IP 下发防火墙规则，尚未实现。',
    'data.title': '数据维护',
    'data.desc': '导出与清理。会改数据的操作要求先确认影响范围，这里只摆放已经成立的那一项。',
    'data.export': '导出设置快照',
    'data.exportHint': '这一页正在读到的配置（沙箱、通知、令牌前缀）—— 不含任何明文凭据。',
    'data.exportAction': '导出 JSON',
    'data.purge': '清空业务记录',
    'data.purgeHint': '后端尚未提供该操作。它会删除扫描、漏洞、资产与对话历史 —— 需要先设计范围、确认与回滚，不能在这里假装完成。',
    'data.reset': '恢复初始平台配置',
    'data.resetHint': '同上：只重置配置、不动业务数据的“恢复出厂”需要单独实现，当前只能逐面板手工恢复。',
    'data.unavailable': '暂不可用',
    'data.unavailableHint': '后端尚未提供该端点',
  },
  'zh-TW': {
    'scanPreview.aria': '任務預覽',
    'scanPreview.empty': '從左側選擇一個任務',
    'scanPreview.emptyHint': '這裡會顯示它的目標、進度與發現數；完整的執行步驟在「詳情」裡。',
    'scanPreview.findings': '發現',
    'scanPreview.duration': '耗時',
    'scanPreview.kind': '目標類型',
    'scanPreview.open': '查看執行詳情',
    'assetMap.hosts': '台主機',
    'assetMap.bleeding': '台有未處置高危',
    'assetMap.ports': '個連接埠',
    'assetMap.high': '高危',
    'assetMap.center': '工作空間',
    'assetMap.zoomOut': '縮小',
    'assetMap.zoomIn': '放大',
    'assetMap.reset': '重設視圖',
    'network.title': '網路限制',
    'network.desc': '對話沙箱的對外目標守衛。命令中出現的目標必須落在這裡列出的範圍內，否則執行前就被拒絕。',
    'network.loadFailed': '網路限制載入失敗',
    'network.enabled': '守衛已啟用',
    'network.disabled': '守衛已關閉',
    'network.disableHint': '關閉方式：SUPEX_CHAT_SHELL_SCOPE_GUARD=0（改完重啟 API）',
    'network.extra': '維運額外放行',
    'network.extraHint': '來自 SUPEX_CHAT_SHELL_ALLOWED_HOSTS，只該放固定設施（鏡像站、內部套件庫）',
    'network.extraEmpty': '沒有額外放行項',
    'network.builtin': '內建基礎設施',
    'network.builtinHint': '套件鏡像、程式碼託管、公共 DNS 與環回 —— 程式碼常數，不可在此修改',
    'network.textLevel': '⚠️ 這是文字級守衛：目標藏在變數/base64 裡、或網域解析到內網仍能繞過。網路層邊界需要按容器來源 IP 下發防火牆規則，尚未實作。',
    'data.title': '資料維護',
    'data.desc': '匯出與清理。會改資料的操作要求先確認影響範圍，這裡只擺放已經成立的那一項。',
    'data.export': '匯出設定快照',
    'data.exportHint': '这一页正在讀到的設定（沙箱、通知、權杖前綴）—— 不含任何明文憑證。',
    'data.exportAction': '匯出 JSON',
    'data.purge': '清空業務紀錄',
    'data.purgeHint': '後端尚未提供該操作。它會刪除掃描、漏洞、資產與對話歷史 —— 需要先設計範圍、確認與回滾，不能在這裡假裝完成。',
    'data.reset': '恢復初始平台設定',
    'data.resetHint': '同上：只重設設定、不動業務資料的「恢復出廠」需要單獨實作，目前只能逐面板手動恢復。',
    'data.unavailable': '暫不可用',
    'data.unavailableHint': '後端尚未提供該端點',
  },
  en: {
    'scanPreview.aria': 'Task preview',
    'scanPreview.empty': 'Pick a task on the left',
    'scanPreview.emptyHint': 'Its target, progress and finding count show here; full steps live under “Details”.',
    'scanPreview.findings': 'Findings',
    'scanPreview.duration': 'Duration',
    'scanPreview.kind': 'Target kind',
    'scanPreview.open': 'Open run details',
    'assetMap.hosts': 'hosts',
    'assetMap.bleeding': 'with open high-risk',
    'assetMap.ports': 'ports',
    'assetMap.high': 'high',
    'assetMap.center': 'Workspace',
    'assetMap.zoomOut': 'Zoom out',
    'assetMap.zoomIn': 'Zoom in',
    'assetMap.reset': 'Reset view',
    'network.title': 'Network restrictions',
    'network.desc': 'Egress target guard for the chat sandbox. Targets in a command must fall inside this list, or the command is refused before it runs.',
    'network.loadFailed': 'Failed to load network restrictions',
    'network.enabled': 'Guard enabled',
    'network.disabled': 'Guard disabled',
    'network.disableHint': 'Disable: SUPEX_CHAT_SHELL_SCOPE_GUARD=0 (restart the API after)',
    'network.extra': 'Operator allowlist',
    'network.extraHint': 'From SUPEX_CHAT_SHELL_ALLOWED_HOSTS — only fixed infrastructure (mirrors, internal registries)',
    'network.extraEmpty': 'No extra entries',
    'network.builtin': 'Built-in infrastructure',
    'network.builtinHint': 'Package mirrors, code hosting, public DNS and loopback — code constants, not editable here',
    'network.textLevel': '⚠️ Text-level guard only: targets hidden in variables/base64, or domains resolving to an internal address, still get through. A network boundary requires per-container firewall rules and is not implemented.',
    'data.title': 'Data maintenance',
    'data.desc': 'Export and cleanup. Anything that mutates data must state its blast radius first; only the established action is offered here.',
    'data.export': 'Export settings snapshot',
    'data.exportHint': 'The configuration this page reads (sandbox, notices, token prefixes) — no plaintext credentials.',
    'data.exportAction': 'Export JSON',
    'data.purge': 'Purge business records',
    'data.purgeHint': 'No backend endpoint yet. It would delete scans, findings, assets and chat history — scope, confirmation and rollback must be designed first.',
    'data.reset': 'Restore default platform settings',
    'data.resetHint': 'Same as above: a config-only factory reset needs its own design; for now restore panel by panel.',
    'data.unavailable': 'Unavailable',
    'data.unavailableHint': 'No backend endpoint yet',
  },
  ru: {}, fr: {}, ja: {}, de: {},
}

for (const [locale, labels] of Object.entries(UI_LABELS)) {
  Object.assign(DICTS[locale as LocaleId], labels)
}


/** 任务页文案（阶段 5）。 */
const SCREEN_LABELS: Record<LocaleId, Dict> = {
  'zh-CN': {
    'scans.filter.all': '全部',
    'scans.view.archived': '已归档',
    'scans.view.deleted': '回收站',
    'scans.search.placeholder': '筛选目标',
    'scans.search.aria': '按目标筛选',
    'scans.empty.filtered': '没有符合条件的扫描',
    'scans.empty.clear': '清除筛选条件',
    'scans.empty.none.title': '还没有扫描记录',
    'scans.empty.none.body': '给它一个目标，它会先摸清攻击面、寻找薄弱点，再逐一尝试用可复现的利用方式证明。',
    'scans.empty.none.cta': '发起第一次扫描',
    'scans.empty.status': '这个状态下没有扫描',
    'scans.empty.viewAll': '查看全部',
    'scans.footnote.filtered': '筛选出 {filtered} 条，共 {total} 次扫描 · 累计发现 {findings} 条漏洞',
    'scans.footnote.all': '共 {total} 次扫描 · 累计发现 {findings} 条漏洞',
    'scans.archived.title': '没有已归档的扫描',
    'scans.archived.body': '归档把不再关注的记录从默认列表里移走 —— 数据不会删，随时可以取消归档。可在「设置 → 扫描保留规则」里让失败与已终止的扫描到期自动归档。',
    'scans.trash.title': '回收站是空的',
    'scans.trash.body': '从列表里删除的扫描会先到这里，保留期满后才会被真正清除 —— 保留天数在「设置 → 扫描保留规则」里可调。期间随时可以恢复。',
  },
  'zh-TW': {
    'scans.filter.all': '全部',
    'scans.view.archived': '已封存',
    'scans.view.deleted': '回收筒',
    'scans.search.placeholder': '篩選目標',
    'scans.search.aria': '依目標篩選',
    'scans.empty.filtered': '沒有符合條件的掃描',
    'scans.empty.clear': '清除篩選條件',
    'scans.empty.none.title': '還沒有掃描紀錄',
    'scans.empty.none.body': '給它一個目標，它會先摸清攻擊面、尋找弱點，再逐一嘗試用可重現的方式證明。',
    'scans.empty.none.cta': '發起第一次掃描',
    'scans.empty.status': '這個狀態下沒有掃描',
    'scans.empty.viewAll': '查看全部',
    'scans.footnote.filtered': '篩選出 {filtered} 筆，共 {total} 次掃描 · 累計發現 {findings} 個漏洞',
    'scans.footnote.all': '共 {total} 次掃描 · 累計發現 {findings} 個漏洞',
    'scans.archived.title': '沒有已封存的掃描',
    'scans.archived.body': '封存會把不再關注的紀錄從預設列表移走 —— 資料不會刪，隨時可以取消封存。可在「設定 → 掃描保留規則」裡讓失敗與已終止的掃描到期自動封存。',
    'scans.trash.title': '回收筒是空的',
    'scans.trash.body': '從列表刪除的掃描會先到這裡，保留期滿後才會真正清除 —— 保留天數在「設定 → 掃描保留規則」裡調整。期間隨時可以還原。',
  },
  en: {
    'scans.filter.all': 'All',
    'scans.view.archived': 'Archived',
    'scans.view.deleted': 'Trash',
    'scans.search.placeholder': 'Filter targets',
    'scans.search.aria': 'Filter by target',
    'scans.empty.filtered': 'No scans match the filters',
    'scans.empty.clear': 'Clear filters',
    'scans.empty.none.title': 'No scans yet',
    'scans.empty.none.body': 'Give it a target: it maps the attack surface, looks for weak spots, then proves what it finds with reproducible evidence.',
    'scans.empty.none.cta': 'Start the first scan',
    'scans.empty.status': 'No scans in this state',
    'scans.empty.viewAll': 'Show all',
    'scans.footnote.filtered': '{filtered} of {total} scans · {findings} findings',
    'scans.footnote.all': '{total} scans · {findings} findings',
    'scans.archived.title': 'Nothing archived',
    'scans.archived.body': 'Archiving hides a scan from the default list without deleting it — unarchive any time. Failure and aborted scans can auto-archive after a retention period in Settings → Scan retention.',
    'scans.trash.title': 'Trash is empty',
    'scans.trash.body': 'Deleted scans land here until retention expires, then they are removed for good. The retention window lives in Settings → Scan retention; restore any time before that.',
  },
  ru: {}, fr: {}, ja: {}, de: {},
}

for (const [locale, labels] of Object.entries(SCREEN_LABELS)) {
  Object.assign(DICTS[locale as LocaleId], labels)
}

function readSetting(): LocaleSetting {
  // 默认中文而不是 auto：这是中文优先的产品，老用户升级后界面不该突然变成英文；
  // 想跟随系统的人可以在语言菜单里显式选「跟随浏览器」。
  if (typeof localStorage === 'undefined') return 'zh-CN'
  const stored = localStorage.getItem(STORAGE_KEY)
  if (stored === 'auto') return 'auto'
  if (stored && stored in DICTS) return stored as LocaleId
  return 'zh-CN'
}

export interface I18nValue {
  /** 用户选择（含 `auto`）。 */
  setting: LocaleSetting
  /** 实际生效的语言。 */
  locale: LocaleId
  setSetting: (next: LocaleSetting) => void
  /** 取一条文案；缺失时回落中文，再回落 key 本身。 */
  t: (key: string, vars?: Record<string, string | number>) => string
}

/**
 * 默认值不是 `null`。
 *
 * 顶栏、总览这些组件会被**单独渲染**的测试（以及将来的独立挂载点）用到，
 * 那时没有 Provider。`null` 会让它们整组炸在"必须在 Provider 里"——
 * 而这条约束对"渲染一个卡片"没有任何价值。
 * 缺省回落中文，`setSetting` 是空操作；生产入口 `main.tsx` 一定包了 Provider。
 */
const FALLBACK: I18nValue = {
  setting: 'auto',
  locale: 'zh-CN',
  setSetting: () => {},
  t: (key: string, vars?: Record<string, string | number>) => {
    const raw = zhCN[key] ?? key
    return vars ? raw.replace(/\{(\w+)\}/g, (_, name: string) => String(vars[name] ?? `{${name}}`)) : raw
  },
}

const I18nContext = createContext<I18nValue>(FALLBACK)

export function I18nProvider({ children }: { children: ReactNode }) {
  const [setting, setSettingState] = useState<LocaleSetting>(readSetting)
  const locale = resolveLocale(setting)

  const setSetting = useCallback((next: LocaleSetting) => {
    setSettingState(next)
    if (typeof localStorage !== 'undefined') localStorage.setItem(STORAGE_KEY, next)
  }, [])

  // 跟随浏览器时，系统语言改了要跟着变（桌面端存在这种场景）。
  useEffect(() => {
    if (setting !== 'auto' || typeof window === 'undefined') return
    const onChange = () => setSettingState('auto')
    window.addEventListener('languagechange', onChange)
    return () => window.removeEventListener('languagechange', onChange)
  }, [setting])

  const value = useMemo<I18nValue>(() => {
    const t = (key: string, vars?: Record<string, string | number>) => {
      const raw = DICTS[locale][key] ?? en[key] ?? zhCN[key] ?? key
      if (!vars) return raw
      return raw.replace(/\{(\w+)\}/g, (_, name: string) => String(vars[name] ?? `{${name}}`))
    }
    return { setting, locale, setSetting, t }
  }, [locale, setting, setSetting])

  return <I18nContext.Provider value={value}>{children}</I18nContext.Provider>
}

export function useI18n(): I18nValue {
  return useContext(I18nContext)
}
