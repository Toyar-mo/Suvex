import type { ExtraLocaleMap } from './index'

/** 报告页、报告纸、历史列表与题解弹窗的文案。 */
export const reports: ExtraLocaleMap = {
  'zh-CN': {
    // 页面框架
    'report.view.aria': '报告视图',
    'report.view.compose': '生成报告',
    'report.view.history': '历史报告',
    'report.view.historyCount': '历史报告（{n}）',
    'report.title.label': '报告标题',
    'report.format.label': '导出格式',
    'report.generate.action': '生成并导出',
    'report.generate.loading': '正在生成…',
    'report.generate.pdfHint':
      '将调用浏览器打印，在目标处选择「另存为 PDF」。生成的版本与右侧预览一致。',
    'report.preview.updating': '正在更新…',
    'report.loadingContent': '正在加载报告内容',

    // 错误与提示
    'report.error.scopeLoad': '数据范围加载失败',
    'report.error.previewLoad': '报告内容加载失败',
    'report.error.previewGenerate': '报告预览生成失败',
    'report.error.historyLoad': '报告历史加载失败',
    'report.error.pendingPrint': '报告已生成，但预览内容没能加载出来，请在历史列表里重新下载。',
    'report.error.emptySelection': '当前范围与选项下没有可导出的条目，请调整后再生成。',
    'report.notice.generatedDownloaded': '已生成并下载 {filename}',
    'report.notice.downloaded': '已下载 {filename}',
    'report.notice.printOpened': '已打开打印窗口 —— 在目标处选择「另存为 PDF」即可导出。',
    'report.notice.deleted': '已删除「{title}」',

    // 标题默认值
    'report.title.default': '安全检测报告',
    'report.title.scan': '{name} 安全检测报告',
    'report.title.range': '{from} 至 {to} 安全检测报告',
    'report.title.findings': '手选 {n} 条漏洞的安全报告',

    // 范围描述
    'report.scope.scan': '扫描任务「{name}」',
    'report.scope.findings': '手选的 {n} 条漏洞',
    'report.scope.target': '目标 {target}',
    'report.scope.range': '{from} 至 {to}',

    // 空态
    'report.empty.noScans.title': '还没有可出报告的扫描',
    'report.empty.noScans.body':
      '报告来自已经跑完的扫描。发起并完成一次扫描之后，就能在这里把它整理成可交付的报告。',
    'report.history.empty.title': '还没有生成过报告',
    'report.history.empty.body':
      '在「生成报告」里选择范围与内容即可产出第一份。已生成的报告会保留当时的条目与口径，不会随漏洞状态的变化而改变。',
    'report.emptyPreview.select': '从下方列表中选择一份报告',
    'report.emptyPreview.noContent': '当前范围下没有可预览的内容',
    'report.emptyPreview.selectHint': '选中后会在这里显示它与当初生成时一致的内容。',
    'report.emptyPreview.noContentHint': '换一个数据范围，或减少筛选条件。',

    // 预览标识
    'report.caption.history': '历史报告预览 · 内容在生成时固定，不随漏洞状态变化',
    'report.caption.compose': '草稿预览 · 与导出的 PDF / Markdown 内容一致；报告编号在生成时分配',

    // 生成口径卡片
    'report.meta.title': '生成口径',
    'report.meta.desc': '这份报告在生成时固定了条目与选项，之后不会改变。重新下载得到的是当初那一份。',
    'report.meta.scope': '覆盖范围',
    'report.meta.count': '条目数',
    'report.meta.findingCount': '{n} 条',
    'report.meta.excluded': '已排除',
    'report.meta.excludedValue': '{n} 条（按当时的选项）',
    'report.meta.format': '导出格式',
    'report.meta.remediation': '修复建议',
    'report.meta.poc': '验证脚本',
    'report.meta.reasoning': '判断依据',
    'report.meta.filter': '条目筛选',
    'report.meta.included': '包含',
    'report.meta.notIncluded': '未包含',
    'report.meta.confirmedOnly': '仅已确认',
    'report.meta.allStatuses': '全部状态',
    'report.meta.redownload': '重新下载',

    // 范围配置
    'report.scope.heading': '数据范围',
    'report.scope.typeAria': '数据范围类型',
    'report.scopeKind.scan': '某次扫描',
    'report.scopeKind.target': '某个目标',
    'report.scopeKind.range': '时间区间',
    'report.scopeKind.findings': '手选的漏洞',
    'report.findings.receivedPrefix': '已接收',
    'report.findings.receivedSuffix': '条手选的漏洞。',
    'report.findings.receivedHint': '想换一批，回「漏洞发现」勾选后重新点「生成报告」。',
    'report.scope.selectScan': '选择扫描任务',
    'report.scope.selectTarget': '选择目标',
    'report.scope.scanOption': '{name}（{count} 条）',
    'report.scope.targetOption': '{name}（{count} 条）',
    'report.scope.noScans': '没有可用的扫描结果',
    'report.scope.noTargets': '没有可用的目标',
    'report.scope.timeRange': '时间范围',
    'report.scope.presetAria': '时间范围预设',
    'report.preset.7d': '最近 7 天',
    'report.preset.30d': '最近 30 天',
    'report.preset.all': '全部时间',
    'report.scope.from': '开始日期',
    'report.scope.to': '结束日期',

    // 内容选项
    'report.content.heading': '包含内容',
    'report.content.remediation': '修复建议',
    'report.content.remediationHint': '每条问题附上可执行的修复方向',
    'report.content.remediationEmpty': '本范围内没有可用的修复建议',
    'report.content.poc': '验证脚本（PoC）',
    'report.content.pocHint': '附上可复现问题的脚本与请求原文',
    'report.content.pocEmpty': '本范围内没有可复现的验证脚本',
    'report.content.reasoning': '判断依据',
    'report.content.reasoningHint': '智能体判定该问题存在的过程说明',
    'report.content.reasoningEmpty': '本范围内没有可用的判断依据',
    'report.content.confirmed': '只导出已确认的问题',
    'report.content.confirmedHint': '排除待确认与已判定为误报的条目',
    'report.content.confirmedEmpty': '本范围内没有待确认或误报的条目',
    'report.content.noEntries': '本范围内没有可导出的条目',

    // 命中预览
    'report.preview.containsPrefix': '报告将包含',
    'report.preview.findingSuffix': '条发现',
    'report.preview.excluded': '已按「只导出已确认」排除 {excluded} 条（命中 {hits} 条）',
    'report.preview.emptyScope': '该范围内没有发现，报告将说明「本次未发现问题」。',
    'report.preview.emptyFiltered': '当前条件下没有条目会被包含，请检查上面的选项。',

    // 勾选标记
    'report.include.aria': '包含',
    'report.exclude.aria': '不包含',

    // 历史列表
    'report.history.table.report': '报告',
    'report.history.table.findings': '发现',
    'report.history.table.created': '生成于',
    'report.history.table.actions': '操作',
    'report.history.openPreviewTitle': '打开「{title}」的预览',
    'report.history.zeroExcluded': '0 条（排除 {n} 条）',
    'report.history.none': '未发现问题',
    'report.history.excluded': '排除 {n} 条',
    'report.history.generating': '生成中…',
    'report.history.download': '下载',
    'report.history.open': '打开',
    'report.history.confirmDelete': '确认删除',
    'report.history.cancel': '取消',
    'report.history.deleteAria': '删除「{title}」',

    // 报告纸
    'report.sheet.previewAria': '{title} 预览',
    'report.sheet.footerDraft': 'Suvex · 草稿预览 · 编制于 {date}',
    'report.sheet.footerFinal': '本报告由 Suvex 自动生成 · 报告编号 {code} · 生成于 {date}',
    'report.sheet.reportCode': '报告编号',
    'report.sheet.assignedOnGenerate': '生成后分配',
    'report.sheet.scope': '覆盖范围',
    'report.sheet.generatedAt': '生成时间',
    'report.sheet.format': '导出格式',
    'report.sheet.totalFindings': '发现总数',
    'report.sheet.noFindings': '未发现问题',
    'report.sheet.count': '{n} 条',
    'report.sheet.targets': '涉及目标',
    'report.sheet.targetCount': '{n} 个',
    'report.sheet.severity': '严重程度',
    'report.sheet.distribution': '分布',
    'report.sheet.countLabel': '数量',
    'report.sheet.share': '占比',
    'report.sheet.total': '合计',

    // 摘要 / 总览
    'report.section.summary': '一、摘要',
    'report.summary.urgentPrefix': '其中',
    'report.summary.urgentSuffix': '条为严重或高危，建议在下一轮迭代前完成处置。',
    'report.summary.verifiedPrefix': '本次共',
    'report.summary.verifiedSuffix':
      '条通过可复现的验证脚本确认。报告中的证据与复现步骤均取自 实际执行结果，未做推测性描述。',
    'report.summary.excludedPrefix': '本报告仅收录已确认的问题，另有',
    'report.summary.excludedSuffix': '条待确认或已判定为误报的条目未纳入。',
    'report.section.overview': '二、风险总览',
    'report.overview.none': '本次范围内未发现需要报告的安全问题。',
    'report.overview.distribution': '严重程度分布',
    'report.overview.disposition': '处置情况',
    'report.overview.verified': '已通过验证脚本确认',
    'report.overview.fixed': '已修复',
    'report.overview.pending': '待处置',

    // 漏洞详情
    'report.section.entries': '三、漏洞详情',
    'report.entries.none': '无。',
    'report.entries.intro': '以下 {n} 条按严重程度从高到低排列。',
    'report.entries.withRemediation': '每条附修复建议。',
    'report.entries.withoutRemediation': '本报告未包含修复建议，可在系统中查看。',
    'report.entry.type': '类型',
    'report.entry.target': '目标',
    'report.entry.verification': '验证程度',
    'report.entry.status': '处置状态',
    'report.entry.discoveredAt': '发现时间',
    'report.entry.description': '问题描述',
    'report.entry.affected': '受影响位置',
    'report.entry.reasoning': '判断依据',
    'report.entry.evidence': '证据',
    'report.entry.poc': '复现方式',
    'report.entry.remediation': '修复建议',
    'report.entry.dispositions': '处置记录',

    // 附录
    'report.appendix.method': '附录 A　检测范围与方法',
    'report.appendix.targets': '覆盖目标',
    'report.appendix.span': '发现时间跨度',
    'report.appendix.spanTo': '至',
    'report.appendix.skills': '启用的检测能力',
    'report.appendix.scans': '关联的扫描任务',
    'report.appendix.incomplete': '未完成',
    'report.coverage.completed': '完整',
    'report.coverage.aborted': '不完整（已终止）',
    'report.coverage.running': '不完整（进行中）',
    'report.coverage.failed': '未产出结果',
    'report.coverage.unknown': '未知',
    'report.appendix.abortedPrefix': '其中',
    'report.appendix.abortedSuffix':
      '次扫描被提前终止，对应目标的检测覆盖不完整，相关结论仅代表已执行范围内的结果。',
    'report.appendix.boundary':
      '本报告覆盖上述目标在指定范围内的检测结果。检测以自动化的黑盒与灰盒方式为主，未覆盖的目标、未启用的检测能力以及业务逻辑层面的复杂组合场景不在此次范围内。',
    'report.appendix.severity': '附录 B　严重程度说明',
    'report.appendix.meaning': '判定含义',
    'report.appendix.severityNote':
      '严重程度描述问题被利用的难易与影响面，不等同于业务优先级。实际处置顺序建议结合资产重要性与业务影响一并判断。',
    'report.appendix.content': '附录 C　内容范围',
    'report.appendix.remediation': '修复建议',
    'report.appendix.poc': '验证脚本（PoC）',
    'report.appendix.reasoning': '判断依据（智能体推理过程）',
    'report.appendix.filter': '条目筛选',
    'report.appendix.confirmedOnly': '仅已确认的问题',
    'report.appendix.allStatuses': '全部状态的问题',
    'report.appendix.contentNote':
      '未包含的内容可在系统内的漏洞发现页面查看，或重新生成包含这些内容的报告。报告内容在生成时固定，不随漏洞处置状态的变化而改变。',
    'report.listSeparator': '、',

    // 题解弹窗
    'report.writeup.title': '题解',
    'report.writeup.copied': '已复制',
    'report.writeup.copy': '复制全文',
    'report.writeup.download': '下载 .md',
    'report.writeup.close': '关闭',
    'report.writeup.loading': '正在读取题解…',
    'report.writeup.errorTitle': '这次没有可看的题解',
    'report.writeup.untitled': '未命名题目',
  },
  'zh-TW': {},
  en: {
    // Page frame
    'report.view.aria': 'Report view',
    'report.view.compose': 'Create report',
    'report.view.history': 'Report history',
    'report.view.historyCount': 'Report history ({n})',
    'report.title.label': 'Report title',
    'report.format.label': 'Export format',
    'report.generate.action': 'Generate and export',
    'report.generate.loading': 'Generating…',
    'report.generate.pdfHint':
      'Your browser print dialog opens; choose “Save as PDF”. The exported version matches the preview on the right.',
    'report.preview.updating': 'Updating…',
    'report.loadingContent': 'Loading report content',

    // Errors and notices
    'report.error.scopeLoad': 'Failed to load data scope',
    'report.error.previewLoad': 'Failed to load report content',
    'report.error.previewGenerate': 'Failed to generate report preview',
    'report.error.historyLoad': 'Failed to load report history',
    'report.error.pendingPrint':
      'The report was generated, but its preview could not be loaded. Download it again from the history list.',
    'report.error.emptySelection':
      'No entries can be exported with the current scope and options. Adjust them and try again.',
    'report.notice.generatedDownloaded': 'Generated and downloaded {filename}',
    'report.notice.downloaded': 'Downloaded {filename}',
    'report.notice.printOpened':
      'Print dialog opened — choose “Save as PDF” at the destination to export.',
    'report.notice.deleted': 'Deleted “{title}”',

    // Default titles
    'report.title.default': 'Security Assessment Report',
    'report.title.scan': '{name} Security Assessment Report',
    'report.title.range': '{from} to {to} Security Assessment Report',
    'report.title.findings': 'Security Report — {n} Selected Findings',

    // Scope descriptions
    'report.scope.scan': 'Scan “{name}”',
    'report.scope.findings': '{n} hand-picked findings',
    'report.scope.target': 'Target {target}',
    'report.scope.range': '{from} – {to}',

    // Empty states
    'report.empty.noScans.title': 'No completed scans to report on yet',
    'report.empty.noScans.body':
      'Reports come from completed scans. Once a scan finishes, you can turn it into a deliverable report here.',
    'report.history.empty.title': 'No reports generated yet',
    'report.history.empty.body':
      'Pick a scope and content under “Create report” to produce your first one. Generated reports keep the findings and settings from that moment and do not change as finding statuses change.',
    'report.emptyPreview.select': 'Select a report from the list below',
    'report.emptyPreview.noContent': 'Nothing to preview in the current scope',
    'report.emptyPreview.selectHint':
      'Once selected, its content as generated will appear here.',
    'report.emptyPreview.noContentHint': 'Try another data scope, or loosen the filters.',

    // Preview caption
    'report.caption.history': 'Report history preview · content is frozen at generation time',
    'report.caption.compose':
      'Draft preview · matches the exported PDF / Markdown; the report ID is assigned on generation',

    // Meta card
    'report.meta.title': 'Generation settings',
    'report.meta.desc':
      'This report froze its entries and options when generated; that will not change. Re-downloading returns that same report.',
    'report.meta.scope': 'Coverage',
    'report.meta.count': 'Entries',
    'report.meta.findingCount': '{n} findings',
    'report.meta.excluded': 'Excluded',
    'report.meta.excludedValue': '{n} (per options at the time)',
    'report.meta.format': 'Export format',
    'report.meta.remediation': 'Remediation',
    'report.meta.poc': 'PoC scripts',
    'report.meta.reasoning': 'Rationale',
    'report.meta.filter': 'Entry filter',
    'report.meta.included': 'Included',
    'report.meta.notIncluded': 'Not included',
    'report.meta.confirmedOnly': 'Confirmed only',
    'report.meta.allStatuses': 'All statuses',
    'report.meta.redownload': 'Download again',

    // Scope picker
    'report.scope.heading': 'Data scope',
    'report.scope.typeAria': 'Data scope type',
    'report.scopeKind.scan': 'A scan',
    'report.scopeKind.target': 'A target',
    'report.scopeKind.range': 'Time range',
    'report.scopeKind.findings': 'Hand-picked findings',
    'report.findings.receivedPrefix': 'Received',
    'report.findings.receivedSuffix': 'hand-picked findings.',
    'report.findings.receivedHint':
      'To change the selection, pick findings under “Findings” and click “Create report” again.',
    'report.scope.selectScan': 'Select scan',
    'report.scope.selectTarget': 'Select target',
    'report.scope.scanOption': '{name} ({count} findings)',
    'report.scope.targetOption': '{name} ({count} findings)',
    'report.scope.noScans': 'No scan results available',
    'report.scope.noTargets': 'No targets available',
    'report.scope.timeRange': 'Time range',
    'report.scope.presetAria': 'Time range presets',
    'report.preset.7d': 'Last 7 days',
    'report.preset.30d': 'Last 30 days',
    'report.preset.all': 'All time',
    'report.scope.from': 'Start date',
    'report.scope.to': 'End date',

    // Content options
    'report.content.heading': 'Content',
    'report.content.remediation': 'Remediation',
    'report.content.remediationHint': 'Add actionable remediation guidance to every finding',
    'report.content.remediationEmpty': 'No remediation guidance in this scope',
    'report.content.poc': 'PoC scripts',
    'report.content.pocHint': 'Include reproducible scripts and raw requests',
    'report.content.pocEmpty': 'No reproducible PoC scripts in this scope',
    'report.content.reasoning': 'Rationale',
    'report.content.reasoningHint': 'How the agent concluded the issue exists',
    'report.content.reasoningEmpty': 'No rationale available in this scope',
    'report.content.confirmed': 'Export confirmed issues only',
    'report.content.confirmedHint': 'Exclude pending and false-positive findings',
    'report.content.confirmedEmpty': 'No pending or false-positive findings in this scope',
    'report.content.noEntries': 'No exportable entries in this scope',

    // Hit preview
    'report.preview.containsPrefix': 'The report will include',
    'report.preview.findingSuffix': 'findings',
    'report.preview.excluded':
      'Excluded {excluded} by “confirmed only” ({hits} matched)',
    'report.preview.emptyScope':
      'No findings in this scope; the report will state that none were found.',
    'report.preview.emptyFiltered':
      'No entries will be included under the current options. Check the settings above.',

    // Include markers
    'report.include.aria': 'Included',
    'report.exclude.aria': 'Not included',

    // History list
    'report.history.table.report': 'Report',
    'report.history.table.findings': 'Findings',
    'report.history.table.created': 'Created',
    'report.history.table.actions': 'Actions',
    'report.history.openPreviewTitle': 'Open preview of “{title}”',
    'report.history.zeroExcluded': '0 findings ({n} excluded)',
    'report.history.none': 'No findings',
    'report.history.excluded': '{n} excluded',
    'report.history.generating': 'Generating…',
    'report.history.download': 'Download',
    'report.history.open': 'Open',
    'report.history.confirmDelete': 'Confirm delete',
    'report.history.cancel': 'Cancel',
    'report.history.deleteAria': 'Delete “{title}”',

    // Report sheet
    'report.sheet.previewAria': '{title} preview',
    'report.sheet.footerDraft': 'Suvex · Draft preview · Prepared {date}',
    'report.sheet.footerFinal':
      'Generated automatically by Suvex · Report ID {code} · Generated {date}',
    'report.sheet.reportCode': 'Report ID',
    'report.sheet.assignedOnGenerate': 'Assigned on generation',
    'report.sheet.scope': 'Coverage',
    'report.sheet.generatedAt': 'Generated',
    'report.sheet.format': 'Export format',
    'report.sheet.totalFindings': 'Total findings',
    'report.sheet.noFindings': 'No findings',
    'report.sheet.count': '{n} findings',
    'report.sheet.targets': 'Targets covered',
    'report.sheet.targetCount': '{n}',
    'report.sheet.severity': 'Severity',
    'report.sheet.distribution': 'Distribution',
    'report.sheet.countLabel': 'Count',
    'report.sheet.share': 'Share',
    'report.sheet.total': 'Total',

    // Summary / overview
    'report.section.summary': '1. Summary',
    'report.summary.urgentPrefix': 'Of these,',
    'report.summary.urgentSuffix':
      'are critical or high severity and should be remediated before the next iteration.',
    'report.summary.verifiedPrefix': 'This report confirms',
    'report.summary.verifiedSuffix':
      'findings with reproducible verification scripts. All evidence and reproduction steps come from actual execution, with no speculative descriptions.',
    'report.summary.excludedPrefix':
      'This report includes only confirmed issues;',
    'report.summary.excludedSuffix':
      'findings pending confirmation or marked false positive were excluded.',
    'report.section.overview': '2. Risk overview',
    'report.overview.none': 'No reportable security issues were found in this scope.',
    'report.overview.distribution': 'Severity distribution',
    'report.overview.disposition': 'Disposition',
    'report.overview.verified': 'Confirmed by verification scripts',
    'report.overview.fixed': 'Fixed',
    'report.overview.pending': 'Outstanding',

    // Finding details
    'report.section.entries': '3. Finding details',
    'report.entries.none': 'None.',
    'report.entries.intro': 'The following {n} findings are ordered by severity, highest first.',
    'report.entries.withRemediation': 'Remediation is included for each.',
    'report.entries.withoutRemediation':
      'This report does not include remediation; see the system for details.',
    'report.entry.type': 'Type',
    'report.entry.target': 'Target',
    'report.entry.verification': 'Verification',
    'report.entry.status': 'Status',
    'report.entry.discoveredAt': 'Discovered',
    'report.entry.description': 'Description',
    'report.entry.affected': 'Affected locations',
    'report.entry.reasoning': 'Rationale',
    'report.entry.evidence': 'Evidence',
    'report.entry.poc': 'Reproduction',
    'report.entry.remediation': 'Remediation',
    'report.entry.dispositions': 'Handling history',

    // Appendix
    'report.appendix.method': 'Appendix A — Scope and method',
    'report.appendix.targets': 'Targets covered',
    'report.appendix.span': 'Finding time span',
    'report.appendix.spanTo': 'to',
    'report.appendix.skills': 'Detection capabilities enabled',
    'report.appendix.scans': 'Related scans',
    'report.appendix.incomplete': 'Incomplete',
    'report.coverage.completed': 'Complete',
    'report.coverage.aborted': 'Incomplete (aborted)',
    'report.coverage.running': 'Incomplete (running)',
    'report.coverage.failed': 'No results',
    'report.coverage.unknown': 'Unknown',
    'report.appendix.abortedPrefix': 'Of these,',
    'report.appendix.abortedSuffix':
      'scan(s) were terminated early, so coverage of the affected targets is incomplete; conclusions apply only to the executed scope.',
    'report.appendix.boundary':
      'This report covers the detection results for the targets above within the specified scope. Testing relied mainly on automated black-box and grey-box methods; untested targets, disabled detection capabilities, and complex business-logic combinations were outside this scope.',
    'report.appendix.severity': 'Appendix B — Severity definitions',
    'report.appendix.meaning': 'Meaning',
    'report.appendix.severityNote':
      'Severity describes how easily an issue can be exploited and its impact, not business priority. Actual remediation order should also consider asset criticality and business impact.',
    'report.appendix.content': 'Appendix C — Content scope',
    'report.appendix.remediation': 'Remediation',
    'report.appendix.poc': 'PoC scripts',
    'report.appendix.reasoning': 'Rationale (agent reasoning)',
    'report.appendix.filter': 'Entry filter',
    'report.appendix.confirmedOnly': 'Confirmed issues only',
    'report.appendix.allStatuses': 'Issues in all statuses',
    'report.appendix.contentNote':
      'Excluded content can be viewed on the Findings page, or included by regenerating the report. Report content is frozen at generation time and does not change with finding status updates.',
    'report.listSeparator': ', ',

    // Writeup dialog
    'report.writeup.title': 'Writeup',
    'report.writeup.copied': 'Copied',
    'report.writeup.copy': 'Copy all',
    'report.writeup.download': 'Download .md',
    'report.writeup.close': 'Close',
    'report.writeup.loading': 'Loading writeup…',
    'report.writeup.errorTitle': 'No writeup available this time',
    'report.writeup.untitled': 'Untitled challenge',
  },
  ru: {},
  fr: {},
  ja: {},
  de: {},
}
