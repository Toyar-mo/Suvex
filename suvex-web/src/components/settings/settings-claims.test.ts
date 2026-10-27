/**
 * 设置页「文案 ≠ 行为」回归守卫。
 *
 * ## 为什么这类问题值得单独一批测试
 *
 * 这个仓库的注释写得极详尽，但注释**用户看不见**。真正会伤人的是
 * 界面上那句灰字：用户按它理解系统，而系统按代码行事。两者不一致时，
 * 报错的是用户，不是代码 —— 没有任何测试会红。
 *
 * 2026-10-26 审计实际抓到两处，且都是**说反话**（不是没说，是说了相反的）：
 *
 *   1. 「禁止沙箱访问外网」写着"针对外部目标的扫描会直接失败"。
 *      实际 `agent_runner.py:1137` 是
 *      `network_isolated = bool(spec.sandbox_network_isolated) and not is_network_target`
 *      —— url/service 目标**强制开网**。用户按文案以为开了开关就安全，
 *      以为扫描失败是自己配错了；实际开关对网络目标毫无作用。
 *
 *   2. 「发现严重问题时立即通知」写着"不等扫描结束，边跑边发"。
 *      实际 `notify_on_critical_immediate` 在 `app/settings/` 之外
 *      **零消费端**：`dispatch.should_notify` 自己就写着"本轮只在收尾投递"。
 *      用户以为拿到了实时告警，实际永远收不到。
 *      —— **v0.90 已修**：`scheduler._import_for` 在新建成 critical 记录时
 *      入队 `scan.critical`，`dispatch` 按 `payload.event` 分派到
 *      `should_notify_critical`。下面的守卫已反转成"不许再说它不生效"。
 *
 *   3. 「发送测试消息」写着"不会真的发送"。后端 v0.89 起
 *      `test_channel` 改成形状校验 → **真发一次** → 按真实结果写 verified。
 *      用户以为点了没事，实际一条消息会到达他的 Slack / 工单系统。
 *
 * ## 判定方式
 *
 * 直接读源文件断言**关键措辞**。这看起来笨，但它是唯一能守住这类问题的方式：
 * 组件的 DOM 快照里那句话本来就是对的（它渲染的正是那句错话），
 * 只有把"这句话里不许出现什么"写成断言，改错的人才会被绊住。
 *
 * ⚠️ 断言的是**语义关键词**，不是整句原文 —— 允许改写措辞，不允许说反。
 */
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'

import { describe, expect, it } from 'vitest'

const SETTINGS_DIR = resolve(__dirname)

function source(name: string): string {
  return readFileSync(resolve(SETTINGS_DIR, name), 'utf8')
}

/**
 * 去掉注释，只留真正会渲染出去的部分。
 *
 * 这一步是必须的：这个仓库的注释里**如实记录着**曾经的错话
 * （"这里原来写着……"），不去掉就会把历史说明当成现行文案误判。
 */
