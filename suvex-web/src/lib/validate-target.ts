import { TARGET_KIND_HINT, type TargetKind } from '@/data/scan'

/**
 * 目标输入的校验 —— 纯函数，放在 lib 里便于独立验证。
 *
 * 设计取向：**刻意不做得太严。**
 * 这是安全测试工具，用户会把各种奇怪的东西填进来 —— 内网 IP、带端口的主机、
 * 非标准 TLD、IP 段。过严的正则会拦掉合法输入，那比漏校验更烦人
 * （用户无法绕过，只能改工具）。
 *
 * 所以只拦"明显不对"的，其余交给后端。每一条错误都给出**怎么改**，
 * 而不是只说"格式错误" —— 后者等于把问题抛回给用户。
 *
 * ⚠️ 唯一的例外是 `artifact`（任务 17）：它**没有地址**，空输入就是
 * 唯一合法值，所以它在"空值 → 请填写目标"那条早退**之前**单独返回。
 * 判定顺序在这里是有意义的，不是随手排的 —— 详见 `validateTarget`。
 */

export interface TargetValidation {
  ok: boolean
  /** 错误信息。ok 为 true 时为 null */
  error: string | null
  /** 即时提示（不是错误）—— 帮助用户理解格式，或预告会发生什么 */
  hint: string | null
}

export function validateTarget(kind: TargetKind, raw: string): TargetValidation {
  /*
    ★ `artifact` 必须在下面那条"空值 → 请填写目标"**之前**返回。

    那一条是给地址类目标用的：空地址 = 漏填，拦下来是对的。而 `artifact` 下
    空地址是**唯一合法的值** —— 把顺序写反，这类题目就会永远停在
    「请填写目标」，也就是这次改动要修的那个毛病原样复现一遍，
    而且它会看起来像校验没生效，最难归因。

    与后端同口径：`targets.validate` 对 `artifact` 不管输入是什么都返回通过。
    这里仍然给一句 hint 而不是静默通过 —— 向导会把它显示在目标地址
    那一块的下方，正好用来回答"为什么不用填"。
  */
  if (kind === 'artifact') {
    return { ok: true, error: null, hint: '这类题目只用附件，不需要目标地址。' }
  }

  const value = raw.trim()

  if (value.length === 0) {
    return { ok: false, error: '请填写目标', hint: null }
  }

  switch (kind) {
    case 'url':
      return validateUrl(value)
    case 'repo':
      return validateRepo(value)
    case 'path':
      return validatePath(value)
    case 'service':
      return validateService(value)
    default:
      return { ok: true, error: null, hint: null }
  }
}

/**
 * 从 CTF 平台给的连接命令里抽出 `<host>:<port>`。
 *
 * ★ 与后端 `scans/targets.py: extract_service` 是**同一套正则**，
 *   逐条对照写的。前端这一份的作用是"不必往返就能看到结果"，
 *   服务端那一份才是真正生效的（前端可以绕过）。
 *   两处规则不一致会得到"前端说行、后端说不行"这种最难解释的体验。
 */
const BARE_SERVICE = /^([\w.-]+):(\d{1,5})$/
const SERVICE_PASTE_PATTERNS: RegExp[] = [
  // openssl s_client -connect H:P -servername H
  /-connect\s+([\w.-]+):(\d{1,5})/i,
  // socat - TCP:H:P
  /\bTCP:([\w.-]+):(\d{1,5})/i,
  // nc / ncat / netcat [选项] H P
  /\b(?:nc|ncat|netcat)\s+(?:-\S+\s+)*([\w.-]+)\s+(\d{1,5})(?![\d.])/i,
]

export function extractService(value: string): { host: string; port: number } | null {
  const text = value.trim()
  if (text.length === 0) return null

  const bare = BARE_SERVICE.exec(text)
  if (bare) return { host: bare[1], port: Number(bare[2]) }

  for (const pattern of SERVICE_PASTE_PATTERNS) {
    const found = pattern.exec(text)
    if (found) return { host: found[1], port: Number(found[2]) }
  }
  return null
}

