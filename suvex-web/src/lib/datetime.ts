/** 把 ISO 时间转成"3 分钟前"这种扫视友好的人话。 */
export function formatRelativeTime(iso: string, now: number = Date.now()): string {
  const then = new Date(iso).getTime()
  if (!Number.isFinite(then)) return '—'

  const diffSec = Math.round((now - then) / 1000)
  if (diffSec < 10) return '刚刚'
  if (diffSec < 60) return `${diffSec} 秒前`

  const diffMin = Math.floor(diffSec / 60)
  if (diffMin < 60) return `${diffMin} 分钟前`

  const diffHour = Math.floor(diffMin / 60)
  if (diffHour < 24) return `${diffHour} 小时前`

  const diffDay = Math.floor(diffHour / 24)
  if (diffDay < 7) return `${diffDay} 天前`

  // 超过一周给绝对日期，相对时间在那一档已经没有信息量
  return new Date(iso).toLocaleDateString('zh-CN', { month: 'numeric', day: 'numeric' })
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

  const total = Math.round(seconds)
  if (total < 60) return `${total} 秒`

  const minutes = Math.floor(total / 60)
  const remainderSec = total % 60

  if (minutes < 60) {
    return remainderSec === 0 ? `${minutes} 分` : `${minutes} 分 ${remainderSec} 秒`
  }

  const hours = Math.floor(minutes / 60)
  const remainderMin = minutes % 60
  return remainderMin === 0 ? `${hours} 小时` : `${hours} 小时 ${remainderMin} 分`
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
