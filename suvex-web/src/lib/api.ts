/**
 * 统一的 API 客户端（契约 §7 的 `lib/api.ts`、§8 的「401 拦截」「凭据携带」）。
 *
 * ## 为什么必须有这一层
 *
 * 契约 §7 把它列为**新增**项。理由不是"少写几行重复代码"，而是三件
 * 每个页面各写一遍就一定会写错的事：
 *
 * 1. **`credentials: 'include'`** —— 少了它浏览器根本不带 Cookie，
 *    症状是「登录成功，但下一个请求又是未登录」。开发态前端在 `:5173`、
 *    后端在 `:8000`，是**跨源**请求，必须显式声明。
 * 2. **401 的统一处理** —— 会话过期后，让每个页面各自决定怎么办，
 *    结果一定是「有的页跳登录、有的页静静地显示空列表」。
 *    这件事只能有一处决定。
 * 3. **错误信封的解析** —— 后端固定返回 `{ error: { code, message, details? } }`。
 *    各页自己 `res.json()` 再猜字段名，最后总会有一半页面在出错时白屏。
 *
 * ## ★ 一个隐蔽的坑：`SameSite=Strict` 关心的是 **site**，不是 origin
 *
 * 后端下发的会话 Cookie 是 `SameSite=Strict`（契约 §1.2，防 CSRF 的关键）。
 * 浏览器判断 `SameSite` 用的是 **site**（域名），而 **端口不算**。
 *
 *   http://127.0.0.1:5173  →  http://127.0.0.1:8000   ✅ 同 site，Cookie 照发
 *   http://localhost:5173  →  http://127.0.0.1:8000   ❌ **跨 site，Cookie 不发**
 *
 * 第二行是最容易踩的：两个地址都指向本机回环，看起来完全是同一个东西，
 * 但在浏览器眼里 `localhost` 与 `127.0.0.1` 是**两个不同的 site**。
 * 而后端 CORS 白名单（`SUPEX_CORS_ORIGINS`）两个都放行了 ——
 * 也就是说**请求会成功、Cookie 却被丢掉**，于是又回到
 * 「登录成功但下一个请求未登录」这个最难查的症状。
 *
 * 所以这里的默认地址**跟随当前页面的 hostname**，而不是写死 `127.0.0.1`：
 * 用哪个地址打开前端，就用哪个地址去请求后端，永远同 site。
 * 显式配了 `VITE_API_BASE_URL` 时以配置为准（部署场景）。
 */

// ─────────────────────────────────────────────
// 地址
// ─────────────────────────────────────────────

/**
 * 后端地址。
 *
 * 优先用 `VITE_API_BASE_URL`（`.env.local`，见 `.env.example`）；
 * 没配时按下面这条推导 —— 目的是**与前端同 site**（见文件头那段）。
 */
function resolveBaseUrl(): string {
  const configured = import.meta.env.VITE_API_BASE_URL?.trim()
  if (configured) return configured.replace(/\/+$/, '')

  /*
    ★ 默认 = **当前页面的 origin**，即请求发回"前端自己所在的这个地址"。

    这不是"回退到 127.0.0.1"，而是一个刻意的选择：让前端**根本不出现
    后端的主机名和端口**。请求打到同源的 `/api/...`，由开发服务器的
    `server.proxy`（见 vite.config.ts）或生产环境的反向代理转发到后端。

    为什么这样才对：

    * **开发机怎么变都不用改代码。** 无论用 `127.0.0.1:5173`、
      `192.168.44.128:5173` 还是别的地址打开前端，请求都发回同一个
      origin，不存在"写死一个 IP、换台机器就废掉"的问题。
    * **同源就没有 CORS。** 不再需要 `SUPEX_CORS_ORIGINS` 里逐个列出
      访问者的来源 —— 而那个列表每换一个 IP 就要改一次，正是
      "本地能跑、交付到别处就登不上"的典型成因。
    * **cookie 一定带得上。** 同源请求必然携带 Cookie，绕开了
      `SameSite` 对跨 site 的限制（见文件头那段）。

    原来的实现是 `http://${window.location.hostname}:8000` —— 主机名跟着
    页面走是对的，但**端口写死了 8000**。后端一旦不在 8000（比如 8000
    被别的服务占着），就只能靠 `VITE_API_BASE_URL` 配一个绝对地址来绕，
    而那一配就把主机名也钉死了。

    ⚠️ 前提：**代理必须存在**。开发时靠 vite.config.ts 的 `server.proxy`；
    生产部署时要么让后端直接托管前端产物（同源），要么用 nginx 反代。
    如果既没有代理、后端又不同源，就必须显式配 `VITE_API_BASE_URL`。
  */
  if (typeof window !== 'undefined') return window.location.origin

  // SSR / 测试环境没有 window：保持一个能解析的绝对地址
  return 'http://127.0.0.1:8000'
}

