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
  it('不再提供「邮件」新增入口，只保留 Webhook / Slack', () => {
    render(<NotifyPanel rule={rule} channels={[]} />)
    expect(screen.getByRole('button', { name: 'Webhook' })).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Slack' })).toBeTruthy()
    expect(screen.queryByRole('button', { name: '邮件' })).toBeNull()
  })

  it('历史邮件渠道仍可见，并明确标注不支持发送', () => {
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
    expect(screen.getByText('不支持发送')).toBeTruthy()
  })
})
