/**
 * 极简语法高亮 —— 自实现，不引依赖。
 *
 * 为什么不用 highlight.js / shiki：
 *   - shiki 会把每种语言的语法定义整包带进来（几百 KB），而我们只需要
 *     4 种语言（python / bash / html / http），且都是可复现脚本这种短文本；
 *   - highlight.js 的自动语言检测在这里是有害的 —— PoC 代码块的语言是
 *     数据里明确标注的，不需要猜；
 *   - 引入一个高亮库会带来它的主题系统，和我们既有的 token 体系打架。
 *
 * 做法是**线性扫描**（单遍，不回溯、不用正则栈），把源码切成带类型的片段。
 * 这不是一个通用的高亮器，它只处理这几种语言里最常见的东西，
 * 对这个场景够用：PoC 是给人读的短脚本，不是要编译的工程代码。
 *
 * 一个刻意的取舍：**不追求 100% 正确**。遇到无法确定的片段就归为 plain，
 * 高亮错了只是不好看，而漏掉一个关键字没有代价。
 */

export type TokenKind =
  | 'plain'
  | 'comment'
  | 'keyword'
  | 'string'
  | 'number'
  | 'function'
  | 'variable'
  | 'operator'
  | 'property'
  | 'meta'

export interface Token {
  kind: TokenKind
  text: string
}

/**
 * 各种语言的关键字集合。
 * 用 Set 而不是数组：查找是 O(1)，而这些集合每次高亮都会查很多次。
 */
const PYTHON_KEYWORDS = new Set([
  'def',
  'class',
  'return',
  'if',
  'elif',
  'else',
  'for',
  'while',
  'in',
  'not',
  'and',
  'or',
  'is',
  'None',
  'True',
  'False',
  'import',
  'from',
  'as',
  'with',
  'try',
  'except',
  'finally',
  'raise',
  'pass',
  'break',
  'continue',
  'lambda',
  'yield',
  'await',
  'async',
  'global',
  'nonlocal',
  'assert',
  'del',
])

const PYTHON_BUILTINS = new Set([
  'print',
  'len',
  'range',
  'str',
  'int',
  'float',
  'dict',
  'list',
  'set',
  'tuple',
  'enumerate',
  'zip',
  'open',
  'isinstance',
  'type',
  'sum',
  'min',
  'max',
  'abs',
  'sorted',
  'any',
  'all',
  'map',
  'filter',
  'format',
  'repr',
  'bytes',
  'bool',
])

const SHELL_KEYWORDS = new Set([
  'if',
  'then',
  'else',
  'elif',
  'fi',
  'for',
  'while',
  'do',
  'done',
  'case',
  'esac',
  'function',
  'return',
  'exit',
  'export',
  'local',
  'readonly',
  'set',
  'unset',
  'shift',
  'source',
  'alias',
  'echo',
  'cd',
  'printf',
])

const SHELL_BUILTINS = new Set([
  'curl',
  'wget',
  'jq',
  'grep',
  'sed',
  'awk',
  'cat',
  'head',
  'tail',
  'sort',
  'uniq',
  'wc',
  'find',
  'xargs',
  'cut',
  'tr',
  'tee',
  'chmod',
  'mkdir',
  'rm',
  'cp',
  'mv',
  'sqlmap',
  'nmap',
  'nuclei',
  'python',
  'python3',
  'node',
  'npm',
])

const HTML_KEYWORDS = new Set([
  'html',
  'head',
  'body',
  'div',
  'span',
  'script',
  'style',
  'img',
  'a',
  'p',
  'form',
  'input',
  'button',
  'iframe',
  'svg',
  'meta',
  'link',
  'br',
  'hr',
])

/**
 * 主入口。按语言分派到对应的扫描器。
 *
 * 语言不认识时退化成纯文本而不是抛错 —— 数据里可能出现新语言，
 * 高亮不了也要能正常显示。
 */
export function tokenize(code: string, language: string): Token[][] {
  const lang = language.toLowerCase()

  let lines: Token[][]
  switch (lang) {
    case 'python':
    case 'py':
      lines = tokenizePython(code)
      break
    case 'bash':
    case 'sh':
    case 'shell':
      lines = tokenizeShell(code)
      break
    case 'html':
    case 'xml':
      lines = tokenizeHtml(code)
      break
    case 'http':
      lines = tokenizeHttp(code)
      break
    default:
      // 未知语言：整段当纯文本，仍按行切分（CodeBlock 要用行号）
      lines = code.split('\n').map((line) => [{ kind: 'plain' as TokenKind, text: line }])
  }

  return lines
}

