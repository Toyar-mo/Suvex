import { PageHeader } from '@/components/layout/AppShell'
import AccountPanel from '@/components/settings/AccountPanel'
import KernelPanel from '@/components/settings/KernelPanel'
import MembersPanel from '@/components/settings/MembersPanel'
import PlatformPanel from '@/components/settings/PlatformPanel'
import NetworkPanel from '@/components/settings/NetworkPanel'
import NotifyPanel from '@/components/settings/NotifyPanel'
import { BenchmarkCasesPanel } from '@/components/settings/BenchmarkCasesPanel'
import { BenchmarkPanel } from '@/components/settings/BenchmarkPanel'
import { PurplePanel } from '@/components/settings/PurplePanel'
import SandboxPanel from '@/components/settings/SandboxPanel'
import ScanRetentionPanel from '@/components/settings/ScanRetentionPanel'
import UsersPanel from '@/components/settings/UsersPanel'
import ErrorState from '@/components/ui/ErrorState'
import { SkeletonRows } from '@/components/ui/Skeleton'
import { isAdmin, useAuth } from '@/data/auth-store'
import { cn } from '@/lib/cn'
import { loadSettings, useSettings } from '@/data/settings-store'
import { loadUsers } from '@/data/users-store'
import { Database, Lock, ShieldCheck } from 'lucide-react'
import { useEffect, useMemo, useState } from 'react'

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
  { id: 'sandbox', label: '沙箱' },
  { id: 'network', label: '网络限制' },
  // ★ v0.60：原来的一节「CTF」拆成两节 —— 「CTF 解题」只放只影响解题的设置，
  //   「内核通用能力」放对所有场景共用的设置（默认镜像 / 回合预算 / 备用模型 /
  //   应急响应场景包 / 多智能体编排）。拆的理由：后一半改的是**内核行为**，
  //   跟 CTF 无关 —— 混在一节里会让人以为关掉它们就是"不跑 CTF 了"。
  { id: 'ctf', label: 'CTF 解题' },
  { id: 'kernel-core', label: '内核通用能力' },
  { id: 'platform', label: '平台接入' },
  { id: 'notify', label: '通知' },
  { id: 'scan-retention', label: '扫描保留' },
  { id: 'benchmark', label: '评测快照' },
  { id: 'benchmark-cases', label: '评测用例' },
  { id: 'purple', label: '紫队规则' },
  /*
    ★ v1.79：「处理人」补上。

    后端 `/members` 的增删改从任务 10 就在，漏洞页的「指派」下拉与
    筛选也在读它 —— 但设置页一直没有这一节。于是"一个处理人都没有"时，
    指派对话框提示「去设置 → 处理人里加一个」，那个入口**不存在**。
    这一节就是那个入口。

    它与「用户」是两份名单：处理人可以是不登录的外包/对接人，
    登录账号也不一定被指派漏洞（见 `MembersPanel` 文件头）。
  */
  { id: 'members', label: '处理人' },
  { id: 'account', label: '账号与安全' },
  // ★ v0.24：用户管理放在**最后**。
  //
  //   理由：它改的是"谁能进这个系统"，而上面那些改的是"系统怎么跑"。
  //   把权限相关的放最后，符合"越靠后越少动"的阅读习惯 ——
  //   而这一节确实是设置页里最不该随手点的一节。
  //
  //   ⚠️ 它对 viewer **不显示**（见下面的 `isAdmin(user)` 判断）。
  //      导航里也据此过滤，否则会出现"点了导航但那一节不存在"。
  { id: 'users', label: '用户' },
] as const

