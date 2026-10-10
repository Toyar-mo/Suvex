import { PageHeader } from '@/components/layout/AppShell'
import AccountPanel from '@/components/settings/AccountPanel'
import Ctf2Panel from '@/components/settings/Ctf2Panel'
import PlatformPanel from '@/components/settings/PlatformPanel'
import NotifyPanel from '@/components/settings/NotifyPanel'
import ProviderPanel from '@/components/settings/ProviderPanel'
import SandboxPanel from '@/components/settings/SandboxPanel'
import ScanRetentionPanel from '@/components/settings/ScanRetentionPanel'
import ErrorState from '@/components/ui/ErrorState'
import { SkeletonRows } from '@/components/ui/Skeleton'
import { BACKEND_PENDING, loadSettings, useSettings } from '@/data/settings-store'
import { Database, Lock, ShieldCheck } from 'lucide-react'
import { useEffect, useState } from 'react'

/**
 * Settings 设置页。
 *
 * 版面：可信度说明 → 锚点导航 → 四块独立面板。
 *
 * ★ 为什么是四块**各自独立保存**的面板，而不是一个"保存全部"：
 *   provider、沙箱、通知、账号是四件互不相关的事。
 *   一个全局保存按钮会让用户改了一个开关却不敢点（怕连带提交别的、
 *   怕影响正在跑的扫描），也让"到底哪一块没存上"变得无法回答。
 *   分块保存后，每块的成功/失败都是自己的、可停留的状态。
 *
 * ★ 锚点导航不是装饰：这一页有四个面板、每个都可能是展开的表单，
 *   滚动距离很长。"我在哪一节"和"怎么直接跳到通知"必须随手可得。
 *   它用原生 <a href="#id">，不写 JS 滚动 —— 浏览器自己处理，
 *   键盘、读屏器、右键"在新标签打开"全都天然可用。
 */

/** 面板清单 —— 导航与正文共用一份，避免加了面板忘了加导航 */
const SECTIONS = [
  { id: 'providers', label: '模型服务' },
  { id: 'sandbox', label: '沙箱' },
  // ★ CTF2 紧挨沙箱：两者都在回答"解题跑起来是什么样"，
  //   而它与沙箱共享同一个并发上限（见后端 ctf2/scheduler 的说明）。
  { id: 'ctf2', label: 'CTF2' },
  { id: 'platform', label: '平台接入' },
  { id: 'notify', label: '通知' },
  { id: 'scan-retention', label: '扫描保留' },
  { id: 'account', label: '账号与安全' },
] as const

export function SettingsPage() {
  const { accounts, sandbox, notices, tokens, status, error } = useSettings()

  /*
    进页面拉一次真实设置（任务 7）。

    ⚠️ 不能靠 store 的初始值：它是**分源组装**的（见 settings-store 的
    `loadSettings`），初始快照里 `tokens` 是空的 —— 不拉的话界面上会显示
    "还没有令牌"，而用户明明创建过。把"加载中"和"确实是空的"分开，
    是这一页最容易搞错的地方。
  */
  useEffect(() => {
    void loadSettings()
  }, [])

  return (
    <>
      <PageHeader navId="settings" />

      <div className="px-gutter py-bay">
        <TrustNote />

        {/*
          ★ `sectionsReady` 判据必须是 `status === 'ready'`：
          下面那个三分支渲染里，只有它走"渲染面板"那一支。
          传早了（面板还没进 DOM）观察器会空转，传晚了高亮开始得不及时。
        */}
        <SectionNav sectionsReady={status === 'ready'} />

        <div className="mt-panel flex flex-col gap-bay">
          {status === 'loading' ? (
            /* 骨架而不是转圈：本机后端通常几十毫秒就回，转圈会一闪而过 */
            <SkeletonRows rows={3} rowClassName="h-24" />
          ) : status === 'error' ? (
            <ErrorState
              title="设置加载失败"
              /*
                ★ 直接传 store 里的 `ApiError`，**不要再包一层**。
                  早先这里写的是 `new ApiError('SETTINGS_LOAD_FAILED', error, 0)`
                  —— 那个 `0` 会让 `ErrorState` 把**所有**失败都判成网络错误
                  （0 在本代码库里专指"请求没拿到应答"），于是后端返回 500 时
                  用户看到的是"连接不上后端服务（http://…）"，
                  被引向"去启动服务"这条完全错误的路。
              */
              error={error}
              onRetry={() => void loadSettings()}
            />
          ) : (
            <>
              <ProviderPanel accounts={accounts} />
              <SandboxPanel config={sandbox} />
              {/*
                CTF2 面板**自己读自己的 store**（`data/ctf2.ts`），
                与 `ScanRetentionPanel` 同一个做法 —— 它有独立的端点、
                独立的加载状态与独立的失败原因，塞进上面那个聚合
                store 会让它的失败与别的混在一起，面板也就无法知道
                自己为什么是空的。
              */}
              <Ctf2Panel />
              <PlatformPanel />
              <NotifyPanel rule={notices.rule} channels={notices.channels} />
              {/* 保留规则：属于「系统行为」这一类，与沙箱/通知同类 */}
              <ScanRetentionPanel />
              <AccountPanel tokens={tokens} />
            </>
          )}
        </div>

        <DataBoundaryNote />
      </div>
    </>
  )
}

