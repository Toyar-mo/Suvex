import { getActiveLocale } from '@/i18n/locale-store'

/** 把 ISO 时间转成"3 分钟前"这种扫视友好的人话（跟随界面语言）。 */
export function formatRelativeTime(iso: string, now: number = Date.now()): string {
  const then = new Date(iso).getTime()
  if (!Number.isFinite(then)) return '—'

  const locale = getActiveLocale()
  const rtf = new Intl.RelativeTimeFormat(locale, { numeric: 'auto', style: 'short' })
  const diffSec = Math.round((now - then) / 1000)

  // 用 Intl 而不是手写单位表：7 种语言的复数、词序、空格规则都在平台里，
  // 手写一张表只会漏掉某一种语言的格变化。
  if (diffSec < 60) return diffSec < 10 ? rtf.format(0, 'second') : rtf.format(-diffSec, 'second')
  const diffMin = Math.floor(diffSec / 60)
  if (diffMin < 60) return rtf.format(-diffMin, 'minute')
  const diffHour = Math.floor(diffMin / 60)
  if (diffHour < 24) return rtf.format(-diffHour, 'hour')
  const diffDay = Math.floor(diffHour / 24)
  if (diffDay < 7) return rtf.format(-diffDay, 'day')

  // 超过一周给绝对日期，相对时间在那一档已经没有信息量
  return new Date(iso).toLocaleDateString(locale, { month: 'numeric', day: 'numeric' })
}

/**
 * 秒 → "1 小时 12 分" / "4 分 30 秒" / "38 秒"
 *
 * 刻意保留两级单位：扫描耗时跨越秒到小时，只给总秒数（"4330 秒"）无法扫视，
 * 只给小时又会把 4 分钟的扫描显示成 "0 小时"。
 *
 * 中文单位比英文长（"14 分" vs "14m"），所以在窄卡片里要注意：
 * 指标卡的 "平均扫描耗时" 因此用 "14 分" 而非 "14 分钟"——
 * 后者在 320px 下会把卡片标题挤到换行。
 */
export function formatDuration(seconds: number | null): string {
  if (seconds === null || !Number.isFinite(seconds) || seconds < 0) return '—'

  const locale = getActiveLocale()
  const unit = (value: number, name: 'second' | 'minute' | 'hour') =>
    new Intl.NumberFormat(locale, { style: 'unit', unit: name, unitDisplay: 'short' }).format(value)
  // 中日文单位紧贴数字，拉丁文字需要一个空格（"50m 24s"）
  const sep = locale.startsWith('zh') || locale.startsWith('ja') ? '' : ' '

  const total = Math.round(seconds)
  if (total < 60) return unit(total, 'second')

  const minutes = Math.floor(total / 60)
  const remainderSec = total % 60
  if (minutes < 60) {
    return remainderSec === 0
      ? unit(minutes, 'minute')
      : `${unit(minutes, 'minute')}${sep}${unit(remainderSec, 'second')}`
  }

  const hours = Math.floor(minutes / 60)
  const remainderMin = minutes % 60
  return remainderMin === 0
    ? unit(hours, 'hour')
    : `${unit(hours, 'hour')}${sep}${unit(remainderMin, 'minute')}`
}

/**
 * 指标卡专用的紧凑时长：只保留一级单位。
 *
 * 为什么需要一个单独的：指标卡的数值用 text-3xl（32px），
 * "1 小时 12 分" 在这个字号下会把卡片撑破。紧凑版给 "72 分"，
 * 精确到分钟已经足够回答"一次扫描大概多久"这个问题。
 */
export function formatDurationCompact(seconds: number | null): string {
  if (seconds === null || !Number.isFinite(seconds) || seconds < 0) return '—'

  const total = Math.round(seconds)
  if (total < 60) return `${total} 秒`
  if (total < 3600) return `${Math.round(total / 60)} 分`

  const hours = total / 3600
  // 超过 10 小时就不显示小数了，精度没有意义
  return hours >= 10 ? `${Math.round(hours)} 小时` : `${hours.toFixed(1)} 小时`
}