export const API_BASE_URL = resolveBaseUrl()

/** 契约 §0 定下的统一前缀。所有业务端点都在它下面。 */
export const API_PREFIX = '/api/v1'

// ─────────────────────────────────────────────
// 错误
// ─────────────────────────────────────────────

/**
 * 后端的错误信封（契约 §1.3）解出来的东西。
 *
 * 带 `code` 而不是只带 `message`：调用方要按**错误种类**分支
 * （比如 `PROVIDER_IS_DEFAULT` 要提示"先去把另一个设为默认"），
 * 而拿中文消息去 `includes('默认')` 做判断会在文案一改就静默失效。
 */
export class ApiError extends Error {
  /** 契约 §1.3 的错误码，如 `UNAUTHENTICATED`、`VALIDATION_FAILED` */
  readonly code: string
  /** HTTP 状态码。0 表示请求根本没发出去（后端没起、断网） */
  readonly status: number
  /** 字段级校验错误：`{ 字段名: 提示 }`。用于表单标红 */
  readonly fields: Record<string, string>

  constructor(
    code: string,
    message: string,
    status: number,
    fields: Record<string, string> = {},
  ) {
    super(message)
    this.name = 'ApiError'
    this.code = code
    this.status = status
    this.fields = fields
  }

  /** 后端根本没应答 —— 与"后端应答了一个错误"是两种完全不同的处境 */
  get isNetworkError(): boolean {
    return this.status === 0
  }
}

/**
 * 把后端的 `details` 收敛成 `{ 字段: 文案 }`。
 *
 * 契约里 `details` 的形状是"字段名 → 提示"，但值是 `Any`，
 * 所以这里做一次防御性收窄：不是字符串的值（数组、嵌套对象）
 * 统一 `JSON.stringify`。**不猜结构**，猜错会让表单标错字段，
 * 而"标错字段"比"不标字段"更让人困惑。
 */
function toFieldErrors(details: unknown): Record<string, string> {
  if (!details || typeof details !== 'object') return {}
  const out: Record<string, string> = {}
  for (const [key, value] of Object.entries(details as Record<string, unknown>)) {
    if (value == null) continue
    out[key] = typeof value === 'string' ? value : JSON.stringify(value)
  }
  return out
}

// ─────────────────────────────────────────────
// 401 的统一处理
// ─────────────────────────────────────────────

/**
 * 会话失效时要做什么（清本地态 + 跳登录页）。
 *
 * ★ 用**注册回调**而不是在这里 `import` 路由：
 *   `lib/` 是叶子层，让它依赖 react-router 会让"这个模块能不能单独测"
 *   变成"要先把整个路由搭起来"。App 启动时注册一次即可。
 */
type UnauthorizedHandler = () => void

let unauthorizedHandler: UnauthorizedHandler | null = null

export function setUnauthorizedHandler(handler: UnauthorizedHandler | null): void {
  unauthorizedHandler = handler
}

// ─────────────────────────────────────────────
// 请求
// ─────────────────────────────────────────────

