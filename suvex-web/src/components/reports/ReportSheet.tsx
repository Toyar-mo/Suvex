import { cn } from '@/lib/cn'
import { PRINT_INK, PRINT_SEVERITY, PRINT_SYNTAX, type ReportFormat } from '@/data/report'
import { tokenize, type TokenKind } from '@/lib/highlight'
import { coverageNote, type ReportDocument, type ReportEntry } from '@/lib/report-document'
import { type CSSProperties, type ReactNode } from 'react'

/**
 * 报告纸 —— 报告的**唯一渲染源**。
 *
 * ★ 屏幕预览和 PDF 导出用的是**同一个组件、同一份 DOM**。
 *
 *   这不是偷懒，而是本轮验收标准（"预览效果和导出内容一致"）的实现方式：
 *   PDF 走浏览器打印引擎，打印的就是这个 DOM 本身，
 *   所以两者在内容与版式上不可能分叉。Markdown 导出读的是同一份 IR
 *   （见 report-document.ts 的说明），内容同样一致。
 *
 * ⚠️ 这个组件与站内其它组件的根本差异：**它是白底黑字的。**
 *    全站建立在近黑底上，signal 色是给深色底配的。报告要交付、要打印、
 *    要被钉在会议投屏上，它必须按纸来排版。所以这里：
 *      · 不用任何 surface-* / ink-* 语义色（那些是暗色的）
 *      · 不用 signal-* 严重度色（白底上 4/5 不达标）
 *      · 改用 PRINT_INK / PRINT_SEVERITY / PRINT_SYNTAX（全部按白底实测）
 *
 * 克制优先于炫技（验收要求：这是给非技术管理层看的）：
 *   - 没有入场动效、没有渐变、没有卡片阴影、没有大色块；
 *   - 层级只用「字号 + 1px 分隔线」表达，和站内是同一套手段，但用纸的灰阶；
 *   - 颜色只出现在**承载信息**的地方：严重度标记、图表条。
 *     其余一律灰阶 —— 一份五颜六色的安全报告会让人以为在卖东西。
 */

// ─────────────────────────────────────────────
// 纸面上的字号
// ─────────────────────────────────────────────

/**
 * 报告的字号不直接复用站内字阶。
 *
 * 原因：站内字阶是按**界面**调的（12px 起、行高 1.7），屏幕上没问题；
 * 但报告会被打印、被缩小、被投屏 —— 11~12px 的正文字在 A4 上偏小，
 * 且站内字阶的最大号（4xl 44px）对报告的标题过大。
 *
 * 这里另定一套：正文 13.5px（纸上约 10pt，正式文档的常见档位）、
 * 行高 1.75（中文要比拉丁更松，这条与站内一致）、标题逐级收紧。
 * 用内联 style 而不是 Tailwind 类：这些值**只属于报告**，
 * 塞进 tailwind.config 会让全局字阶多出一批只有报告用的档位。
 *
 * ⚠️ 写成 CSSProperties 对象而不是字符串。
 *    早先写成了 `'font-size:13.5px;...'` 这样的字符串，然后 `style={TYPE.body}`
 *    —— 编译期报 "has no properties in common"，`{...TYPE.body}` 展开更是直接把
 *    整条字符串拆成字符索引。React 的 style 只接受对象。
 */
const TYPE: Record<string, CSSProperties> = {
  docTitle: { fontSize: '22px', lineHeight: 1.4, fontWeight: 600, letterSpacing: 0 },
  sectionTitle: { fontSize: '16px', lineHeight: 1.5, fontWeight: 600, letterSpacing: 0 },
  entryTitle: { fontSize: '14.5px', lineHeight: 1.55, fontWeight: 600, letterSpacing: 0 },
  subTitle: { fontSize: '13px', lineHeight: 1.6, fontWeight: 600, letterSpacing: '0.01em' },
  body: { fontSize: '13.5px', lineHeight: 1.75, letterSpacing: '0.01em' },
  meta: { fontSize: '12px', lineHeight: 1.7, letterSpacing: '0.01em' },
  mono: { fontSize: '12px', lineHeight: 1.65, letterSpacing: 0 },
}

