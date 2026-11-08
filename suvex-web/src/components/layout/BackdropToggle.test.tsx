import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'

import { BackdropToggle } from './BackdropToggle'
import { initBackdrop } from '@/data/backdrop-store'
import { BACKDROP_STORAGE_KEY } from '@/lib/backdrop'

/*
  ## 为什么测这个

  背景是整屏的，选错档的代价是**每次打开都被一个不喜欢的底铺满**。
  三条判据：

  1. 选了要真的写进 `<html data-backdrop>` —— CSS 只认这个属性；
  2. 要存住 —— 下次打开还是这一档（存的 key 与 `lib/backdrop` 同一处）；
  3. 当前档要打勾 —— 否则用户看不出"我现在是哪一档"。

  ⚠️ 三个坑与项目其它组件测试一致：手动 cleanup（没开 globals）、
     不用 jest-dom 的匹配器、菜单要靠可访问名定位。
*/
beforeEach(() => {
  window.localStorage.clear()
  delete document.documentElement.dataset.backdrop
  initBackdrop()
})

afterEach(cleanup)

describe('BackdropToggle', () => {
  it('按钮说得出当前是哪一档', () => {
    render(<BackdropToggle />)

    expect(screen.getByRole('button', { name: /背景：点阵场/ })).not.toBeNull()
  })

  it('★ 选「光晕」→ 写进 data-backdrop 并持久化', () => {
    render(<BackdropToggle />)

    fireEvent.click(screen.getByRole('button', { name: /背景：/ }))
    fireEvent.click(screen.getByRole('menuitemradio', { name: /光晕/ }))

    expect(document.documentElement.dataset.backdrop).toBe('aurora')
    expect(window.localStorage.getItem(BACKDROP_STORAGE_KEY)).toBe('aurora')
  })

  it('选完菜单关掉，再打开时当前项打勾', () => {
    render(<BackdropToggle />)

    fireEvent.click(screen.getByRole('button', { name: /背景：/ }))
    fireEvent.click(screen.getByRole('menuitemradio', { name: /纸纹/ }))
    expect(screen.queryByRole('menu')).toBeNull()

    fireEvent.click(screen.getByRole('button', { name: /背景：/ }))
    const checked = screen.getByRole('menuitemradio', { name: /纸纹/, checked: true })
    expect(checked).not.toBeNull()
  })

  it('四档都在菜单里（漏一档 = 那一档永远选不到）', () => {
    render(<BackdropToggle />)

    fireEvent.click(screen.getByRole('button', { name: /背景：/ }))

    expect(screen.getAllByRole('menuitemradio')).toHaveLength(4)
  })

  it('Esc 关掉菜单', () => {
    render(<BackdropToggle />)

    fireEvent.click(screen.getByRole('button', { name: /背景：/ }))
    fireEvent.keyDown(document, { key: 'Escape' })

    expect(screen.queryByRole('menu')).toBeNull()
  })
})