/**
 * 裸 TCP 目标（任务 15）。
 *
 * ⚠️ 提示里那句"**不是 HTTP**"必须保留：执行后端（strix）的类型系统里
 * 没有 TCP 服务这一类，`host:9999` 会被它判成 `web_application` 并补
 * `https://`。我们改不了它的分类，只能靠指令纠正 —— 而这里正是用户
 * 第一次被告知"这是 TCP 不是网站"的地方，口径必须与指令一致。
 */
function validateService(value: string): TargetValidation {
  const found = extractService(value)
  if (!found) {
    return {
      ok: false,
      error: '不是合法的 TCP 目标',
      hint: `格式应为 ${TARGET_KIND_HINT.service.example}，也可以直接粘贴平台的连接命令（openssl s_client / nc / socat）`,
    }
  }

  if (found.port < 1 || found.port > 65535) {
    return { ok: false, error: `端口 ${found.port} 超出范围`, hint: '端口应在 1-65535 之间' }
  }

  if (!found.host.includes('.') && found.host !== 'localhost') {
    return {
      ok: false,
      error: '主机名看起来不完整',
      hint: `需要可解析的域名或 IP，例如 ${TARGET_KIND_HINT.service.example}`,
    }
  }

  const canonical = `${found.host}:${found.port}`
  if (value.trim() !== canonical) {
    return {
      ok: true,
      error: null,
      hint: `已从粘贴的命令里提取 ${canonical}；将按原始 TCP 连接（不是 HTTP）。`,
    }
  }
  return { ok: true, error: null, hint: '将按原始 TCP 连接（不是 HTTP）。' }
}

function validateUrl(value: string): TargetValidation {
  // 允许不带协议 —— 自动补 https。这是常见输入习惯，不该报错
  const hasScheme = /^https?:\/\//i.test(value)
  const withScheme = hasScheme ? value : `https://${value}`

  let parsed: URL
  try {
    parsed = new URL(withScheme)
  } catch {
    return {
      ok: false,
      error: '不是合法的 URL',
      hint: `格式应为 ${TARGET_KIND_HINT.url.example}`,
    }
  }

  if (!parsed.hostname.includes('.') && parsed.hostname !== 'localhost') {
    return {
      ok: false,
      error: '主机名看起来不完整',
      hint: '需要可解析的域名或 IP，例如 api.example.com',
    }
  }

  // 没写协议时预告将要使用的协议 —— 这是有用的信息，不是错误
  return {
    ok: true,
    error: null,
    hint: hasScheme ? null : `将按 ${parsed.protocol}// 访问`,
  }
}

function validateRepo(value: string): TargetValidation {
  // 支持 https 与 ssh 两种 git 地址形式
  const looksLikeGit = /^https?:\/\/\S+\/\S+/.test(value) || /^git@[\w.-]+:\S+/.test(value)
  if (!looksLikeGit) {
    return {
      ok: false,
      error: '不是合法的仓库地址',
      hint: `支持 https 或 git@ 形式，例如 ${TARGET_KIND_HINT.repo.example}`,
    }
  }
  return { ok: true, error: null, hint: '将以只读方式克隆到沙箱。' }
}

function validatePath(value: string): TargetValidation {
  // 绝对路径：POSIX 以 / 开头，Windows 以盘符开头
  const isAbsolute = value.startsWith('/') || /^[A-Za-z]:[\\/]/.test(value)
  if (!isAbsolute) {
    return {
      ok: false,
      error: '需要绝对路径',
      hint: `相对路径无法确定指向哪里，例如 ${TARGET_KIND_HINT.path.example}`,
    }
  }
  // 说明"不会修改文件" —— 这是用户在选择本地路径时最关心的事
  return { ok: true, error: null, hint: '将直接读取该目录，不会修改文件。' }
}

