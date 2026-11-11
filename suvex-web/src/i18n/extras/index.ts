import type { LocaleId } from '../index'

/** 每页/每面板一个字典文件，避免并行改动同一个巨型文件。 */
export type ExtraLocaleMap = Record<LocaleId, Record<string, string>>

// 由各页面的字典文件汇总；新增文件时在这里加一行。
import { settings_rest } from './settings-rest'
import { reports } from './reports'
import { activity_tools } from './activity-tools'
import { inventory } from './inventory'

export const EXTRAS: ExtraLocaleMap[] = [settings_rest, reports, activity_tools, inventory]