// ─────────────────────────────────────────────
// 主组件
// ─────────────────────────────────────────────

export interface ReportSheetProps {
  doc: ReportDocument
  /**
   * 导出格式。仅用于封面上的标识与 Markdown 在纸上的呈现差异，
   * **不影响内容** —— 两种格式的条目集合完全一致。
   */
  format?: ReportFormat
}

export function ReportSheet({ doc, format = 'pdf' }: ReportSheetProps) {
  return (
    <article
      /*
        `report-sheet` 这个类名是**打印样式的锚点**（见 index.css）：
        打印时只保留通往这个元素的那条链，其余全部隐藏。
        改名时务必同步改 index.css 里的那条选择器，否则导出的 PDF 里
        会混进侧栏和按钮。
      */
      className="report-sheet mx-auto w-full max-w-[52rem] rounded-md border border-[#D9DDE3] bg-white"
      style={{ color: PRINT_INK.body }}
      aria-label={`${doc.meta.title} 预览`}
    >
      <Cover doc={doc} format={format} />
      <Divider />
      <Summary doc={doc} />
      <Overview doc={doc} />
      <Entries doc={doc} />
      <Appendix doc={doc} />

      <footer className="px-[2.2rem] pb-[2rem] pt-[1.4rem]" style={TYPE.meta}>
        <div className="border-t pt-[0.9rem]" style={{ borderColor: PRINT_INK.rule }}>
          <span style={{ color: PRINT_INK.muted }}>
            {/* 草稿说"草稿"、不提"编号"：编号还没被分配，
                而"生成"二字用在尚未生成的稿子上自相矛盾 */}
            {doc.meta.provisional
              ? `Suvex · 草稿预览 · 编制于 ${doc.meta.generatedAt.slice(0, 10)}`
              : `本报告由 Suvex 自动生成 · 报告编号 ${doc.meta.code} · 生成于 ${doc.meta.generatedAt.slice(0, 10)}`}
          </span>
        </div>
      </footer>
    </article>
  )
}

// ─────────────────────────────────────────────
// 封面
// ─────────────────────────────────────────────

/**
 * 封面。
 *
 * 刻意不做"大字标题 + 渐变色带"那种设计：交付报告的封面只承担
 * 一件事 —— 让读者**一眼确认拿到的是哪一份、覆盖什么范围、什么时候生成的**。
 * 所以它是一张信息表，不是一个视觉海报。
 */
function Cover({ doc, format }: { doc: ReportDocument; format: ReportFormat }) {
  return (
    <header className="px-[2.2rem] pb-[1.6rem] pt-[2.4rem]">
      {/*
        ★ 标题上方**不放** kicker / eyebrow 小标签。

        这里原本有一行"安全检测报告"的眉标签。删掉它有两条依据：
          1. design-system §9 把"标题上方加 kicker / eyebrow 小标签"
             明确列在禁止项里，理由是"删除标签，让标题自己承重"；
          2. 它还是**冗余**的 —— 标题本身已经写着"acme-web 安全检测报告"，
             同一句话在标题上方再出现一次，读者先读到的是重复信息。
        视觉评审也独立指出了这一点（"眉标签与主标题文字重复，层级被削弱"）。
      */}
      <h1 style={TYPE.docTitle}>{doc.meta.title}</h1>

      {/* 元信息：6 个字段，13rem 下限在纸宽下正好落成 3 列 × 2 行 ——
          没有参差的尾行，也比两列时更省纵向空间 */}
      <dl
        className="mt-[1.5rem] grid gap-x-[2rem] gap-y-[0.55rem] border-t pt-[1.1rem]"
        style={{
          gridTemplateColumns: 'repeat(auto-fit, minmax(13rem, 1fr))',
          borderColor: PRINT_INK.rule,
        }}
      >
        {/* 草稿没有编号 —— 如实说"生成后分配"，不编一个将来会变的出来。
            见 ReportDocument.meta.provisional 的说明。 */}
        <MetaItem
          label="报告编号"
          value={doc.meta.provisional ? '生成后分配' : doc.meta.code}
          mono={!doc.meta.provisional}
        />
        <MetaItem label="覆盖范围" value={doc.meta.scope} />
        <MetaItem label="生成时间" value={doc.meta.generatedAt.slice(0, 10)} />
        <MetaItem label="导出格式" value={format === 'pdf' ? 'PDF' : 'Markdown'} />
        <MetaItem
          label="发现总数"
          value={doc.stats.total === 0 ? '未发现问题' : `${doc.stats.total} 条`}
        />
        {/* 用 method.targets（覆盖了哪些目标）而不是 stats.targets（发现了问题的目标）：
            没有收录条目时后者是 0，会写成"涉及目标 0 个" ——
            那读起来像"这次什么都没测"，与事实相反 */}
        <MetaItem label="涉及目标" value={`${doc.method.targets.length} 个`} />
      </dl>
    </header>
  )
}