export interface RequestOptions {
  method?: 'GET' | 'POST' | 'PUT' | 'PATCH' | 'DELETE'
  /** 请求体。会被 JSON 序列化 —— 传 FormData 请走 `rawBody` */
  body?: unknown
  /**
   * 原样发送的请求体（不 JSON 序列化、不设 `Content-Type`）。
   *
   * ★ 只有一个用途：**multipart 上传**（任务 10 P6-d）。
   *
   * 两件事都不能做：
   *  · **不能 `JSON.stringify`** —— 那会把 `FormData` 变成一个空对象 `{}`，
   *    而服务端收到的是一个没有文件的请求（表现是"上传成功但附件是空的"）。
   *  · **不能手写 `Content-Type`** —— multipart 必须带 `boundary`，
   *    而 boundary 由浏览器在发送时生成。手写成
   *    `multipart/form-data`（不带 boundary）会让服务端解析失败。
   *
   * 这个选项在注释里被承诺过（`body` 那行写着"传 FormData 请走 rawBody"），
   * 但一直没实现 —— 直到附件上传真的需要它。
   */
  rawBody?: BodyInit
  /** 查询参数。`undefined` / `null` 的键会被丢掉（不拼成 `?a=undefined`） */
  query?: Record<string, string | number | boolean | null | undefined>
  /**
   * 401 时**不**触发跳转。
   *
   * 三种情况必须设它，否则会陷入循环或误伤：
   *   · `/auth/state`（启动探测，未登录是正常结果）
   *   · `/auth/login` 与 `/auth/setup`（口令错误本身就是 401，跳登录页毫无意义）
   *   · `/auth/me`（由路由守卫自己决定跳哪，不需要 lib 再插一手）
   */
  skipAuthRedirect?: boolean
  /** 超时（毫秒）。真实网络会挂住，而"转圈转到天荒地老"是最糟的加载态 */
  timeoutMs?: number
  signal?: AbortSignal
}

/** 默认超时。20 秒：足够慢接口跑完，又不至于让用户以为页面死了。 */
const DEFAULT_TIMEOUT_MS = 20_000

function buildUrl(path: string, query?: RequestOptions['query']): string {
  const url = new URL(`${API_BASE_URL}${API_PREFIX}${path}`)
  if (query) {
    for (const [key, value] of Object.entries(query)) {
      // 丢掉空值而不是拼成 "?severity=undefined" —— 后者会真的发给后端，
      // 而后端把它当成一个筛选条件，于是列表**静默变空**。
      if (value === undefined || value === null || value === '') continue
      url.searchParams.set(key, String(value))
    }
  }
  return url.toString()
}

/**
 * 发一个请求并解出结果。失败一律抛 `ApiError`。
 *
 * **永不返回"半成功"**：调用方只需要 `try/catch` 一种分支，
 * 不必再判断 `res.ok` —— 那种判断漏一处就是一次白屏。
 */
export async function apiRequest<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const {
    method = 'GET',
    body,
    rawBody,
    query,
    skipAuthRedirect = false,
    timeoutMs = DEFAULT_TIMEOUT_MS,
    signal,
  } = options

  // 超时用 AbortController 实现，并把它与外部传入的 signal 串起来
  const controller = new AbortController()
  const timer = window.setTimeout(() => controller.abort(), timeoutMs)
  const onAbort = () => controller.abort()
  signal?.addEventListener('abort', onAbort)

  let response: Response
  try {
    // ★ `rawBody` 优先：它原样发送，且**不设 `Content-Type`**
    //   （multipart 的 boundary 必须由浏览器生成 —— 见 `RequestOptions.rawBody`）。
    const payloadForFetch: BodyInit | undefined =
      rawBody ?? (body === undefined ? undefined : JSON.stringify(body))
    const headers =
      rawBody !== undefined || body === undefined
        ? undefined
        : { 'Content-Type': 'application/json' }

    response = await fetch(buildUrl(path, query), {
      method,
      // ★ 契约 §8：不显式写这行，跨源请求不会带 Cookie
      credentials: 'include',
      headers,
      body: payloadForFetch,
      signal: controller.signal,
    })
  } catch (error) {
    // 走到这里说明**请求根本没拿到应答**：后端没起、断网、超时、或被主动取消。
    // 这与"后端回了一个 4xx"是两回事，所以给一个独立的 code。
    const aborted = error instanceof DOMException && error.name === 'AbortError'
    throw new ApiError(
      aborted ? 'REQUEST_ABORTED' : 'NETWORK_ERROR',
      aborted
        ? '请求超时或被取消，请重试。'
        : `连接不上后端服务（${API_BASE_URL}）。请确认服务已启动。`,
      0,
    )
  } finally {
    window.clearTimeout(timer)
    signal?.removeEventListener('abort', onAbort)
  }

  if (response.status === 204) {
    // 契约里 logout / 删除类端点返回 204，没有响应体
    return undefined as T
  }

  // 先取文本再解析：万一后端返回的不是 JSON（反代错误页、HTML 报错页），
  // 直接 `res.json()` 会抛一个和真实原因无关的 SyntaxError，
  // 排查方向会被完全带偏。
  const text = await response.text()
  let payload: unknown = null
  if (text) {
    try {
      payload = JSON.parse(text)
    } catch {
      if (!response.ok) {
        throw new ApiError(
          'MALFORMED_RESPONSE',
          `服务端返回了非 JSON 响应（HTTP ${response.status}）。`,
          response.status,
        )
      }
      throw new ApiError('MALFORMED_RESPONSE', '服务端返回了无法解析的响应。', response.status)
    }
  }

  if (!response.ok) {
    const envelope = (payload as { error?: { code?: string; message?: string; details?: unknown } })
      ?.error

    const error = new ApiError(
      envelope?.code ?? 'UNKNOWN_ERROR',
      // 没有 message 时不要编一个 —— 状态码本身是事实，编出来的文案不是
      envelope?.message ?? `请求失败（HTTP ${response.status}）。`,
      response.status,
      toFieldErrors(envelope?.details),
    )

    // ★ 401 只在这里处理一次。会话过期时任何页面的任何请求都会走到这，
    //   跳转逻辑只需要实现一遍（契约 §8：「不能只在登录接口处理」）。
    if (response.status === 401 && !skipAuthRedirect) {
      unauthorizedHandler?.()
    }

    throw error
  }

  return payload as T
}