// ─────────────────────────────────────────────
// 通用工具
// ─────────────────────────────────────────────

/**
 * 从 `start` 开始扫描一个字符串字面量，返回结束位置（不含）。
 *
 * 处理转义：`"a\"b"` 里的 `\"` 不是结尾。
 * 不处理跨行字符串（三引号）—— 我们的 PoC 里没有，遇到就归为 plain，
 * 顶多高亮不好看。
 */
function scanString(line: string, start: number): number {
  const quote = line[start]
  let i = start + 1
  while (i < line.length) {
    if (line[i] === '\\') {
      i += 2 // 跳过被转义的字符
      continue
    }
    if (line[i] === quote) return i + 1
    i += 1
  }
  return line.length // 未闭合，吃到行尾
}

function isIdentStart(ch: string): boolean {
  return /[A-Za-z_$]/.test(ch)
}

function isIdentPart(ch: string): boolean {
  return /[A-Za-z0-9_$]/.test(ch)
}

function isDigit(ch: string): boolean {
  return ch >= '0' && ch <= '9'
}

// ─────────────────────────────────────────────
// Python
// ─────────────────────────────────────────────

function tokenizePython(code: string): Token[][] {
  return code.split('\n').map((line) => {
    const tokens: Token[] = []
    let i = 0
    let plain = ''

    const flushPlain = () => {
      if (plain) {
        tokens.push({ kind: 'plain', text: plain })
        plain = ''
      }
    }

    while (i < line.length) {
      const ch = line[i]

      // 注释：Python 的 # 在字符串外一定是注释
      if (ch === '#') {
        flushPlain()
        tokens.push({ kind: 'comment', text: line.slice(i) })
        i = line.length
        continue
      }

      // 字符串
      if (ch === '"' || ch === "'") {
        flushPlain()
        const end = scanString(line, i)
        const text = line.slice(i, end)
        // 三引号判断：连续三个引号是文档字符串，标成 meta
        const isTriple = text.startsWith(ch.repeat(3))
        tokens.push({ kind: isTriple ? 'meta' : 'string', text })
        i = end
        continue
      }

      // 数字
      if (isDigit(ch) && !isIdentPart(line[i - 1] ?? '')) {
        flushPlain()
        let j = i
        while (j < line.length && /[0-9._xXa-fA-F]/.test(line[j])) j += 1
        tokens.push({ kind: 'number', text: line.slice(i, j) })
        i = j
        continue
      }

      // 标识符 / 关键字
      if (isIdentStart(ch)) {
        let j = i
        while (j < line.length && isIdentPart(line[j])) j += 1
        const word = line.slice(i, j)
        flushPlain()

        if (PYTHON_KEYWORDS.has(word)) {
          tokens.push({ kind: 'keyword', text: word })
        } else if (PYTHON_BUILTINS.has(word)) {
          tokens.push({ kind: 'function', text: word })
        } else if (line[j] === '(') {
          // 后面紧跟左括号 → 当作函数调用
          tokens.push({ kind: 'function', text: word })
        } else if (word === word.toUpperCase() && word.length > 1 && /^[A-Z_]+$/.test(word)) {
          // 全大写标识符 → 常量，用变量色区分
          tokens.push({ kind: 'variable', text: word })
        } else {
          tokens.push({ kind: 'plain', text: word })
        }
        i = j
        continue
      }

      // 运算符
      if (/[+\-*/%=<>!&|^~@:]/.test(ch)) {
        flushPlain()
        let j = i
        while (j < line.length && /[+\-*/%=<>!&|^~@:]/.test(line[j])) j += 1
        tokens.push({ kind: 'operator', text: line.slice(i, j) })
        i = j
        continue
      }

      plain += ch
      i += 1
    }

    flushPlain()
    return tokens
  })
}

// ─────────────────────────────────────────────
// Shell
// ─────────────────────────────────────────────