function MetaItem({ label, value, mono }: { label: string; value: string; mono?: boolean }) {
  return (
    <div className="flex flex-col gap-[0.15rem]">
      <dt style={{ ...TYPE.meta, color: PRINT_INK.muted }}>{label}</dt>
      <dd className={cn('m-0', mono && 'font-mono')} style={TYPE.meta}>
        {value}
      </dd>
    </div>
  )
}

// ─────────────────────────────────────────────
// 一、摘要
// ─────────────────────────────────────────────

/**
 * 摘要。
 *
 * 这是全篇**唯一**会被完整读的一段（管理层通常止步于此），所以：
 *   - 第一句就是结论（riskVerdict 的产物），不让读者自己对数字；
 *   - 只有真的存在高危时才说"建议优先处置" —— 没有高危还喊，
 *     下次真有时就没人当回事了（狼来了）；
 *   - 被排除的条目数必须写出来（见下方 excludedCount）。
 */
function Summary({ doc }: { doc: ReportDocument }) {
  return (
    <Section title="一、摘要">
      <p style={TYPE.body}>{doc.verdict}</p>

      {doc.stats.urgent > 0 && (
        <p className="mt-[0.7rem]" style={TYPE.body}>
          其中 <Num>{doc.stats.urgent}</Num> 条为严重或高危，建议在下一轮迭代前完成处置。
        </p>
      )}

      {doc.stats.verified > 0 && (
        <p className="mt-[0.7rem]" style={TYPE.body}>
          本次共 <Num>{doc.stats.verified}</Num>{' '}
          条通过可复现的验证脚本确认。报告中的证据与复现步骤均取自 实际执行结果，未做推测性描述。
        </p>
      )}

      {/*
        ★ 排除必须写在报告里。
        这与 design-system §7「任何少算都要让用户看得出来」是同一条原则，
        只是这里读者从"用户"变成了"客户"：报告说"未发现问题"，
        而实际有 3 条待确认的没进来，不写清楚就是失真。
      */}
      {doc.meta.excludedCount > 0 && (
        <p className="mt-[0.7rem]" style={{ ...TYPE.body, color: PRINT_INK.muted }}>
          本报告仅收录已确认的问题，另有 <Num>{doc.meta.excludedCount}</Num>{' '}
          条待确认或已判定为误报的条目未纳入。
        </p>
      )}
    </Section>
  )
}

/** 数字统一等宽 —— 中文正文里的阿拉伯数字不等宽会让行看起来在抖 */
function Num({ children }: { children: ReactNode }) {
  return (
    <span className="font-mono" data-numeric style={{ fontWeight: 600 }}>
      {children}
    </span>
  )
}

// ─────────────────────────────────────────────
// 二、风险总览
// ─────────────────────────────────────────────

/**
 * 风险总览。
 *
 * 用**表格 + 水平条**，不用环形图/饼图：
 *   饼图要求读者比较角度，人眼对长度的判断远准于角度 ——
 *   五个级别的分布用水平条能一眼排出大小，饼图不能。
 *   这与 Overview 页的图表选型是同一条理由（design-system §7）。
 *
 * 颜色是**冗余编码**：每行都有文字标签与数字，颜色只是让"严重"跳出来。
 * 色觉障碍读者靠标签能完整读到信息。
 */
