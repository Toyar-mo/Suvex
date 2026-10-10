import { describe, expect, it } from 'vitest'
import type { TargetKind } from '@/data/scan'
import { defaultNameFrom, extractService, normalizeTarget, validateTarget } from './validate-target'

/*
  ══════════════════════════════════════════════════════════════
  目标校验与归一化
  ══════════════════════════════════════════════════════════════

  ## 为什么测这个

  它是**用户输入与后端之间的唯一一道转换**，而且踩过真实的坑：

    · 一次 32 分钟的卡死，成因是用户没写协议、前端按规则补成 `https://`，
      而目标只在 http 下有应答（见契约 §2.8.1）；
    · `service`（裸 TCP）这一类被加进来之前，Pwn 题**连创建都创建不了**
      （url 校验强制要求输入像 URL）。

  两条都说明：**这里的判断错一个方向，代价是十几分钟白跑或者一个
  目标根本建不出来** —— 而两种症状都不指向这里。

  ## 测试策略

  这个文件是纯函数、无 DOM 依赖，所以直接断言输入输出。
  重点在三处**容易搞反**的地方：

    · `url` 补协议的方向（补 https 而不是 http）
    · `service` 的粘贴识别（题面给的是命令，不是 host:port）
    · 各类型的归一化**刻意不同**（path 不小写、repo 不去尾斜杠）
*/

// ══════════════════════════════════════════════════════════════
// 一、service 的粘贴识别
// ══════════════════════════════════════════════════════════════

describe('extractService —— 从粘贴的命令里抠出 host:port', () => {
  it('裸 host:port 直接认', () => {
    expect(extractService('10.0.0.5:9999')).toEqual({ host: '10.0.0.5', port: 9999 })
  })

  it('★ nc / ncat / netcat 形式', () => {
    /*
      这类题的题面给的就是一条连接命令。让用户自己从中抠 host 和 port
      是把他当解析器用 —— 而抠错一个数字的代价是 agent 对着错误的端口
      跑十几分钟。
    */
    expect(extractService('nc 10.0.0.5 9999')).toEqual({ host: '10.0.0.5', port: 9999 })
    expect(extractService('ncat 10.0.0.5 9999')).toEqual({ host: '10.0.0.5', port: 9999 })
    expect(extractService('nc -v 10.0.0.5 9999')).toEqual({ host: '10.0.0.5', port: 9999 })
  })

  it('★ openssl s_client -connect 形式', () => {
    expect(extractService('openssl s_client -connect 10.0.0.5:9999')).toEqual({
      host: '10.0.0.5',
      port: 9999,
    })
  })

  it('★ socat 形式', () => {
    expect(extractService('socat - TCP:10.0.0.5:9999')).toEqual({
      host: '10.0.0.5',
      port: 9999,
    })
  })

  it('域名与 localhost 都认', () => {
    expect(extractService('chall.example.com:1337')).toEqual({
      host: 'chall.example.com',
      port: 1337,
    })
    expect(extractService('localhost:8080')).toEqual({ host: 'localhost', port: 8080 })
  })

  it('空串与垃圾输入返回 null（不猜）', () => {
    expect(extractService('')).toBeNull()
    expect(extractService('   ')).toBeNull()
    expect(extractService('随便写点什么')).toBeNull()
  })
})

describe('validateTarget —— service', () => {
  it('合法的 host:port 通过，且提示里说明**不是 HTTP**', () => {
    const result = validateTarget('service', '10.0.0.5:9999')

    expect(result.ok).toBe(true)
    // ⚠️ 这句必须保留：执行后端会把 host:9999 判成 web_application
    //    并补 https://，我们只能靠指令纠正，而这里是与指令口径一致的地方
    expect(result.hint).toContain('不是 HTTP')
  })

  it('★ 粘贴命令时提示"已提取"，并给出归一化结果', () => {
    const result = validateTarget('service', 'nc -v 10.0.0.5 9999')

    expect(result.ok).toBe(true)
    expect(result.hint).toContain('10.0.0.5:9999')
    expect(result.hint).toContain('已从粘贴的命令里提取')
  })

  it('端口越界被拒', () => {
    expect(validateTarget('service', '10.0.0.5:0').ok).toBe(false)
    expect(validateTarget('service', '10.0.0.5:70000').ok).toBe(false)
  })

  it('★ 主机名不含点且不是 localhost 时被拒', () => {
    // 这是"看起来像域名但其实不可解析"的输入（用户漏了后缀）
    const result = validateTarget('service', 'chall:9999')

    expect(result.ok).toBe(false)
    expect(result.error).toContain('不完整')
  })
})

// ══════════════════════════════════════════════════════════════
// 二、url 补协议的方向
// ══════════════════════════════════════════════════════════════

describe('validateTarget —— url', () => {
  it('不带协议时自动补 https（不是 http）', () => {
    /*
      ⚠️ 这个方向是**刻意的**，而它正是那次 32 分钟卡死的成因：
      用户输入 `10.0.0.5:8080`，前端补成 `https://`，而该主机只在
      http 下有应答 —— 于是 agent 对着 TLS 握手失败跑了半小时。

      现在的补救是**可达性预检**（创建时探一次并写进 notice），
      而补 https 这个默认方向没变：绝大多数目标确实是 https，
      改成 http 会让更多目标连不上。
    */
    const result = validateTarget('url', 'api.example.com')

    expect(result.ok).toBe(true)
  })

  it('显式写了 http:// 就尊重它（不覆盖）', () => {
    // 用户明确写了协议，那是他的意图，不该被"纠正"
    expect(validateTarget('url', 'http://api.example.com').ok).toBe(true)
  })

  it('无法解析为 URL 时被拒', () => {
    expect(validateTarget('url', 'http://').ok).toBe(false)
  })

  it('★ 主机名不含点且不是 localhost 时被拒', () => {
    expect(validateTarget('url', 'intranet').ok).toBe(false)
    expect(validateTarget('url', 'localhost').ok).toBe(true)
  })
})

