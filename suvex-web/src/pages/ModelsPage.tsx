import { PageHeader } from '@/components/layout/AppShell'
import ProviderPanel from '@/components/settings/ProviderPanel'
import { ModelRoutesPanel } from '@/components/settings/ModelRoutesPanel'
import { loadSettings, useSettings } from '@/data/settings-store'
import { useEffect } from 'react'

/**
 * 模型管理（v1.83）。
 *
 * 从设置页拆出来的理由：设置页回答“系统怎么跑”，模型接入点与按判据路由
 * 回答“用哪个模型跑” —— 它们会频繁改动（换模型、加 provider），
 * 而设置页的大部分内容改一次就长期不动。
 */
export function ModelsPage() {
  const { accounts } = useSettings()

  useEffect(() => {
    void loadSettings()
  }, [])

  return (
    <>
      <PageHeader navId="models" />
      <div className="flex flex-col gap-bay p-panel">
        <ProviderPanel accounts={accounts} />
        <ModelRoutesPanel accounts={accounts} />
      </div>
    </>
  )
}

export default ModelsPage