function Overview({ doc }: { doc: ReportDocument }) {
  if (doc.stats.total === 0) {
    return (
      <Section title="二、风险总览">
        <p style={TYPE.body}>本次范围内未发现需要报告的安全问题。</p>
      </Section>
    )
  }

  const max = Math.max(...doc.distribution.map((d) => d.count), 1)

  return (
    <Section title="二、风险总览">
      {/* ── 严重程度分布 ── */}
      <h3 style={TYPE.subTitle}>严重程度分布</h3>

      <table className="mt-[0.7rem] w-full border-collapse" style={TYPE.meta}>
        <thead>
          <tr>
            <Th align="left" width="5.5rem">
              严重程度
            </Th>
            <Th align="left">分布</Th>
            <Th align="right" width="3.5rem">
              数量
            </Th>
            <Th align="right" width="3.5rem">
              占比
            </Th>
          </tr>
        </thead>
        <tbody>
          {doc.distribution.map((row) => {
            const share = Math.round((row.count / doc.stats.total) * 100)
            const ink = PRINT_SEVERITY[row.severity].ink
            return (
              <tr key={row.severity}>
                <Td>
                  <span className="inline-flex items-center gap-[0.4rem]">
                    {/* 色点 + 文字标签：颜色是冗余的，标签才是信息本身 */}
                    <span
                      aria-hidden="true"
                      className="inline-block h-[0.5rem] w-[0.5rem] rounded-full"
                      style={{ backgroundColor: ink }}
                    />
                    {row.label}
                  </span>
                </Td>
                <Td>
                  {/* 水平条：宽度按最大值归一，不是按总数 —— 否则小值全都看不见 */}
                  <span
                    className="inline-block h-[0.5rem] rounded-[1px]"
                    style={{
                      width: `${Math.max((row.count / max) * 100, 2)}%`,
                      maxWidth: '14rem',
                      backgroundColor: ink,
                    }}
                  />
                </Td>
                <Td align="right">
                  <Num>{row.count}</Num>
                </Td>
                <Td align="right">
                  <span style={{ color: PRINT_INK.muted }}>{share}%</span>
                </Td>
              </tr>
            )
          })}
          <tr>
            <Td>
              <span style={{ fontWeight: 600 }}>合计</span>
            </Td>
            <Td />
            <Td align="right">
              <span style={{ fontWeight: 600 }}>
                <Num>{doc.stats.total}</Num>
              </span>
            </Td>
            <Td align="right">
              <span style={{ color: PRINT_INK.muted }}>100%</span>
            </Td>
          </tr>
        </tbody>
      </table>

      {/* ── 处置情况 ── */}
      <h3 className="mt-[1.6rem]" style={TYPE.subTitle}>
        处置情况
      </h3>
      <table className="mt-[0.7rem] w-full border-collapse" style={TYPE.meta}>
        <tbody>
          <StatRow label="已通过验证脚本确认" value={doc.stats.verified} />
          <StatRow label="已修复" value={doc.stats.fixed} />
          <StatRow label="待处置" value={doc.stats.total - doc.stats.fixed} />
        </tbody>
      </table>
    </Section>
  )
}

function StatRow({ label, value }: { label: string; value: number }) {
  return (
    <tr>
      <Td>{label}</Td>
      <Td align="right" width="5rem">
        <Num>{value}</Num>
      </Td>
    </tr>
  )
}

// ─────────────────────────────────────────────
// 三、漏洞详情
// ─────────────────────────────────────────────

function Entries({ doc }: { doc: ReportDocument }) {
  if (doc.entries.length === 0) {
    return (
      <Section title="三、漏洞详情">
        <p style={{ ...TYPE.body, color: PRINT_INK.muted }}>无。</p>
      </Section>
    )
  }

  return (
    <Section title="三、漏洞详情">
      <p className="mb-[1.4rem]" style={{ ...TYPE.meta, color: PRINT_INK.muted }}>
        以下 {doc.entries.length} 条按严重程度从高到低排列。
        {doc.meta.options.includeRemediation
          ? '每条附修复建议。'
          : '本报告未包含修复建议，可在系统中查看。'}
      </p>

      <div className="flex flex-col gap-[2rem]">
        {doc.entries.map((entry) => (
          <Entry key={entry.findingId} entry={entry} />
        ))}
      </div>
    </Section>
  )
}