/**
 * 可信度说明。
 *
 * ★ 这一块是验收要求「让人感到这里是安全的」的落点，但写法上刻意**不是**营销话术：
 *   没有"银行级加密""我们非常重视您的安全"这类无法验证的表述 ——
 *   那种话在安全从业者眼里是负分（说得出这种话的产品通常没做这些事）。
 *
 *   这里只陈述**这个界面实际会做什么**，每一条都能当场验证：
 *     · 密钥不回显 → 填完保存再打开，输入框确实是空的；
 *     · 不出现在报告里 → 报告页确实没有凭据字段；
 *     · 显示是按住才看 → 松开鼠标立刻回到遮蔽。
 *   能被当场验证的承诺才是承诺。
 */
function TrustNote() {
  return (
    <section
      aria-label="凭据处理方式"
      className="mb-panel flex flex-wrap items-start gap-x-panel gap-y-2 rounded-md border border-line bg-surface p-panel"
    >
      <span className="bg-signal-ok/12 flex h-8 w-8 shrink-0 items-center justify-center rounded-pill text-signal-ok">
        <ShieldCheck size={15} strokeWidth={1.75} aria-hidden="true" />
      </span>

      <div className="flex min-w-0 flex-1 flex-col gap-1">
        <h2 className="text-sm font-medium text-ink">凭据在这个界面里的处理方式</h2>
        <ul className="flex flex-col gap-0.5 text-2xs text-ink-muted">
          <li className="flex items-start gap-1.5">
            <span
              aria-hidden="true"
              className="mt-1.5 h-1 w-1 shrink-0 rounded-pill bg-ink-ghost"
            />
            <span>
              已保存的密钥<span className="text-ink">不会回填</span>
              到输入框，界面任何位置都不会完整显示它。
            </span>
          </li>
          <li className="flex items-start gap-1.5">
            <span
              aria-hidden="true"
              className="mt-1.5 h-1 w-1 shrink-0 rounded-pill bg-ink-ghost"
            />
            <span>
              密钥输入时即为遮蔽状态。填入内容后点眼睛图标可查看明文，明文会在
              <span className="text-ink">20 秒后自动隐藏</span>，点到别处也会立即隐藏。
            </span>
          </li>
          <li className="flex items-start gap-1.5">
            <span
              aria-hidden="true"
              className="mt-1.5 h-1 w-1 shrink-0 rounded-pill bg-ink-ghost"
            />
            <span>
              复制仅在明文状态下可用，且剪贴板会在
              <span className="text-ink">2 秒后清空</span>。
            </span>
          </li>
          <li className="flex items-start gap-1.5">
            <span
              aria-hidden="true"
              className="mt-1.5 h-1 w-1 shrink-0 rounded-pill bg-ink-ghost"
            />
            <span>密钥与令牌不进入扫描记录、报告与通知内容。</span>
          </li>
        </ul>
      </div>
    </section>
  )
}

/**
 * 锚点导航。
 *
 * 用 sticky 而不是固定定位：全站是 document 滚动（见 AppShell 的滚动策略），
 * sticky 在文档流里，不会像 fixed 那样需要按侧栏宽度算偏移 ——
 * 那正是批量操作栏当初改用 sticky 的原因。
 *
 * 高亮当前区块用 IntersectionObserver 而不是监听 scroll + 算位置：
 * 后者要读布局（强制同步布局，每帧一次），前者由浏览器在合成阶段完成。
 * 而且它天然处理"一屏里出现多个区块"这种情况。
 */
