import type { LocaleId } from '../index'

/** 每页/每面板一个字典文件，避免并行改动同一个巨型文件。 */
export type ExtraLocaleMap = Record<LocaleId, Record<string, string>>

// 由各页面的字典文件汇总；新增文件时在这里加一行。
import { settings_rest } from './settings-rest'
import { reports } from './reports'
import { activity_tools } from './activity-tools'
import { inventory } from './inventory'
import { ja_chunk1 } from './ja-chunk1'
import { ja_chunk2 } from './ja-chunk2'
import { ja_chunk3 } from './ja-chunk3'
import { ja_chunk4 } from './ja-chunk4'
import { ja_chunk5 } from './ja-chunk5'
import { ja_chunk6 } from './ja-chunk6'
import { ja_chunk7 } from './ja-chunk7'
import { ja_chunk8 } from './ja-chunk8'



/** 整个站点的日语补充词条：由 8 个分片合并，只覆盖 ja。 */
const jaAll = Object.assign(
  {},
  ja_chunk1, ja_chunk2, ja_chunk3, ja_chunk4,
  ja_chunk5, ja_chunk6, ja_chunk7, ja_chunk8,
)
const jaExtra: ExtraLocaleMap = {
  'zh-CN': {},
  'zh-TW': {},
  en: {},
  ru: {},
  fr: {},
  ja: jaAll,
  de: {},
}

export const EXTRAS: ExtraLocaleMap[] = [settings_rest, reports, activity_tools, inventory, jaExtra]
