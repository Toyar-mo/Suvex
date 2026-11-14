import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'
import type { KernelEnvironment } from '@/data/kernel'
import { EnvironmentStatus } from './EnvironmentStatus'

afterEach(cleanup)

function env(accessUrl: string): KernelEnvironment {
  return {
    id: 'env_1',
    challengeId: 'chal_1',
    status: 'ready',
    accessUrl,
    accessUrls: [accessUrl],
    pollCount: 1,
    lastError: null,
    expiresAt: null,
  } as unknown as KernelEnvironment
}

describe('EnvironmentStatus access URL', () => {
  it('does not render a javascript: URL as a link', () => {
    render(<EnvironmentStatus environment={env('javascript:alert(1)')} />)
    expect(screen.queryByRole('link')).toBeNull()
    expect(screen.getByText('javascript:alert(1)')).toBeTruthy()
  })

  it('renders http(s) URLs as external links', () => {
    render(<EnvironmentStatus environment={env('https://target.example')} />)
    expect(screen.getByRole('link').getAttribute('href')).toBe('https://target.example')
  })
})
