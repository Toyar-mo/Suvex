/**
 * 「我的视图」的 query 编解码 —— 纯函数，不碰 React，也不碰网络。
 *
 * ## 为什么视图里存的是**原始 query 串**
 *
 * 另一种做法是存解析后的结构（`{status, q, sort, dir}`），读回来时逐个字段
 * 赋给 state。它更好读，但有个会随时间恶化的毛病：
 *
 * **视图是长期存在的，筛选维度是会变的。**
 *
 * 今天存了 `{"status":"failed","q":"acme"}`，明天加了「只看有高危发现的」
 * 这个新维度，那条老视图的结构里没有这个字段 —— 于是它要么被当成"该维度
 * 未设置"（视图悄悄变了意思），要么整个反序列化失败（视图直接报废）。
 *
 * 存 query 串就没有这个耦合：新增维度只是多一个 key，老串里没有它，
 * 读的时候当作"未设置"即可。**不认识 key 就忽略**，这条规则让视图的
 * 生存期与筛选维度的生存期解耦。
 *
 * ## 为什么不写默认值
 *
 * `encodeViewQuery` 不写 `status=all`、`sort=recent`、`dir=desc` 这类默认值。
 * 视图列表里要显示一句摘要（`describeViewQuery`），塞满默认值会让摘要
 * 退化成噪音，也会让"这条视图到底筛了什么"看不出来。
 * 代价是解码时必须自己兜默认值 —— 在 `decodeViewQuery` 里一次性兜完。
 */

import { SCAN_STATUS_LABEL, type ScanStatus } from '@/data/scan'
import { SORT_LABEL, type SortDir, type SortKey } from '@/lib/scan-query'

/** 视图能表达的筛选维度。新增维度时只加这里，老视图不受影响。 */
export interface ScanViewParams {
  status: ScanStatus | 'all'
  q: string
  sort: SortKey
  dir: SortDir
}

export const DEFAULT_VIEW_PARAMS: ScanViewParams = {
  status: 'all',
  q: '',
  sort: 'recent',
  dir: 'desc',
}

/** `status=all` 不在 `SCAN_STATUS_LABEL` 里（那不是一种状态，是"不筛"）。 */
const ALL_STATUS_LABEL = '全部状态'

const SORT_KEYS = Object.keys(SORT_LABEL) as SortKey[]
const STATUS_VALUES = [...Object.keys(SCAN_STATUS_LABEL), 'all'] as (ScanStatus | 'all')[]

/**
 * 把当前筛选编码成 query 串（不含前导 `?`）。
 *
 * 空视图返回空串 —— 后端 `PUT` 会 `lstrip("?")`，两边约定一致。
 */
export function encodeViewQuery(params: Partial<ScanViewParams>): string {
  const search = new URLSearchParams()

  if (params.status && params.status !== DEFAULT_VIEW_PARAMS.status) {
    search.set('status', params.status)
  }
  const q = params.q?.trim()
  if (q) search.set('q', q)
  if (params.sort && params.sort !== DEFAULT_VIEW_PARAMS.sort) {
    search.set('sort', params.sort)
  }
  if (params.dir && params.dir !== DEFAULT_VIEW_PARAMS.dir) {
    search.set('dir', params.dir)
  }

  return search.toString()
}

/**
 * 解码。
 *
 * ★ 白名单式取值：认不出的 key 直接忽略，认得出但值非法的**回退到默认**。
 *
 * 值非法时回退而不是原样透传，是因为这些值最终会进 `useState` 并参与
 * 比较（`sortKey === 'severity'`）。透传一个垃圾值会让列表"看起来没排序"
 * 而排序控件上显示着一个不存在的选项 —— 那是比回退更难看的状态。
 */
export function decodeViewQuery(query: string): ScanViewParams {
  let search: URLSearchParams
  try {
    search = new URLSearchParams(query.replace(/^\?/, ''))
  } catch {
    // URLSearchParams 的构造几乎不会抛，但真抛了也不该让整个列表页崩掉
    return { ...DEFAULT_VIEW_PARAMS }
  }

  const status = search.get('status') ?? ''
  const sort = search.get('sort') ?? ''
  const dir = search.get('dir') ?? ''

  return {
    status: STATUS_VALUES.includes(status as ScanStatus | 'all')
      ? (status as ScanStatus | 'all')
      : DEFAULT_VIEW_PARAMS.status,
    q: search.get('q') ?? DEFAULT_VIEW_PARAMS.q,
    sort: SORT_KEYS.includes(sort as SortKey) ? (sort as SortKey) : DEFAULT_VIEW_PARAMS.sort,
    dir: dir === 'asc' || dir === 'desc' ? dir : DEFAULT_VIEW_PARAMS.dir,
  }
}

/** 这条视图是否等于「什么都没筛」。菜单里用它把空视图标出来。 */
export function isViewEmpty(query: string): boolean {
  const params = decodeViewQuery(query)
  return (
    params.status === DEFAULT_VIEW_PARAMS.status &&
    params.q === DEFAULT_VIEW_PARAMS.q &&
    params.sort === DEFAULT_VIEW_PARAMS.sort &&
    params.dir === DEFAULT_VIEW_PARAMS.dir
  )
}

/** 当前筛选是否"有东西可存"。空筛选存成视图没有意义。 */
export function hasAnyFilter(params: Partial<ScanViewParams>): boolean {
  return encodeViewQuery(params).length > 0
}

/**
 * 人话摘要，例如 `失败 · 目标含 acme`。
 *
 * 光有视图名不够 —— 用户给视图起名「我的排查视图」之后，一周后自己不记得
 * 它筛了什么。让他点开才知道内容，等于每次都要用一次才敢用。摘要把这件事
 * 摊开在菜单里。
 */
export function describeViewQuery(query: string): string {
  const params = decodeViewQuery(query)
  const parts: string[] = []

  if (params.status !== DEFAULT_VIEW_PARAMS.status) {
    parts.push(
      params.status === 'all' ? ALL_STATUS_LABEL : SCAN_STATUS_LABEL[params.status],
    )
  }
  if (params.q) parts.push(`目标含 ${params.q}`)
  if (params.sort !== DEFAULT_VIEW_PARAMS.sort) {
    parts.push(`${SORT_LABEL[params.sort]}${params.dir === 'asc' ? '升序' : '降序'}`)
  } else if (params.dir !== DEFAULT_VIEW_PARAMS.dir) {
    parts.push(params.dir === 'asc' ? '升序' : '降序')
  }

  return parts.length > 0 ? parts.join(' · ') : '无筛选条件'
}