/**
 * 把任意异常收敛成一句**可以直接显示给用户**的中文。
 *
 * 放在这里而不是各 store 各写一份：这份逻辑有一个容易漏的分支 ——
 * `ApiError` 的 `message` 已经是后端写好的中文（契约要求错误信封里的
 * `message` 可直接展示），重新拼一遍只会把有用的信息换成泛泛的
 * "请求失败"。三处各写一遍，就会有其中一处漏掉这个分支。
 */
export function errorMessage(error: unknown): string {
  if (error instanceof ApiError) return error.message
  if (error instanceof Error && error.message) return error.message
  return '请求失败，请稍后重试。'
}

/**
 * 把任意异常收敛成 `ApiError`。
 *
 * ★ 存在的理由：`ErrorState` 靠 `isNetworkError`（即 `status === 0`）区分
 *   「连不上后端」与「后端答了一个错误」，而这两种处境**下一步动作完全不同**
 *   （去启动服务 vs 看提示改输入）。
 *
 *   所以 store 里必须存**真的** `ApiError`，不能只存一句文案再在页面里
 *   包一个 `new ApiError(..., 0)` —— 那会把后端返回的 500、403 全部
 *   显示成"连接不上后端服务（http://…）"，把用户引向完全错误的方向。
 *
 * 落不到 `ApiError` 时用 `status: -1` 而不是 0：0 在这个代码库里
 * **专指"请求没拿到应答"**，占用它会让上面那个判断失效。
 */
export function toApiError(error: unknown): ApiError {
  if (error instanceof ApiError) return error
  return new ApiError('UNKNOWN_ERROR', errorMessage(error), -1)
}

/**
 * 文本产物（报告下载）。
 *
 * ★ 为什么不能用 `apiRequest`：它按 JSON 解析响应体，而
 *   `GET /reports/{id}/download` 回的是 `text/markdown` 正文 ——
 *   `JSON.parse` 会抛 SyntaxError，然后被包成 `MALFORMED_RESPONSE`，
 *   用户看到「服务端返回了无法解析的响应」。**而它回得完全正确。**
 *   这类"解析器的假设与端点不符"造成的错误信息，排查时最容易被带偏。
 *
 * 文件名从 `Content-Disposition` 取，而不是前端自己按编号拼：
 *   后端用报告编号命名（契约 §3.5），前端再算一遍就是**第二处编号来源**，
 *   而编号是交付物的身份 —— 两处来源迟早会分叉。
 */
export interface TextDownload {
  text: string
  /** 后端给的下载文件名。没给时为 null（调用方自行兜底） */
  filename: string | null
  mime: string
}

/** 从 `Content-Disposition` 解出文件名 */
function filenameFrom(header: string | null): string | null {
  if (!header) return null
  /*
    只认 `filename="..."` 这一种写法。RFC 5987 的 `filename*=UTF-8''…`
    本后端不用（报告编号是纯 ASCII，见契约 §3.5 的说明），
    为它写一套解码只会多出一段永远不执行的代码。
  */
  const match = /filename="([^"]+)"/i.exec(header)
  return match ? match[1] : null
}

