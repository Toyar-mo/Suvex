import { Component, type ErrorInfo, type ReactNode } from 'react'

/**
 * 顶层错误边界（任务 10 P6 补）。
 *
 * ## ★ 为什么必须有它
 *
 * 这个项目**原本一个错误边界都没有** —— 而 React 的行为是：
 * 渲染期抛出的异常若无人接住，**整棵组件树会被卸载**，
 * `#root` 变成空的，用户看到一片白。
 *
 * 这不是理论风险，它在实测中真的发生了：
 *
 * > 后端的时间线多了一种 `kind`（`assigned`，任务 10 P2 的指派），
 * > 而前端的图标表没跟上。`TIMELINE_ICON[kind]` 得到 `undefined`，
 * > `<Icon />` 抛 "Element type is invalid" —— **打开任何一条被指派过的
 * > 漏洞，整个后台白屏**。用户没法去看别的页面，也没法自己排查。
 *
 * 一个图标的疏忽不该让整个产品不可用。而且更糟的是：
 * 白屏时用户**看不到任何信息** —— 他不知道是自己操作错了、
 * 服务挂了、还是数据坏了，只能刷新（无济于事）或放弃。
 *
 * ## 它做什么、不做什么
 *
 * **做**：把崩溃限制在一处，说清"界面出错"、给出可执行的下一步
 * （重试 / 回列表），并把技术细节**折叠起来**（默认不展开）。
 *
 * **不做**：不重试渲染（那会无限循环）、不上报（本项目没有遥测后端，
 * 假装上报等于骗自己）。`componentDidCatch` 只往控制台写，
 * 那是排查者真正会去看的地方。
 *
 * ⚠️ **错误边界抓不住的东西**（React 的固有限制，写在这里免得下一个人
 * 以为有了它就万无一失）：事件处理器里的异常、异步代码（`setTimeout`、
 * Promise）、服务端渲染、以及**它自己**抛出的异常。
 * 那些要靠 try/catch 与全局 `unhandledrejection` 处理，不是这里。
 */
interface ErrorBoundaryProps {
  children: ReactNode
}

interface ErrorBoundaryState {
  error: Error | null
}

export class ErrorBoundary extends Component<ErrorBoundaryProps, ErrorBoundaryState> {
  state: ErrorBoundaryState = { error: null }

  static getDerivedStateFromError(error: Error): ErrorBoundaryState {
    return { error }
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    // 控制台是排查者唯一一定会看的地方。带上组件栈 ——
    // 它比 error.stack 更能指出"是哪个组件渲染时炸的"
    // （刚才那次白屏就是靠组件栈定位到 Timeline 的）。
    console.error('[suvex] 界面渲染出错，已被错误边界接住：', error, info.componentStack)
  }

  private handleRetry = () => {
    /*
      「重试」= 清掉错误状态再渲染一次。

      它**不是**万能药：若错误来自数据（比如界面上那条坏记录还在），
      重渲染会立刻再炸一次 —— 而那比"点一下没反应"好，因为用户
      至少看到它是稳定复现的。真正能救他的是"回到列表"那个按钮。
    */
    this.setState({ error: null })
  }

  private handleReload = () => {
    // 整页重载：能解决"某段状态坏了"这一类问题，也是用户自己会做的第一件事
    window.location.reload()
  }

  render() {
    const { error } = this.state
    if (!error) return this.props.children

    return (
      <div className="flex min-h-screen items-center justify-center bg-canvas px-gutter">
        <div className="w-full max-w-[52ch] rounded-md border border-line bg-surface p-panel">
          <h1 className="text-sm font-medium text-ink">界面出错了</h1>
          <p className="mt-1.5 text-xs leading-relaxed text-ink-muted">
            这一页在渲染时遇到问题，已经停在这里，避免继续显示不完整的数据。
            你正在看的**数据没有丢** —— 后端与数据库都不受影响。
          </p>

          <div className="mt-3 flex flex-wrap gap-tight">
            <button
              type="button"
              onClick={this.handleReload}
              className="press inline-flex h-8 items-center rounded-sm bg-accent px-3 text-xs font-medium text-canvas transition-ui duration-fast hover:bg-accent-strong"
            >
              重新加载页面
            </button>
            <button
              type="button"
              onClick={this.handleRetry}
              className="press inline-flex h-8 items-center rounded-sm border border-line px-3 text-xs font-medium text-ink-muted transition-ui duration-fast hover:border-line-strong hover:text-ink"
            >
              再试一次
            </button>
            {/*
              「回总览」用整页跳转而不是路由跳转：
              路由跳转要复用当前这棵已经出错的树，而错误状态还在 ——
              换页多半会立刻再炸。整页重载能保证从干净的状态起来。
            */}
            <a
              href="/overview"
              className="press inline-flex h-8 items-center rounded-sm border border-line px-3 text-xs font-medium text-ink-muted transition-ui duration-fast hover:border-line-strong hover:text-ink"
            >
              回到总览
            </a>
          </div>

          {/*
            技术细节**默认折叠**：普通用户不需要看，而把它摊开
            会让人以为"这是我弄坏的"。但也不能丢 —— 出问题时
            用户截图给维护者，那几行就是全部线索。
          */}
          <details className="mt-3">
            <summary className="cursor-pointer text-2xs text-ink-faint hover:text-ink-muted">
              技术细节（反馈问题时请附上这一段）
            </summary>
            <pre className="mono-ui mt-1.5 max-h-48 overflow-auto whitespace-pre-wrap rounded-sm bg-surface-sunken p-2 text-2xs text-ink-muted">
              {error.message}
              {error.stack ? `\n\n${error.stack}` : ''}
            </pre>
          </details>
        </div>
      </div>
    )
  }
}

export default ErrorBoundary
