import { describe, expect, it } from 'vitest'
import type { Scan } from '@/data/scan'
import { scansToCsv } from './scan-export'

describe('scansToCsv', () => {
  it('neutralizes spreadsheet formulas from scan-controlled fields', () => {
    const scan = {
      target: '=HYPERLINK("http://evil.example","x")',
      targetKind: 'web',
      status: 'completed',
      findings: {},
      durationSec: 1,
      startedAt: '2026-01-01T00:00:00Z',
      failureReason: '+1+1',
      origin: 'scan',
      flag: null,
    } as unknown as Scan

    const csv = scansToCsv([scan])
    expect(csv).toContain("'=HYPERLINK")
    expect(csv).toContain("'+1+1")
  })
})