/**
 * 单条漏洞。
 *
 * 信息层级按**读者的决策路径**排，不按数据字段顺序：
 *   标题（这是什么）
 *   → 属性行（多严重、在哪）
 *   → 问题描述（怎么回事）
 *   → 受影响位置（具体到哪个接口）
 *   → 判断依据（凭什么这么说，可选）
 *   → 证据（原始材料）
 *   → 复现方式（可选）
 *   → 修复建议
 *   → 处置记录（人做过什么）
 *
 * 为什么修复建议放在证据之后：管理层读到"证据"时已经理解了问题，
 * 建议才有落点。反过来先给建议，读者不知道要修的是什么 ——
 * 这和"先看结论再看细节"并不矛盾：**结论是标题和属性行，细节是证据**，
 * 而建议是对细节的应对，所以它排在细节之后。
 */
function Entry({ entry }: { entry: ReportEntry }) {
  const meta = PRINT_SEVERITY[entry.severity]

  return (
    /*
      report-entry 类名供打印分页用（见 index.css）：
      让一条漏洞尽量整体落在同一页，避免被劈成两页看起来像两条。
    */
    <section className="report-entry">
      <div className="flex items-start gap-[0.6rem]">
        {/* 序号用等宽，与正文里的交叉引用（附录/摘要）对得上 */}
        <span
          className="mt-[0.15rem] shrink-0 font-mono tabular-nums"
          style={{ ...TYPE.meta, color: PRINT_INK.muted }}
          data-numeric
        >
          {entry.index}
        </span>

        <h3 className="min-w-0 flex-1" style={TYPE.entryTitle}>
          {entry.title}
        </h3>
      </div>

      {/*
        属性行：严重程度用**带衬底的徽标**（ink 文字 + wash 底）。
        这是报告里少数几处用彩色底的地方 —— 因为它承载的是分级结论，
        读者扫一页时需要立刻看出哪几条是严重的。
        徽标里的 ink 对 wash 全部 >= 4.6:1（实测见 _tmp/print-syntax-check.mjs）。
      */}
      <div className="mt-[0.7rem] flex flex-wrap items-center gap-x-[0.9rem] gap-y-[0.4rem]">
        <span
          className="inline-flex items-center gap-[0.35rem] rounded-[3px] px-[0.45rem] py-[0.12rem]"
          style={{
            ...TYPE.meta,
            backgroundColor: meta.wash,
            color: meta.ink,
            fontWeight: 600,
          }}
        >
          {entry.severityLabel}
        </span>

        <Attr label="类型" value={entry.categoryLabel} />
        <Attr label="目标" value={entry.target} mono />
        <Attr label="验证程度" value={entry.verificationLabel} />
        <Attr label="处置状态" value={entry.statusLabel} />
        <Attr label="发现时间" value={entry.discoveredAt} />
      </div>

      <div className="mt-[1rem] flex flex-col gap-[1.1rem] pl-[1.4rem]">
        <Field label="问题描述">
          <p style={TYPE.body}>{entry.description}</p>
        </Field>

        {entry.affected.length > 0 && (
          <Field label="受影响位置">
            <ul className="m-0 flex list-none flex-col gap-[0.2rem] p-0">
              {entry.affected.map((location) => (
                <li key={location} className="font-mono" style={TYPE.mono}>
                  {location}
                </li>
              ))}
            </ul>
          </Field>
        )}

        {/* 判断依据是可选项 —— 关掉时整段不出现，不留空标题 */}
        {entry.reasoning && (
          <Field label="判断依据">
            <p className="whitespace-pre-line" style={{ ...TYPE.body, color: PRINT_INK.muted }}>
              {entry.reasoning}
            </p>
          </Field>
        )}

        <Field label="证据">
          <CodePanel code={entry.evidence} language="http" />
        </Field>

        {entry.poc && (
          <Field label="复现方式">
            <CodePanel code={entry.poc.code} language={entry.poc.language} />
          </Field>
        )}

        {entry.remediation && (
          <Field label="修复建议">
            <p className="whitespace-pre-line" style={TYPE.body}>
              {entry.remediation}
            </p>
          </Field>
        )}

        {entry.dispositions.length > 0 && (
          <Field label="处置记录">
            <ul className="m-0 flex list-none flex-col gap-[0.25rem] p-0" style={TYPE.meta}>
              {entry.dispositions.map((item, i) => (
                <li key={i} className="flex gap-[0.6rem]">
                  <span
                    className="shrink-0 tabular-nums"
                    style={{ color: PRINT_INK.muted }}
                    data-numeric
                  >
                    {item.at}
                  </span>
                  <span>{item.summary}</span>
                </li>
              ))}
            </ul>
          </Field>
        )}
      </div>

      {/* 条目之间的分隔线。用线不用间距，因为间距已经在排版里承担了层级 */}
      <div className="mt-[1.4rem] h-px w-full" style={{ backgroundColor: PRINT_INK.rule }} />
    </section>
  )
}