function tokenizeShell(code: string): Token[][] {
  return code.split('\n').map((line) => {
    const tokens: Token[] = []
    let i = 0
    let plain = ''

    const flushPlain = () => {
      if (plain) {
        tokens.push({ kind: 'plain', text: plain })
        plain = ''
      }
    }

    // shebang
    if (line.startsWith('#!')) {
      return [{ kind: 'meta', text: line }]
    }

    while (i < line.length) {
      const ch = line[i]

      // 注释：# 在行首或前面是空白时才是注释
      if (ch === '#' && (i === 0 || /\s/.test(line[i - 1]))) {
        flushPlain()
        tokens.push({ kind: 'comment', text: line.slice(i) })
        i = line.length
        continue
      }

      // 字符串
      if (ch === '"' || ch === "'") {
        flushPlain()
        const end = scanString(line, i)
        tokens.push({ kind: 'string', text: line.slice(i, end) })
        i = end
        continue
      }

      // 变量：$VAR 或 ${VAR}
      if (ch === '$') {
        flushPlain()
        let j = i + 1
        if (line[j] === '{') {
          while (j < line.length && line[j] !== '}') j += 1
          j += 1
        } else {
          while (j < line.length && isIdentPart(line[j])) j += 1
        }
        tokens.push({ kind: 'variable', text: line.slice(i, j) })
        i = j
        continue
      }

      // 长选项 --flag 与短选项 -x
      if (ch === '-' && (line[i + 1] === '-' || isIdentStart(line[i + 1] ?? ''))) {
        flushPlain()
        let j = i
        while (j < line.length && /[A-Za-z0-9-]/.test(line[j])) j += 1
        tokens.push({ kind: 'property', text: line.slice(i, j) })
        i = j
        continue
      }

      if (isIdentStart(ch)) {
        let j = i
        while (j < line.length && isIdentPart(line[j])) j += 1
        const word = line.slice(i, j)
        flushPlain()

        if (SHELL_KEYWORDS.has(word)) tokens.push({ kind: 'keyword', text: word })
        else if (SHELL_BUILTINS.has(word)) tokens.push({ kind: 'function', text: word })
        else tokens.push({ kind: 'plain', text: word })
        i = j
        continue
      }

      if (/[=|&;<>()]/.test(ch)) {
        flushPlain()
        tokens.push({ kind: 'operator', text: ch })
        i += 1
        continue
      }

      plain += ch
      i += 1
    }

    flushPlain()
    return tokens
  })
}

// ─────────────────────────────────────────────
// HTML
// ─────────────────────────────────────────────

function tokenizeHtml(code: string): Token[][] {
  return code.split('\n').map((line) => {
    const tokens: Token[] = []
    let i = 0
    let plain = ''

    const flushPlain = () => {
      if (plain) {
        tokens.push({ kind: 'plain', text: plain })
        plain = ''
      }
    }

    while (i < line.length) {
      // 注释 <!-- ... -->
      if (line.startsWith('<!--', i)) {
        flushPlain()
        const end = line.indexOf('-->', i)
        const stop = end === -1 ? line.length : end + 3
        tokens.push({ kind: 'comment', text: line.slice(i, stop) })
        i = stop
        continue
      }

      // 标签
      if (line[i] === '<') {
        flushPlain()
        let j = i + 1
        const isClosing = line[j] === '/'
        if (isClosing) j += 1

        // 标签名
        let nameEnd = j
        while (nameEnd < line.length && /[A-Za-z0-9-]/.test(line[nameEnd])) nameEnd += 1
        const name = line.slice(j, nameEnd)

        tokens.push({ kind: 'plain', text: isClosing ? '</' : '<' })
        if (name) {
          tokens.push({
            kind: HTML_KEYWORDS.has(name.toLowerCase()) ? 'keyword' : 'plain',
            text: name,
          })
        }

        // 属性直到 > 为止
        while (nameEnd < line.length && line[nameEnd] !== '>') {
          const ch = line[nameEnd]

          if (/\s/.test(ch)) {
            let k = nameEnd
            while (k < line.length && /\s/.test(line[k])) k += 1
            tokens.push({ kind: 'plain', text: line.slice(nameEnd, k) })
            nameEnd = k
            continue
          }

          if (ch === '"' || ch === "'") {
            const end = scanString(line, nameEnd)
            tokens.push({ kind: 'string', text: line.slice(nameEnd, end) })
            nameEnd = end
            continue
          }

          if (ch === '=') {
            tokens.push({ kind: 'operator', text: '=' })
            nameEnd += 1
            continue
          }

          if (/[A-Za-z-]/.test(ch)) {
            let k = nameEnd
            while (k < line.length && /[A-Za-z0-9-]/.test(line[k])) k += 1
            tokens.push({ kind: 'property', text: line.slice(nameEnd, k) })
            nameEnd = k
            continue
          }

          tokens.push({ kind: 'plain', text: ch })
          nameEnd += 1
        }

        if (nameEnd < line.length && line[nameEnd] === '>') {
          tokens.push({ kind: 'plain', text: '>' })
          nameEnd += 1
        }

        i = nameEnd
        continue
      }

      plain += line[i]
      i += 1
    }

    flushPlain()
    return tokens
  })
}

