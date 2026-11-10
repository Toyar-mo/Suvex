/** 运行中扫描的重复动作（v1.83，`GET /scans/stalled`）。 */

import { api } from '@/lib/api'

export interface StalledScan {
  scanId: string
  name: string
  target: string
  repeats: number
  sample: string
}

export async function loadStalledScans(): Promise<StalledScan[]> {
  return api.get<StalledScan[]>('/scans/stalled')
}