/** 属性：`标签 值` 的行内对。标签用弱色，值用正文色 */
function Attr({ label, value, mono }: { label: string; value: string; mono?: boolean }) {
  return (
    <span className="inline-flex items-center gap-[0.35rem]" style={TYPE.meta}>
      <span style={{ color: PRINT_INK.muted }}>{label}</span>
      <span className={cn(mono && 'font-mono')}>{value}</span>
    </span>
  )
}

/**
 * 字段块：小标题 + 内容。
 *
 * 小标题用**弱色小字**而不是加粗黑体：报告里加了倍的字段名会和
 * 正文里的强调混在一起，读者分不清"这是结构"还是"这是重点"。
 * 结构靠弱色区分，重点靠加粗 —— 两套手段各管一件事。
 */
function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div>
      <p
        className="mb-[0.35rem]"
        style={{ ...TYPE.meta, color: PRINT_INK.muted, fontWeight: 600, letterSpacing: '0.04em' }}
      >
        {label}
      </p>
      {children}
    </div>
  )
}

// ─────────────────────────────────────────────
// 代码 / 证据块
// ─────────────────────────────────────────────

/**
 * 纸面上的代码块。
 *
 * 复用站内的分词器（lib/highlight.ts），但**换用印刷色板** ——
 * 站内的 syntax 色是按近黑底配的，印在白纸上关键字只有 1.9:1。
 *
 * 三个与前一条一致的取舍：
 *   1. 渲染成 React 元素，不用 dangerouslySetInnerHTML ——
 *      内容来自 agent 产出，是不可信数据；
 *   2. 不软换行（whitespace-pre + overflow-x-auto）——
 *      日志的换行位置携带信息；
 *   3. 纸面上**不加行号**：报告里的证据是给人读的片段，不是要定位的源码。
 *      行号在屏幕上帮助定位，在纸上只是噪音。
 */
function CodePanel({ code, language }: { code: string; language: string }) {
  const lines = tokenize(code, language)

  return (
    <div
      className="overflow-hidden rounded-[3px] border"
      style={{ borderColor: PRINT_INK.rule, backgroundColor: PRINT_INK.wash }}
    >
      <div className="overflow-x-auto px-[0.8rem] py-[0.6rem]">
        <pre className="m-0" style={TYPE.mono}>
          <code className="font-mono">
            {lines.map((tokens, lineIndex) => (
              <span key={lineIndex} className="block whitespace-pre">
                {/* 空行也要占高度，否则行数看着会少 */}
                {tokens.length === 0 ? '\u00A0' : null}
                {tokens.map((token, tokenIndex) => (
                  <span key={tokenIndex} style={{ color: syntaxColor(token.kind) }}>
                    {token.text}
                  </span>
                ))}
              </span>
            ))}
          </code>
        </pre>
      </div>
    </div>
  )
}

/** token 类型 → 印刷色。缺失的类别退化为正文色，不会渲染成透明 */
function syntaxColor(kind: TokenKind): string {
  return PRINT_SYNTAX[kind] ?? PRINT_INK.body
}

// ─────────────────────────────────────────────
// 附录
// ─────────────────────────────────────────────