// ─────────────────────────────────────────────
// HTTP
// ─────────────────────────────────────────────

/**
 * HTTP 报文。这不是编程语言，高亮规则是另一套：
 *   - 请求行 / 状态行 → keyword
 *   - 头名 → property，头值 → plain（值里可能有敏感信息，不上色更好读）
 *   - JSON body → 交给简单的 JSON 规则
 */
function tokenizeHttp(code: string): Token[][] {
  const lines = code.split('\n')
  let inBody = false

  return lines.map((line) => {
    // 空行之后是 body
    if (line.trim() === '') {
      inBody = true
      return [{ kind: 'plain', text: line }]
    }

    if (inBody) {
      return tokenizeJsonish(line)
    }

    // 请求行：GET /path HTTP/1.1
    if (/^(GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)\s/.test(line)) {
      const [method, ...rest] = line.split(' ')
      const tokens: Token[] = [{ kind: 'keyword', text: method }]
      const restText = rest.join(' ')
      // 把路径与协议分开
      const m = restText.match(/^(\S+)(\s+)(HTTP\/[\d.]+)$/)
      if (m) {
        tokens.push({ kind: 'string', text: m[1] })
        tokens.push({ kind: 'plain', text: m[2] })
        tokens.push({ kind: 'meta', text: m[3] })
      } else {
        tokens.push({ kind: 'string', text: restText })
      }
      return tokens
    }

    // 状态行：HTTP/1.1 200 OK
    if (/^HTTP\/[\d.]+\s/.test(line)) {
      const m = line.match(/^(HTTP\/[\d.]+)(\s+)(\d{3})(\s*)(.*)$/)
      if (m) {
        return [
          { kind: 'meta', text: m[1] },
          { kind: 'plain', text: m[2] },
          { kind: 'number', text: m[3] },
          { kind: 'plain', text: m[4] },
          { kind: 'keyword', text: m[5] },
        ]
      }
      return [{ kind: 'meta', text: line }]
    }

    // 响应头 / 请求头：Name: value
    const header = line.match(/^([A-Za-z0-9-]+)(:\s*)(.*)$/)
    if (header) {
      return [
        { kind: 'property', text: header[1] },
        { kind: 'plain', text: header[2] },
        { kind: 'plain', text: header[3] },
      ]
    }

    return [{ kind: 'plain', text: line }]
  })
}

/** JSON 片段的高亮 —— 只处理键值，够读就行 */
function tokenizeJsonish(line: string): Token[] {
  const tokens: Token[] = []
  let i = 0
  let plain = ''

  const flushPlain = () => {
    if (plain) {
      tokens.push({ kind: 'plain', text: plain })
      plain = ''
    }
  }

  while (i < line.length) {
    const ch = line[i]

    if (ch === '"' || ch === "'") {
      flushPlain()
      const end = scanString(line, i)
      const text = line.slice(i, end)
      // 后面跟冒号 → 是键；否则是值
      const after = line.slice(end).trimStart()
      tokens.push({ kind: after.startsWith(':') ? 'property' : 'string', text })
      i = end
      continue
    }

    if (isDigit(ch) || (ch === '-' && isDigit(line[i + 1] ?? ''))) {
      flushPlain()
      let j = i + 1
      while (j < line.length && /[0-9.eE+-]/.test(line[j])) j += 1
      tokens.push({ kind: 'number', text: line.slice(i, j) })
      i = j
      continue
    }

    if (/[{}[\],:]/.test(ch)) {
      flushPlain()
      tokens.push({ kind: 'operator', text: ch })
      i += 1
      continue
    }

    plain += ch
    i += 1
  }

  flushPlain()
  return tokens
}