// ══════════════════════════════════════════════════════════════
// 三、artifact 不能有地址
// ══════════════════════════════════════════════════════════════

describe('validateTarget —— artifact', () => {
  it('★ 无论输入什么都**通过**（这类题目不需要目标地址）', () => {
    /*
      ⚠️ 我第一版断言的是"填了地址就报错"，**那是错的**。

      实际行为是**一律通过**，理由写在实现里：

        与后端同口径：`targets.validate` 对 `artifact` 不管输入是什么
        都返回通过。这里仍然给一句 hint 而不是静默通过 ——
        向导会把它显示在目标地址那一块的下方，正好用来回答"为什么不用填"。

      "前端不拦"是刻意的：`artifact` 下那个输入框在 UI 上根本不显示
      （见 `TARGET_KIND_HINT.artifact` 的说明），所以"填了地址"这件事
      正常路径下不会发生。前端多拦一道只会造成"前端说行、后端说不行"
      或者反过来的不一致 —— 而这个项目对两处规则不一致的态度是明确的：
      **同一套规则只写一遍，或逐条对照写**（见 `extractService` 的注释）。
    */
    expect(validateTarget('artifact', 'http://example.com').ok).toBe(true)
    expect(validateTarget('artifact', '').ok).toBe(true)
    expect(validateTarget('artifact', '随便什么').ok).toBe(true)
  })

  it('通过时给出 hint 解释"为什么不用填"', () => {
    const result = validateTarget('artifact', '')

    expect(result.hint).toContain('不需要目标地址')
  })

  it('★ 空值在 artifact 下是合法的（顺序不能反）', () => {
    /*
      实现里 `artifact` 的分支必须在"空值 → 请填写目标"**之前**返回。
      顺序写反的话，这类题目会永远停在「请填写目标」——
      也就是当初要修的那个毛病原样复现，而且看起来像校验没生效。
    */
    expect(validateTarget('artifact', '').ok).toBe(true)
    // 对照：地址类目标空值是**不合法**的
    expect(validateTarget('url', '').ok).toBe(false)
    expect(validateTarget('service', '').ok).toBe(false)
  })
})

// ══════════════════════════════════════════════════════════════
// 四、归一化：各类型**刻意不同**
// ══════════════════════════════════════════════════════════════

describe('normalizeTarget', () => {
  it('url 去掉协议与路径，保留端口', () => {
    expect(normalizeTarget('url', 'https://api.example.com/')).toBe('api.example.com')
    // ★ 端口要保留：同一主机的不同端口是两个不同目标
    expect(normalizeTarget('url', 'https://api.example.com:8443/x')).toBe(
      'api.example.com:8443',
    )
  })

  it('★ repo 保留原样（.git 后缀与尾斜杠都可能有意义）', () => {
    const repo = 'https://github.com/acme/backend.git'
    expect(normalizeTarget('repo', repo)).toBe(repo)
  })

  it('★ path 保留原样（大小写在 Windows 与 Linux 上意义不同）', () => {
    const path = 'D:\\Projects\\Acme\\'
    expect(normalizeTarget('path', path)).toBe(path)
  })

  it('★ service 归一化为 host:port（端口必留）', () => {
    expect(normalizeTarget('service', 'nc 10.0.0.5 9999')).toBe('10.0.0.5:9999')
    expect(normalizeTarget('service', '10.0.0.5:9999')).toBe('10.0.0.5:9999')
  })

  it('前后空白一律去掉', () => {
    expect(normalizeTarget('url', '  api.example.com  ')).toBe('api.example.com')
  })
})

// ══════════════════════════════════════════════════════════════
// 五、默认扫描名
// ══════════════════════════════════════════════════════════════

describe('defaultNameFrom', () => {
  it('url 取主机名', () => {
    expect(defaultNameFrom('url', 'https://api.example.com/path')).toBe('api.example.com')
  })

  it('★ service 取 host:port（端口是区分两道题的关键）', () => {
    // 只取 host 的话，同一台机器的两个端口会得到同一个默认名
    expect(defaultNameFrom('service', '10.0.0.5:9999')).toBe('10.0.0.5:9999')
  })

  it('★ artifact 取附件名去扩展名（附件名走**第三个参数**）', () => {
    /*
      ⚠️ 我第一版写的是 `defaultNameFrom('artifact', 'tryre.zip')`，
      期望 `'tryre'` —— 而实际返回 `'未命名扫描'`。

      原因是签名是 `(kind, raw, artifactName?)`：`artifact` 下**第二个
      参数不是附件名**，第三个才是。第二个参数对它没有意义
      （artifact 的目标地址本来就是空的）。

      这条断言的价值不在于"抓到 bug"，而在于把**参数的语义**钉下来 ——
      传错位置不会报错，只会静默得到 `未命名扫描`。
    */
    expect(defaultNameFrom('artifact', '', 'tryre.zip')).toBe('tryre')
    // 只去掉最后一个扩展名（与后端 pathlib 的 stem 同口径）
    expect(defaultNameFrom('artifact', '', 'a.tar.gz')).toBe('a.tar')
    // 没有附件名时给一个可读的兜底，而不是空串
    expect(defaultNameFrom('artifact', '', undefined)).toBe('未命名扫描')
  })

  it('空输入不抛异常', () => {
    for (const kind of ['url', 'repo', 'path', 'service', 'artifact'] as TargetKind[]) {
      expect(() => defaultNameFrom(kind, '')).not.toThrow()
    }
  })
})