function Appendix({ doc }: { doc: ReportDocument }) {
  return (
    <>
      <Method doc={doc} />
      <SeverityKey doc={doc} />
      <ContentScope doc={doc} />
    </>
  )
}

/** 附录 A：范围与方法 —— 交代覆盖边界，让结论的分量可被判断 */
function Method({ doc }: { doc: ReportDocument }) {
  const m = doc.method

  return (
    <Section title="附录 A　检测范围与方法">
      <dl className="flex flex-col gap-[0.7rem]">
        {m.targets.length > 0 && (
          <AppendixItem label="覆盖目标">
            <ul className="m-0 flex list-none flex-col gap-[0.15rem] p-0">
              {m.targets.map((target) => (
                <li key={target} className="font-mono" style={TYPE.mono}>
                  {target}
                </li>
              ))}
            </ul>
          </AppendixItem>
        )}

        {m.span && (
          <AppendixItem label="发现时间跨度">
            <span style={TYPE.meta}>
              {m.span.from} 至 {m.span.to}
            </span>
          </AppendixItem>
        )}

        {m.skills.length > 0 && (
          <AppendixItem label="启用的检测能力">
            <span style={TYPE.meta}>{m.skills.join('、')}</span>
          </AppendixItem>
        )}

        {m.scans.length > 0 && (
          <AppendixItem label="关联的扫描任务">
            <div style={TYPE.meta}>
              {m.scans.map((scan) => (
                <div key={scan.id} className="flex flex-wrap gap-x-[0.6rem]">
                  <span>{scan.name}</span>
                  <span className="font-mono" style={{ color: PRINT_INK.muted }}>
                    {scan.target}
                  </span>
                  <span className="tabular-nums" style={{ color: PRINT_INK.muted }} data-numeric>
                    {scan.finishedAt ? scan.finishedAt.slice(0, 10) : '未完成'}
                  </span>
                  {/* 覆盖完整性：未跑完的扫描必须显式标出，否则读者会
                      以为整个目标都测过了 —— 那是把漏报伪装成"没问题" */}
                  <span
                    style={{
                      color:
                        scan.status === 'completed' ? PRINT_INK.muted : PRINT_SEVERITY.high.ink,
                    }}
                  >
                    {coverageNote(scan.status)}
                  </span>
                </div>
              ))}
            </div>
          </AppendixItem>
        )}
      </dl>

      {/*
        中断提醒。只要范围内有被终止的扫描就必须出现 ——
        读者不能以为整个目标都被测过了。与 Markdown 导出给的是同一句话
        （coverageNote 与这段文案都定义在 IR 层/此处共用一份口径）。
      */}
      {m.scans.some((s) => s.status === 'aborted') && (
        <p className="mt-[0.9rem]" style={{ ...TYPE.meta, color: PRINT_SEVERITY.high.ink }}>
          其中 {m.scans.filter((s) => s.status === 'aborted').length}{' '}
          次扫描被提前终止，对应目标的检测覆盖不完整，相关结论仅代表已执行范围内的结果。
        </p>
      )}

      {/*
        边界说明。
        这段话不是免责套话 —— 它明确告诉读者"没写的问题不代表不存在"，
        是安全报告可信度的边界所在，缺失会让报告显得比实际更权威。
      */}
      <p className="mt-[1rem]" style={{ ...TYPE.meta, color: PRINT_INK.muted }}>
        本报告覆盖上述目标在指定范围内的检测结果。检测以自动化的黑盒与灰盒方式为主，未覆盖的目标、
        未启用的检测能力以及业务逻辑层面的复杂组合场景不在此次范围内。
      </p>
    </Section>
  )
}

function AppendixItem({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div>
      <dt style={{ ...TYPE.meta, color: PRINT_INK.muted, fontWeight: 600 }}>{label}</dt>
      <dd className="m-0 mt-[0.15rem]">{children}</dd>
    </div>
  )
}

