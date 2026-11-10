import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'
import NotifyPanel from './NotifyPanel'
import type { NoticeRule, NotifyChannel } from '@/data/settings'

const rule: NoticeRule = {
  minSeverity: 'high',
  notifyOnScanComplete: false,
  notifyOnCriticalImmediate: true,
}

afterEach(() => cleanup())

describe('NotifyPanel', () => {
  it('提供邮件 / Webhook / Slack 三种新增入口', () => {
    render(<NotifyPanel rule={rule} channels={[]} />)
    expect(screen.getByRole('button', { name: 'Webhook' })).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Slack' })).toBeTruthy()
    expect(screen.getByRole('button', { name: '邮件' })).toBeTruthy()
  })

  it('邮件渠道正常显示，不再标注不支持', () => {
    const channel: NotifyChannel = {
      id: 'ntc_old',
      kind: 'email',
      label: '安全组',
      target: 'security@example.com',
      enabled: true,
      verified: false,
      verifyError: null,
    }
    render(<NotifyPanel rule={rule} channels={[channel]} />)
    expect(screen.queryByText('不支持发送')).toBeNull()
  })
})
