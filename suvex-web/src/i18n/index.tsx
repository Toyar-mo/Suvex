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
  'priority.p1': 'This week',
  'priority.p2': 'Scheduled',
  'priority.p3': 'Watch',
  'priority.p4': 'Log',
  'priority.open': 'Open',
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

function readSetting(): LocaleSetting {
  if (typeof localStorage === 'undefined') return 'auto'
  const stored = localStorage.getItem(STORAGE_KEY)
  if (stored === 'auto') return 'auto'
  if (stored && stored in DICTS) return stored as LocaleId
  return 'auto'
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
      const raw = DICTS[locale][key] ?? zhCN[key] ?? key
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
