import { afterEach, describe, expect, it } from 'vitest'

import {
  BACKDROP_STORAGE_KEY,
  DEFAULT_BACKDROP,
  applyBackdrop,
  isBackdropId,
  readStoredBackdrop,
} from './backdrop'

/*
  ## 为什么测这个

  这一层的失败方式全是**静默**的：读错存储 → 回到默认（用户以为选择没保存）；
  写了非法值 → `<html data-backdrop="x">` 一个样式都不匹配，页面看上去就是纯色，
  而控制台一片安静。三条判据都不抛异常，只能靠测试钉住。
*/
afterEach(() => {
  window.localStorage.clear()
  delete document.documentElement.dataset.backdrop
})

describe('backdrop 偏好', () => {
  it('没存过时是默认档（点阵场）', () => {
    expect(readStoredBackdrop()).toBe(DEFAULT_BACKDROP)
    expect(DEFAULT_BACKDROP).toBe('field')
  })

  it('存过合法值就取它', () => {
    window.localStorage.setItem(BACKDROP_STORAGE_KEY, 'aurora')
    expect(readStoredBackdrop()).toBe('aurora')
  })

  it('★ 存了非法值（手改/旧版本）回到默认，而不是原样返回', () => {
    window.localStorage.setItem(BACKDROP_STORAGE_KEY, 'glitter')
    expect(readStoredBackdrop()).toBe(DEFAULT_BACKDROP)
  })

  it('isBackdropId 只认那四档', () => {
    expect(isBackdropId('grain')).toBe(true)
    expect(isBackdropId('plain')).toBe(true)
    expect(isBackdropId('')).toBe(false)
    expect(isBackdropId(null)).toBe(false)
  })

  it('applyBackdrop 写到 <html data-backdrop>（CSS 就认这个选择器）', () => {
    applyBackdrop('grain')
    expect(document.documentElement.dataset.backdrop).toBe('grain')
  })
})