function SectionNav({ sectionsReady }: { sectionsReady: boolean }) {
  const [active, setActive] = useState<string>(SECTIONS[0].id)

  useEffect(() => {
    /*
      ★★ 必须等面板**真的渲染出来**再观察（v2 修复）。

      这个 effect 原来依赖 `[]`，在 `SectionNav` 挂载那一刻就去
      `document.getElementById(section.id)`。而设置页的数据是**异步**加载的
      （`useSettings()` 的 `status` 初值是 `'loading'`），那一刻
      `SettingsPage` 渲染的是骨架屏，六个面板**一个都不在 DOM 里** ——
      于是 `if (el) observer.observe(el)` **全部静默跳过**，观察器一个
      区块都没观察。而依赖是 `[]`，之后再也不会重跑。

      症状：高亮**永远停在第一项**（"模型服务"），滚到哪儿都不变。

      它与"点导航标签不跳转"是**同一个根因的两半**：
      · 那一半是 `SettingsPanel` 没把 `id` 落到 `<section>` 上
        （已修，见 `SaveBar.tsx`）；
      · 这一半是观察器在元素出现**之前**就放弃了。

      ⚠️ 光修前一半不够 —— 这是实测出来的：`id` 补上、六个锚点都能跳
          之后，高亮仍然一动不动，因为观察器这次是真的什么都没看。

      依赖 `sectionsReady` 而不是 `[]`：面板渲染与否**恰好**由
      `status === 'ready'` 决定（见本文件下面那个三分支渲染），所以它在
      "面板进入 DOM"的那次提交之后变 true，effect 随之重跑 ——
      这时才真的观察得到。
    */
    if (!sectionsReady) return

    const observer = new IntersectionObserver(
      (entries) => {
        /*
          取当前可见且位置最靠上的那个。
          直接用 entries[0] 是错的：回调一次可能带多个条目，
          而它们的顺序不保证是文档顺序 —— 那会让高亮随机跳动。
        */
        const visible = entries
          .filter((e) => e.isIntersecting)
          .sort((a, b) => a.boundingClientRect.top - b.boundingClientRect.top)

        if (visible.length > 0) setActive(visible[0].target.id)
      },
      {
        // 顶部留出状态条 + 导航自身的高度，否则"刚滚进视野"就被判为当前区块
        rootMargin: '-140px 0px -60% 0px',
        threshold: 0,
      },
    )

    for (const section of SECTIONS) {
      const el = document.getElementById(section.id)
      if (el) observer.observe(el)
    }

    return () => observer.disconnect()
  }, [sectionsReady])

  return (
    <nav
      aria-label="设置分区"
      className="sticky top-[52px] z-10 -mx-gutter border-y border-line bg-canvas/85 px-gutter backdrop-blur-sm"
    >
      <ul className="flex gap-1 overflow-x-auto py-1.5">
        {SECTIONS.map((section) => {
          const isActive = active === section.id
          return (
            <li key={section.id} className="shrink-0">
              <a
                href={`#${section.id}`}
                aria-current={isActive ? 'true' : undefined}
                className={
                  isActive
                    ? 'inline-flex h-7 items-center rounded-sm bg-surface-raised px-2.5 text-2xs text-ink'
                    : 'inline-flex h-7 items-center rounded-sm px-2.5 text-2xs text-ink-muted transition-ui duration-fast hover:bg-surface-raised/60 hover:text-ink'
                }
              >
                {section.label}
              </a>
            </li>
          )
        })}
      </ul>
    </nav>
  )
}

/**
 * 数据来源说明。
 *
 * ★ 必须写在页面上，不能只躺在代码注释里。
 *
 *   这一页原来是**整页内存态**（刷新就回到初始值），原文案就是这么说的。
 *   任务 7 接入后端之后，那句话**一半不成立了** —— 令牌与口令现在是真实
 *   持久化的。继续挂旧文案会让用户不信任一个其实已经可靠的功能，
 *   那和"吹嘘没有的能力"是同一种失真，只是方向相反。
 *
 *   反过来，没接入的部分也**必须说**：**通知**的后端接口尚未交付，
 *   它确实还是刷新即失。（沙箱在任务 13 交付后已从这句里移出 ——
 *   它的读写现在都走真实接口了。）
 *   ——「模型服务」曾经也在这句里，但任务 7 之后 provider 与令牌、
 *     登录口令一起都是真实持久化的了，再把它列进来就是在**否认
 *     一个已经可靠的功能**，与吹嘘没有的能力是同一种失真。
 *   让用户以为"沙箱配置已经保存了"是最糟的 ——
 *   他会据此假设计划里的沙箱策略真的生效了。
 *
 *   这与 TopStatusBar 给演示数据打标记是同一条原则：
 *   **不伪造不存在的持久化能力，也不否认已经存在的。**
 */
function DataBoundaryNote() {
  return (
    <div className="mt-bay flex flex-col gap-1 rounded-sm border border-dashed border-line-strong p-3 text-2xs text-ink-faint">
      <p className="flex items-start gap-1.5">
        <Database size={12} strokeWidth={1.75} className="mt-0.5 shrink-0" aria-hidden="true" />
        <span>
          <span className="text-ink-muted">已接入服务端：</span>
          模型服务、访问令牌与登录口令都会真正写到后端并持久化，刷新、换浏览器都还在。
        </span>
      </p>

      {(BACKEND_PENDING.sandbox || BACKEND_PENDING.notices) && (
        <p className="flex items-start gap-1.5">
          <Lock size={12} strokeWidth={1.75} className="mt-0.5 shrink-0" aria-hidden="true" />
          <span>
            <span className="text-ink-muted">尚未接入：</span>
            {BACKEND_PENDING.sandbox && BACKEND_PENDING.notices
              ? '沙箱与通知的后端接口还没交付，这两块目前保存在内存里，'
              : '通知的后端接口还没交付，它目前保存在内存里，'}
            <span className="text-ink">刷新页面会回到初始值</span>。
          </span>
        </p>
      )}

      <p className="flex items-start gap-1.5">
        <Lock size={12} strokeWidth={1.75} className="mt-0.5 shrink-0" aria-hidden="true" />
        <span>
          密钥与令牌<span className="text-ink-muted">不会写入浏览器存储</span>
          （localStorage / Cookie 里都没有）。浏览器存储同源的任何脚本都能读，
          凭据只应提交给服务端保存。
        </span>
      </p>
    </div>
  )
}

export default SettingsPage