/** 附录 B：严重程度说明 —— 让"高危"对非技术读者是可解释的 */
function SeverityKey({ doc }: { doc: ReportDocument }) {
  return (
    <Section title="附录 B　严重程度说明">
      <table className="w-full border-collapse" style={TYPE.meta}>
        <thead>
          <tr>
            <Th align="left" width="6rem">
              严重程度
            </Th>
            <Th align="left">判定含义</Th>
          </tr>
        </thead>
        <tbody>
          {doc.severityDefinitions.map((item) => (
            <tr key={item.severity}>
              <Td>
                <span className="inline-flex items-center gap-[0.4rem]">
                  <span
                    aria-hidden="true"
                    className="inline-block h-[0.5rem] w-[0.5rem] rounded-full"
                    style={{ backgroundColor: PRINT_SEVERITY[item.severity].ink }}
                  />
                  {item.label}
                </span>
              </Td>
              <Td>{item.definition}</Td>
            </tr>
          ))}
        </tbody>
      </table>

      <p className="mt-[0.9rem]" style={{ ...TYPE.meta, color: PRINT_INK.muted }}>
        严重程度描述问题被利用的难易与影响面，不等同于业务优先级。
        实际处置顺序建议结合资产重要性与业务影响一并判断。
      </p>
    </Section>
  )
}

/** 附录 C：内容范围 —— 报告自称包含什么，必须写清楚且与实际一致 */
function ContentScope({ doc }: { doc: ReportDocument }) {
  const o = doc.meta.options

  return (
    <Section title="附录 C　内容范围">
      <dl className="flex flex-col gap-[0.4rem]" style={TYPE.meta}>
        <AppendixItem label="修复建议">{o.includeRemediation ? '包含' : '未包含'}</AppendixItem>
        <AppendixItem label="验证脚本（PoC）">{o.includePoc ? '包含' : '未包含'}</AppendixItem>
        <AppendixItem label="判断依据（智能体推理过程）">
          {o.includeReasoning ? '包含' : '未包含'}
        </AppendixItem>
        <AppendixItem label="条目筛选">
          {o.onlyConfirmed ? '仅已确认的问题' : '全部状态的问题'}
        </AppendixItem>
      </dl>

      <p className="mt-[0.9rem]" style={{ ...TYPE.meta, color: PRINT_INK.muted }}>
        未包含的内容可在系统内的漏洞发现页面查看，或重新生成包含这些内容的报告。
        报告内容在生成时固定，不随漏洞处置状态的变化而改变。
      </p>
    </Section>
  )
}

// ─────────────────────────────────────────────
// 版面原子
// ─────────────────────────────────────────────

/** 章节。一级结构：大标题独占一行，上方留白大于下方 */
function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section className="px-[2.2rem] py-[1.6rem]">
      <h2 className="mb-[1rem]" style={TYPE.sectionTitle}>
        {title}
      </h2>
      {children}
    </section>
  )
}

/** 章节之间的分隔线。用线而非空白，因为纸面上空白会被分页吃掉 */
function Divider() {
  return <div className="h-px w-full" style={{ backgroundColor: PRINT_INK.rule }} />
}

/**
 * 表格单元。
 *
 * 用真实 <table> 而不是 div 网格：
 *   报告会被打印、也可能被复制进 Word/邮件，表格语义能让这些下游工具
 *   正确识别结构。而且打印时 <tr> 的 `break-inside: avoid` 才生效。
 */
function Th({
  children,
  align = 'left',
  width,
}: {
  children?: ReactNode
  align?: 'left' | 'right'
  width?: string
}) {
  return (
    <th
      scope="col"
      style={{
        ...TYPE.meta,
        width,
        textAlign: align,
        color: PRINT_INK.muted,
        fontWeight: 600,
        paddingBottom: '0.4rem',
        borderBottom: `1px solid ${PRINT_INK.rule}`,
      }}
    >
      {children}
    </th>
  )
}

function Td({
  children,
  align = 'left',
  width,
}: {
  children?: ReactNode
  align?: 'left' | 'right'
  width?: string
}) {
  return (
    <td
      style={{
        ...TYPE.meta,
        width,
        textAlign: align,
        padding: '0.4rem 0',
        borderBottom: `1px solid ${PRINT_INK.wash}`,
      }}
    >
      {children}
    </td>
  )
}

export default ReportSheet