/**
 * 把目标输入归一化成列表里统一呈现的形式。
 *
 * 为什么需要：同一目标可以有很多种写法 ——
 *   `api.example.com`、`https://api.example.com`、`https://api.example.com/`
 * 如果原样存下来，列表里会出现三条看起来不同的记录，实际是同一个东西。
 * 归一化之后口径统一。
 *
 * 保留端口（`host:8443`）—— 同一主机的不同端口是两个不同目标。
 */
export function normalizeTarget(kind: TargetKind, raw: string): string {
  /*
    ⚠️ `artifact` 一律归一成**空串**，这是契约而不是将就。

    后端要求 `artifact` 的 `target` 为空，附件名它自己写进 `scans.target`
    （第一个附件的文件名）。这里显式写出来，而不是"靠下面的早退恰好返回空"：
    靠巧合成立的规则，下一个人给这里加一句 trim 或兜底就会把它弄坏，
    而症状是提交时 400，看起来像后端的毛病。
  */
  if (kind === 'artifact') return ''

  const value = raw.trim()
  if (value.length === 0) return value

  if (kind === 'url') {
    try {
      const parsed = new URL(/^https?:\/\//i.test(value) ? value : `https://${value}`)
      // 用 host 而非 hostname：保留端口。不加尾斜杠
      return parsed.host
    } catch {
      return value
    }
  }

  if (kind === 'service') {
    // 粘贴进来的连接命令在这里被归一化成 host:port（与后端同规则）。
    const found = extractService(value)
    return found ? `${found.host}:${found.port}` : value
  }

  // 仓库与路径保留原样 —— 路径的大小写和结尾斜杠都可能有意义（尤其 Windows）
  return value
}

/**
 * 从目标推导一个可读的扫描名。
 *
 * 为什么不直接用输入原文：URL 会带上协议前缀（`https://api.example.com`），
 * 作为列表里的一行标签太长且噪音大。取主机名更干净，也更容易一眼扫过。
 * 用户显式填了名称时不会走这里。
 *
 * ★ `artifactName` 这个可选参数**只服务 `artifact`**：那一类的名字不在目标里
 *   （目标的契约就是空串），而在**附件名**里 —— 后端留空时的取名规则是
 *   "附件名去掉扩展名"（`tryre.zip` → `tryre`）。附件不经过这个函数，
 *   所以由调用方（向导）把第一个附件的文件名递进来。
 *
 *   ⚠️ 拿不到附件名时返回中性的「未命名扫描」，**绝不在这里编一个假名字**：
 *   那会让确认页写着一个提交后并不存在的名称，比留空更误导。
 */
export function defaultNameFrom(
  kind: TargetKind,
  raw: string,
  artifactName?: string,
): string {
  if (kind === 'artifact') {
    const source = (artifactName ?? '').trim()
    if (source.length === 0) return '未命名扫描'
    // 与后端同口径：去掉**最后一个**扩展名（`a.tar.gz` → `a.tar`，同 pathlib 的 stem）
    return source.replace(/\.[^.]+$/, '') || source
  }

  const value = raw.trim()
  if (value.length === 0) return '未命名扫描'

  if (kind === 'url') {
    try {
      const parsed = new URL(/^https?:\/\//i.test(value) ? value : `https://${value}`)
      // host 保留了端口 —— 同一主机不同端口是两个不同的目标，不能丢
      return parsed.host
    } catch {
      return value
    }
  }

  if (kind === 'service') {
    // 带上端口：同一主机不同端口是两个完全不同的服务，
    // 只留主机名会让列表里两行看起来像同一个目标。
    const found = extractService(value)
    return found ? `${found.host}:${found.port}` : value
  }

  if (kind === 'repo') {
    // https://github.com/acme/backend.git → acme/backend
    return (
      value
        .replace(/\.git$/, '')
        .replace(/^git@[\w.-]+:/, '')
        .replace(/^https?:\/\/[^/]+\//, '') || value
    )
  }

  // 本地路径：取最后一段目录名
  const segments = value.split(/[\\/]/).filter(Boolean)
  return segments[segments.length - 1] ?? value
}