export function SettingsPage() {
  const { sandbox, notices, tokens, status, error } = useSettings()
  const { user } = useAuth()

  /*
    进页面拉一次真实设置（任务 7）。

    ⚠️ 不能靠 store 的初始值：它是**分源组装**的（见 settings-store 的
    `loadSettings`），初始快照里 `tokens` 是空的 —— 不拉的话界面上会显示
    "还没有令牌"，而用户明明创建过。把"加载中"和"确实是空的"分开，
    是这一页最容易搞错的地方。

    ★ 用户列表**只对管理员拉**（v0.24）：`GET /auth/users` 对
      viewer 返回 403，而为一个必然失败的请求制造一个错误状态
      会让界面上出现一块莫名其妙的红字 —— 而他本来就不该看到这一节。
  */
  useEffect(() => {
    void loadSettings()
    if (isAdmin(user)) void loadUsers()
  }, [user])

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
        {/*
          ★ v1.78 版面：两栏（左侧分区栏 + 右侧内容）。

          原来是一条横向标签条压在内容上方。九个分区挤成一条之后：
            · 每项只能写两三个字，"内核通用能力"被迫缩成"内核通用能力"；
            · 它还要和顶部的状态条抢横向空间，窄屏直接横向滚动；
            · 而设置页真正需要的"我现在在哪一节、还剩哪几节"反而看不出来。

          左栏是竖向的，能容下完整名字，还能标出当前节；内容列因此
          多出约 40px 的垂直空间（标签条那一条没了）。

          ⚠️ 窄屏（<lg）仍然用横向条：竖栏会占掉三分之一宽度，
             而这一页在小屏上本来就是"一节一节往下翻"的读法。
        */}
        <div className="mt-panel grid gap-bay lg:grid-cols-[168px_minmax(0,1fr)] lg:gap-bay">
          <SectionNav sectionsReady={status === 'ready'} />

          <div className="flex min-w-0 flex-col gap-bay">
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
                <SandboxPanel config={sandbox} />
                <NetworkPanel />
                {/*
                CTF2 面板**自己读自己的 store**（`data/kernel.ts`），
                与 `ScanRetentionPanel` 同一个做法 —— 它有独立的端点、
                独立的加载状态与独立的失败原因，塞进上面那个聚合
                store 会让它的失败与别的混在一起，面板也就无法知道
                自己为什么是空的。
              */}
                <KernelPanel />
                <PlatformPanel />
                <NotifyPanel rule={notices.rule} channels={notices.channels} />
                {/* 保留规则：属于「系统行为」这一类，与沙箱/通知同类 */}
                <ScanRetentionPanel />
                <BenchmarkPanel />
                <BenchmarkCasesPanel />
                <PurplePanel />
                <MembersPanel />
                <AccountPanel tokens={tokens} />
                {/*
                ★ 用户管理**只对管理员显示**（v0.24）。

                与上面那些面板不同，它读的是**自己的 store**
                （`data/users-store.ts`），因为用户列表与设置快照是
                两个来源、两个加载状态 —— 而且它有独立的失败原因
                （viewer 调它必然 403）。

                ⚠️ `isAdmin` 判断只是**展示性**的：真正的拦截在后端
                （`require_admin_session`，机器令牌一律拒绝）。
                前端藏起来是为了避免"看到却点不动" —— 那不是安全边界，
                而且不能当安全边界用（改个 DOM 就绕过去了）。
              */}
                {isAdmin(user) && <UsersPanel />}
              </>
            )}
          </div>
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
      className="mb-panel flex items-start gap-3 rounded-md border border-line bg-surface px-4 py-3"
    >
      <span className="bg-signal-ok/12 flex h-7 w-7 shrink-0 items-center justify-center rounded-pill text-signal-ok">
        <ShieldCheck size={14} strokeWidth={1.75} aria-hidden="true" />
      </span>

      <div className="flex min-w-0 flex-1 flex-col gap-1">
        {/*
          ★ v1.78：四条要点压成一行，细节收进 <details>。

          原来这里是四行项目符号，占掉首屏约 110px —— 而它回答的是
          "我把 token 粘进来安全吗"这个**一次性**问题。第一次看完之后，
          每次进设置页都要再滚过它一遍。压成一句结论 + 一个"细节"，
          答案还在，代价从 110px 降到 44px。
        */}
        <p className="text-xs text-ink">
          凭据只提交给服务端<span className="text-ink-muted">：界面不回显、不进报告、不写日志</span>
        </p>
        <p className="text-2xs text-ink-faint">
          保存后输入框始终为空 · 明文只在你按住时显示，20 秒后自动隐藏 · 剪贴板 2 秒后清空
        </p>

        <details className="group mt-0.5">
          <summary className="w-fit cursor-pointer list-none text-2xs text-ink-faint underline-offset-2 hover:text-ink hover:underline">
            这些承诺在这里怎么兑现
          </summary>
          <ul className="mt-1.5 flex flex-col gap-1 text-2xs text-ink-muted">
            <li>已保存的密钥不会回填到输入框，界面任何位置都不会完整显示它。</li>
            <li>
              密钥输入时即为遮蔽状态；点眼睛图标可看明文，明文 20 秒后自动隐藏，点到别处立即隐藏。
            </li>
            <li>复制仅在明文状态下可用，且剪贴板会在 2 秒后清空。</li>
            <li>密钥与令牌不进入扫描记录、报告与通知内容。</li>
          </ul>
        </details>
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
  const { user } = useAuth()
  const [active, setActive] = useState<string>(SECTIONS[0].id)

  /*
    ★ 导航要按角色过滤（v0.24）。

    非管理员看不到「用户」这一项 —— 否则会出现"点了导航，
    但滚到那儿什么都没有"（因为 `UsersPanel` 对他不渲染）。
    而那种"导航项指向空气"的体验比没有这一项更让人困惑。

    ⚠️ 用 `useMemo` 而不是每次渲染 `.filter()`：下面那个 effect 的
       依赖里有 `sections`，每帧新建数组会让它反复重跑观察器。
  */
  const sections = useMemo(() => SECTIONS.filter((s) => s.id !== 'users' || isAdmin(user)), [user])

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

    for (const section of sections) {
      const el = document.getElementById(section.id)
      if (el) observer.observe(el)
    }

    return () => observer.disconnect()
  }, [sectionsReady, sections])

  return (
    <nav
      aria-label="设置分区"
      className={cn(
        // 窄屏：顶部横向条（与改动前一致）
        'sticky top-[52px] z-10 -mx-gutter border-y border-line bg-canvas/85 px-gutter backdrop-blur-sm',
        // 宽屏：左侧竖栏，跟着滚动吸顶，只占 168px
        'lg:sticky lg:top-[64px] lg:z-0 lg:mx-0 lg:self-start lg:border-0 lg:bg-transparent lg:px-0 lg:backdrop-blur-none',
      )}
    >
      <ul className="flex gap-1 overflow-x-auto py-1.5 lg:flex-col lg:gap-0.5 lg:overflow-visible lg:py-0">
        {sections.map((section) => {
          const isActive = active === section.id
          return (
            <li key={section.id} className="shrink-0">
              <a
                href={`#${section.id}`}
                aria-current={isActive ? 'true' : undefined}
                className={cn(
                  'flex h-7 shrink-0 items-center rounded-sm px-2.5 text-2xs transition-ui duration-fast',
                  // 宽屏：左侧 2px 竖条标记当前项（比整块底色安静，也不抢内容）
                  'lg:h-8 lg:rounded-none lg:border-l-2 lg:px-2.5',
                  isActive
                    ? 'bg-surface-raised text-ink lg:border-accent lg:bg-transparent'
                    : 'text-ink-muted hover:bg-surface-raised/60 hover:text-ink lg:border-transparent lg:hover:bg-transparent lg:hover:text-ink',
                )}
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
 *   反过来，没接入的部分也**必须说**（这条规矩仍然成立）。
 *   而到目前为止**没有**未接入的区块了：沙箱（任务 13）、通知（v0.22）、
 *   模型服务（任务 7）都走真实接口并持久化。
 *   ——「模型服务」曾经也在这句里，但任务 7 之后 provider 与令牌、
 *     登录口令一起都是真实持久化的了，再把它列进来就是在**否认
 *     一个已经可靠的功能**，与吹嘘没有的能力是同一种失真。
 *   让用户以为"沙箱配置已经保存了"是最糟的 ——
 *   他会据此假设计划里的沙箱策略真的生效了。
 *
 *   ⚠️ 这里曾挂着一条"尚未接入：通知的后端接口还没交付，刷新回到初始值"
 *      的提示。v0.22 通知交付后那句就成了假话，遂删除 —— 连同
 *      `BACKEND_PENDING` 那套开关一起（见 settings-store 的说明）。
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
          模型服务、沙箱与执行、通知规则与渠道、扫描保留、访问令牌与登录口令
          都会真正写到后端并持久化，刷新、换浏览器都还在。
        </span>
      </p>

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