function userVisible(src: string): string {
  return src
    .replace(/\/\*[\s\S]*?\*\//g, '')
    .replace(/^\s*\/\/.*$/gm, '')
}

describe('SandboxPanel 的隔离文案', () => {
  const visible = userVisible(source('SandboxPanel.tsx'))

  it('禁止出网必须写明只对本地目标生效', () => {
    // 代码：network_isolated = settings and not is_network_target
    expect(visible).toContain('只对本地')
    expect(visible).toContain('强制开网')
  })

  it('不得再声称外部目标会因隔离而失败', () => {
    // 旧文案："针对 https:// 目标的动态扫描将无法执行" —— 与代码相反
    expect(visible).not.toContain('将无法执行')
    expect(visible).not.toContain('扫描会直接失败')
  })

  it('不得再出现「仅自研引擎生效」这类后端限定语', () => {
    // 2026-10-26 用户明确：现在没有 strix 相关的东西了，只有一个 Suvex 内核
    // （`core/config.py:126` `scan_runner` 默认 `agent`）。既然只有一个引擎，
    // 「仅自研引擎生效（strix 后端不读）」就不再排除任何东西 —— 它只会让用户
    // 以为自己装的是两套里的一套，或者以为换成另一个后端就能得到不同行为。
    // 过时的限定语与虚假的能力宣称是同一种失真：都在描述一个不存在的世界。
    expect(visible).not.toContain('仅自研引擎生效')
    expect(visible.toLowerCase()).not.toContain('strix')
  })

  it('单次超时与允许写入挂载卷都是无条件生效的（不该再带前提）', () => {
    // 两者在 agent_runner.py 里都被真消费（:604/:672 设 deadline、:712 取消循环）
    // 且这是唯一的执行后端，所以界面上它们不需要任何前置条件。
    expect(visible).toContain('超时收尾')
    expect(visible).toContain('容器销毁即消失')
  })
})

describe('NotifyPanel 的即时通知与测试发送文案', () => {
  const visible = userVisible(source('NotifyPanel.tsx'))

  it('「立即通知」不得再声称不生效（v0.90 起它真的生效了）', () => {
    /**
     * 2026-10-26 审计时这里写着「⚠️ 当前版本不生效：…没有任何投递路径读它」，
     * 因为那时 `notify_on_critical_immediate` 在 `app/settings/` 之外零消费端。
     *
     * v0.90 接上了：`scheduler._import_for` 在**新建**出 critical 记录时
     * 入队一条 `scan.critical`，`dispatch` 按 `payload.event` 分派到
     * `should_notify_critical`（后者真的读这个开关）。
     *
     * ★ 这条守卫是**反过来的**同名测试：它原来要求文案承认"不生效"，
     *   现在要求文案**不许**再这么说。留着旧方向就会保护一个已修好的缺陷 ——
     *   用户看到"不生效"就不去开它，等于把刚接上的功能重新关掉。
     */
    expect(visible).not.toContain('不生效')
    expect(visible).not.toContain('没有任何投递路径读它')
  })

  it('必须写明它的三条真实边界（不写就是新的失真）', () => {
    /**
     * 一个"生效了"的开关比"不生效"更需要把边界说清 —— 否则用户会
     * 按自己的想象去配它。三条都对应代码里的具体行为：
     *
     * 1. 不受阈值约束 —— `should_notify_critical` 不看 `min_severity`
     *    （事件本身就是 critical，阈值是收尾判断）；
     * 2. 与「扫描结束时通知」互不影响 —— 两者互不蕴含，见
     *    `test_critical_immediate_on_means_alert_even_if_complete_off`
     *    （`suvex-api/tests/test_notice_dispatch.py`）；
     * 3. 每条扫描最多一次 —— `scheduler._critical_alerted` 去重。
     */
    expect(visible).toContain('不受上面的阈值约束')
    expect(visible).toContain('互不影响')
    expect(visible).toContain('只发一条')
  })

  it('不得再声称"不等扫描结束"这类把缺失当卖点的措辞', () => {
    // 旧文案："不等扫描结束，边跑边发 —— 早一分钟知道就少一分钟暴露窗口。"
    // 现在能力是真的，但这句话本身是个空洞的修辞（它承诺的正是
    // `notify_on_critical_immediate` 的语义），换成明确的条件描述更好：
    // 出了问题才发、发什么级别、多久一次。断言它不再以口号形式出现。
    expect(visible).not.toContain('不等扫描结束')
    expect(visible).not.toContain('早一分钟知道')
  })

  it('测试发送不得再声称"不会真的发送"', () => {
    // 代码：router.test_channel 在 v0.89 起真的发一次
    expect(visible).not.toContain('不会真的发送')
    expect(visible).not.toContain('只做本地校验')
    expect(visible).toContain('真的收到')
  })

  it('「扫描结束时通知」必须写明受阈值约束（干净扫描默认不发）', () => {
    /**
     * 旧文案："即使没有发现问题也发一条，用于确认扫描确实跑完了。"
     *
     * 只有阈值选到「信息」时才成立。代码依据（`dispatch.should_notify`）：
     *   干净扫描 severity = info（_SEVERITY_RANK 里 rank 0），
     *   默认 min_severity = high（rank 3），`_passes_severity` 判 `0 >= 3` 为假
     *   ⇒ `notify_on_scan_complete` 之后这层**再过滤掉**，不发。
     *
     * 这条最危险的地方：用户打开它的全部目的就是"确认扫描跑完了"，
     * 而默认配置下它恰恰不给他这个回执 —— 沉默会被解读为"没扫完"。
     */
    expect(visible).not.toContain('即使没有发现问题也发一条')
    expect(visible).toContain('受上面的阈值约束')
  })

  it('阈值选到「信息」时才承诺每次都发（前提写在文案里）', () => {
    // 两种分支都要有：选 info 说"包括没有发现问题"，否则说"低于阈值不发"
    expect(visible).toContain('阈值已选到')
    expect(visible).toContain('想每次都收到回执')
  })
})

describe('ScanRetentionPanel 的产物清理文案', () => {
  const visible = userVisible(source('ScanRetentionPanel.tsx'))

  it('产物清理不得声称 challenge 目录不受影响（它会跟着删）', () => {
    /**
     * 旧文案："产物目录（扫描日志、报告素材）再留…。附件原件与
     *         challenge 目录不受影响。"
     *
     * 后半句是错的。`runner.challenge_dir()` 返回
     * `scan_root / scan_id / CHALLENGE_DIRNAME` —— challenge/ 就在
     * `<scan_id>/` 里面；`remove_scan_artifacts` 用
     * `shutil.rmtree(scan_root/<scan_id>)` 端掉整个目录，challenge/ 跟着走。
     *
     * `runner.py:715-718` 的注释本来就说对了（"challenge/ 跟着产物一起删
     * 是对的（它是派生物，从 _uploads/ 的原件还能再生成）"），是界面写反了。
     * 这比上一条更该咬住：清理是**不可逆**的，用户照着这句话去开清理，
     * 会以为自己保住了 challenge，其实没有。
     */
    expect(visible).not.toContain('challenge 目录不受影响')
    // 必须点明真正不受影响的是"原件"，并给出它在哪
    expect(visible).toContain('附件原件不受影响')
    expect(visible).toContain('_uploads')
  })
})

describe('settings-store 的测试发送分支', () => {
  const store = readFileSync(
    resolve(SETTINGS_DIR, '..', '..', 'data', 'settings-store.ts'),
    'utf8',
  )

  it('不得再描述 verified 恒为 false 的旧后端', () => {
    // 旧注释："后端 v0.22 只做本地校验 … verified 恒为 false"
    // 现在 verified=true 是常态，那句注释会让维护者以为成功分支是死的
    expect(store).not.toContain('`verified` 恒为 `false`')
  })

  it('成功分支必须真的可达（有 status ok 的返回）', () => {
    expect(store).toContain("status: 'ok'")
  })
})

describe('BACKEND_PENDING 死代码已清除', () => {
  /**
   * 这三个出口当时恒为 false / ''，唯一消费方（SettingsPage 的提示条）
   * 因此是**永久不可达分支**。留着它的代价是：下一个维护者会以为
   * 某个区块仍未交付 —— 而那正是它当初要防的那类失真。
   *
   * ⚠️ SettingsPage 要过 `userVisible`：它的注释里**如实记录着**这次删除
   *    （"这里曾挂着一条'尚未接入'…"）。不去注释就会把历史说明当现行代码。
   */
  const store = readFileSync(
    resolve(SETTINGS_DIR, '..', '..', 'data', 'settings-store.ts'),
    'utf8',
  )
  const page = userVisible(
    readFileSync(resolve(SETTINGS_DIR, '..', '..', 'pages', 'SettingsPage.tsx'), 'utf8'),
  )

  it('settings-store 不再导出恒 false 的开关', () => {
    expect(store).not.toMatch(/export const BACKEND_PENDING\b/)
    expect(store).not.toMatch(/export const HAS_PENDING_BLOCK\b/)
    expect(store).not.toMatch(/export const BACKEND_PENDING_NOTE\b/)
  })

  it('SettingsPage 不再渲染不可达的"尚未接入"分支', () => {
    expect(page).not.toContain('BACKEND_PENDING')
    expect(page).not.toContain('尚未接入')
  })

  it('已接入清单必须包含沙箱与通知（它们早已真实持久化）', () => {
    // 漏写 = 否认一个已经可靠的功能，与吹嘘没有的能力是同一种失真
    expect(page).toContain('沙箱与执行')
    expect(page).toContain('通知规则与渠道')
  })
})