/**
 * 发一个请求并原样返回**文本**。失败仍然抛 `ApiError`（与 `apiRequest` 一致）。
 */
export async function apiText(
  path: string,
  options: Omit<RequestOptions, 'method' | 'body'> = {},
): Promise<TextDownload> {
  const { skipAuthRedirect = false, timeoutMs = DEFAULT_TIMEOUT_MS, signal } = options

  const controller = new AbortController()
  const timer = window.setTimeout(() => controller.abort(), timeoutMs)
  const onAbort = () => controller.abort()
  signal?.addEventListener('abort', onAbort)

  let response: Response
  try {
    response = await fetch(buildUrl(path), {
      method: 'GET',
      credentials: 'include',
      signal: controller.signal,
    })
  } catch (error) {
    const aborted = error instanceof DOMException && error.name === 'AbortError'
    throw new ApiError(
      aborted ? 'REQUEST_ABORTED' : 'NETWORK_ERROR',
      aborted
        ? '请求超时或被取消，请重试。'
        : `连接不上后端服务（${API_BASE_URL}）。请确认服务已启动。`,
      0,
    )
  } finally {
    window.clearTimeout(timer)
    signal?.removeEventListener('abort', onAbort)
  }

  const text = await response.text()

  if (!response.ok) {
    // 失败时后端回的还是 JSON 错误信封，所以走同一套解析 ——
    // 下载失败也必须给出后端那句中文，而不是「下载失败」四个字。
    let envelope: { code?: string; message?: string; details?: unknown } | undefined
    try {
      envelope = (JSON.parse(text) as { error?: typeof envelope }).error
    } catch {
      // 不是 JSON（反代错误页）：下面的兜底文案用状态码，它是唯一的事实
    }
    const error = new ApiError(
      envelope?.code ?? 'UNKNOWN_ERROR',
      envelope?.message ?? `请求失败（HTTP ${response.status}）。`,
      response.status,
      toFieldErrors(envelope?.details),
    )
    if (response.status === 401 && !skipAuthRedirect) unauthorizedHandler?.()
    throw error
  }

  return {
    text,
    filename: filenameFrom(response.headers.get('Content-Disposition')),
    mime: response.headers.get('Content-Type') ?? 'text/plain; charset=utf-8',
  }
}

/** 便捷方法。`path` 以 `/` 开头，不带 `/api/v1` 前缀。 */
export const api = {
  get: <T>(path: string, options?: Omit<RequestOptions, 'method' | 'body'>) =>
    apiRequest<T>(path, { ...options, method: 'GET' }),
  post: <T>(path: string, body?: unknown, options?: Omit<RequestOptions, 'method' | 'body'>) =>
    apiRequest<T>(path, { ...options, method: 'POST', body }),
  put: <T>(path: string, body?: unknown, options?: Omit<RequestOptions, 'method' | 'body'>) =>
    apiRequest<T>(path, { ...options, method: 'PUT', body }),
  patch: <T>(path: string, body?: unknown, options?: Omit<RequestOptions, 'method' | 'body'>) =>
    apiRequest<T>(path, { ...options, method: 'PATCH', body }),
  del: <T>(path: string, options?: Omit<RequestOptions, 'method' | 'body'>) =>
    apiRequest<T>(path, { ...options, method: 'DELETE' }),
  /**
   * **multipart 上传**（任务 10 P6-d）。
   *
   * ⚠️ 单独一个方法而不是让人用 `post(path, formData)`：
   * `post` 会 `JSON.stringify` 请求体，而 `JSON.stringify(new FormData())`
   * 得到 `"{}"` —— 服务端收到一个**没有文件**的请求，而它**不会报错**，
   * 只是附件列表是空的。那种"成功但什么都没传到"是最费劲的一类 bug。
   *
   * 超时给得比默认长：上传可能传几 MB 的图片，20 秒不一定够。
   */
  upload: <T>(
    path: string,
    form: FormData,
    options?: Omit<RequestOptions, 'method' | 'body' | 'rawBody'>,
  ) => apiRequest<T>(path, { ...options, method: 'POST', rawBody: form, timeoutMs: 60_000 }),
}
