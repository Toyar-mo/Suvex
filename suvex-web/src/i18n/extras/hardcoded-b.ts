import type { ExtraLocaleMap } from './index'

/** 扫描页与扫描向导中原本写死的中文文案。 */
export const hardcoded_b: ExtraLocaleMap = {
  'zh-CN': {
    // 目标类型
    'scans2.kind.url': '已部署的应用',
    'scans2.kind.repo': 'Git 仓库',
    'scans2.kind.path': '本地代码路径',
    'scans2.kind.service': 'TCP 服务',
    'scans2.kind.artifact': '题目附件',
    'scans2.kind.url.desc': '对运行中的服务发请求，按实际响应判断。',
    'scans2.kind.repo.desc': '克隆仓库后先读代码，再针对可疑位置做动态验证。',
    'scans2.kind.path.desc': '直接分析本机目录里的代码，不经过网络。',
    'scans2.kind.service.desc':
      '直连裸 TCP 端口收发原始字节（Pwn、网络 Misc、Crypto 预言机）。没有 HTTP 层。',
    'scans2.kind.artifact.desc': '逆向、纯文件 Misc、密码学 —— 考点全在附件里，没有可连的地址。',
    'scans2.kindHint.url.placeholder': 'https://api.example.com',
    'scans2.kindHint.url.example': 'https://api.acme.internal',
    'scans2.kindHint.repo.placeholder': 'https://github.com/org/repo',
    'scans2.kindHint.repo.example': 'https://github.com/acme/backend',
    'scans2.kindHint.path.placeholder': '/srv/app 或 D:\\projects\\acme',
    'scans2.kindHint.path.example': '/srv/app',
    'scans2.kindHint.service.placeholder': '主机:端口，也可直接粘贴平台的连接命令',
    'scans2.kindHint.service.example': '1.2.3.4:9999',
    'scans2.kindHint.artifact.placeholder': '不需要填写',
    'scans2.kindHint.artifact.example': '这类题目只需要上传附件',

    // 技能
    'scans2.skill.web.label': 'Web 应用',
    'scans2.skill.web.desc': '表单、认证、会话、注入类问题。',
    'scans2.skill.api.label': 'API 接口',
    'scans2.skill.api.desc': '按 OpenAPI / GraphQL 描述逐个接口测鉴权与输入处理。',
    'scans2.skill.container.label': '容器配置',
    'scans2.skill.container.desc': 'Dockerfile、compose、K8s 清单里的权限与配置缺陷。',
    'scans2.skill.cloud.label': '云配置',
    'scans2.skill.cloud.desc': 'IaC 模板里的公开桶、过宽 IAM、缺失加密。',
    'scans2.skill.dependencies.label': '依赖漏洞',
    'scans2.skill.dependencies.desc': '比对锁文件里的组件版本与已知漏洞库。',
    'scans2.skill.secrets.label': '密钥泄露',
    'scans2.skill.secrets.desc': '在代码与配置里找硬编码的凭据、令牌、私钥。',
    'scans2.skill.protocol.label': '协议分析',
    'scans2.skill.protocol.desc':
      '裸 TCP 端口的协议逆向与二进制交互（Pwn、网络 Misc、Crypto 预言机）。',
    'scans2.skill.needsCredentials': '需要凭据',

    // 成功判据
    'scans2.goal.findVulns.label': '例行安全评估',
    'scans2.goal.findVulns.hint': '尽可能找出真实存在的问题，按严重度定级并给修复建议。',
    'scans2.goal.captureFlag.label': 'CTF 夺旗（拿到 flag）',
    'scans2.goal.captureFlag.hint': '优先深挖能通向 flag 的路径，而不是广撒网枚举低危问题。',
    'scans2.goal.verifyFix.label': '复测（确认已知问题已修）',
    'scans2.goal.verifyFix.hint': '逐一验证已知问题是否已修复，新发现单独列在最后。',

    // 向导步骤
    'scans2.step.target': '目标',
    'scans2.step.skills': '检测范围',
    'scans2.step.provider': '模型',
    'scans2.step.brief': '任务背景',
    'scans2.step.confirm': '确认',

    // 步骤 1 · 目标
    'scans2.target.whatLabel': '要测什么',
    'scans2.target.whatHelp': 'Suvex 可以直接测已部署的应用，也可以读代码库做白盒分析。',
    'scans2.target.address': '目标地址',
    'scans2.target.artifactBody':
      '请到下一步「任务背景」上传题目文件（zip / ELF / 流量包都行）。附件会被解压成一个工作目录，直接当作代码目录来分析 —— 全程没有网络请求要发。',
    'scans2.target.uploaded': '已上传 {n} 个附件：{names}',
    'scans2.target.noAttachment': '还没有附件。这类题目必须至少上传一个，否则不能提交。',
    'scans2.name.label': '扫描名称',
    'scans2.name.artifactHelp': '留空则用题目附件名作为名称。',
    'scans2.name.help': '留空则用目标地址作为名称。',
    'scans2.name.autoFromAttachment': '上传附件后自动命名',
    'scans2.name.optional': '可选',

    // 步骤 2 · 技能范围
    'scans2.skills.scope': '检测范围',
    'scans2.skills.scopeHelp': '勾选得越多，扫描越慢、消耗的 token 越多。建议先跑一轮核心项。',
    'scans2.skills.inapplicable': '以下技能不适用于{kind}：',
    'scans2.skills.selectOne': '至少勾选一项，否则扫描没有可用的检测能力',

    // 步骤 3 · 模型
    'scans2.provider.none': '还没有配置任何可用的模型。扫描需要一个能用的 LLM 才能规划步骤和判断结果。',
    'scans2.provider.goSettings': '前往设置配置模型',
    'scans2.provider.label': '使用哪个模型',
    'scans2.provider.help': '决定扫描时的推理质量与成本。',
    'scans2.provider.local': '本地',
    'scans2.provider.configure': '去配置',

    // 步骤 4 · 背景
    'scans2.brief.introBefore': '这一段会',
    'scans2.brief.introAfter':
      '交给智能体作为任务背景。题目描述、考点提示、测试范围、测试账号都可以写在这里 —— 它比任何扫描参数都更能决定结果。',
    'scans2.brief.introOptional': '，但只给一个网址时，智能体只能盲扫。',
    'scans2.brief.label': '任务背景',
    'scans2.brief.placeholder':
      '例如：\n题目：LogVault —— 日志模板引擎的污染数据流（PWN，500 分）\n提示：关注 %hhn 与 varargs 的边界条件；题面里那段"安全备注"是诱饵。\n目标：拿到 flag，格式 flag{...}\n附件见下（二进制 chall）。',
    'scans2.brief.credentialsNote':
      '含口令也可以写：它不会出现在进程列表里（保存在指令文件中，仅本机可读）。',
    'scans2.brief.attachments': '题目附件',
    'scans2.brief.attachmentsRequired': '（这类题目必须上传）',
    'scans2.brief.attachmentsOptional': '（可选）',
    'scans2.brief.remove': '移除 {name}',
    'scans2.brief.uploading': '上传中…',
    'scans2.brief.chooseFile': '选择文件',
    'scans2.brief.uploadBefore': '任意类型（二进制、源码、字典、流量包）。它们会被放进沙箱工作区的',
    'scans2.brief.uploadAfter': '目录，智能体可以直接读。',
    'scans2.brief.requiredNote': ' 这类题目没有目标地址，附件就是被测对象本身。',
    'scans2.brief.missingAttachment':
      '这类题目必须至少上传一个附件 —— 没有附件就没有可分析的东西。请点上面的「选择文件」。',

    // 步骤 5 · 确认
    'scans2.confirm.artifactBefore': '将对上传的题目附件 ',
    'scans2.confirm.artifactAfter': ' 做一次白盒分析，使用 ',
    'scans2.confirm.targetBefore': '将对 ',
    'scans2.confirm.targetAfter': ' 发起一次主动扫描，使用 ',
    'scans2.confirm.beforeSkills': '，启用 ',
    'scans2.confirm.capabilities': ' 等{n} 项检测能力。',
    'scans2.confirm.artifactNote':
      '附件会解压到沙箱工作目录里分析，不会向任何地址发包。请确认这些文件你有权查看。',
    'scans2.confirm.networkNote':
      '扫描会向目标发送真实请求，可能触发对方的告警或日志记录。确认这个目标是你有权测试的。',
    'scans2.confirm.kind': '目标类型',
    'scans2.confirm.target': '目标',
    'scans2.confirm.model': '模型',
    'scans2.confirm.authNote':
      '对未授权目标发起扫描在很多司法管辖区是违法行为。这次确认会连同服务端时间与来源一起记录在这条扫描的历史里 —— 它不是一个只在界面上出现的勾。',
    'scans2.common.unselected': '未选择',
    'scans2.common.edit': '修改',
    'scans2.common.required': '必填',
    'scans2.common.listSep': '、',

    // 启动按钮与向导框架
    'scans2.launch.start': '启动扫描',
    'scans2.launch.starting': '正在启动…',
    'scans2.launch.needAttachment': '这类题目必须上传题目附件，请回到「任务背景」一步上传',
    'scans2.launch.invalidTarget': '目标地址无效，请返回第一步修正',
    'scans2.launch.noSkills': '未选择任何检测能力',
    'scans2.launch.noProvider': '未选择可用的模型',
    'scans2.launch.needAuthorization': '请先确认你有权测试该目标',
    'scans2.wizard.title': '新建扫描',
    'scans2.wizard.close': '关闭',
    'scans2.wizard.cancel': '取消',
    'scans2.wizard.back': '上一步',
    'scans2.wizard.next': '下一步',
    'scans2.wizard.progress': '第 {current} 步，共 {total} 步：{step}',
    'scans2.wizard.stepsAria': '新建扫描步骤',

    // 批量操作
    'scans2.bulk.selected': '已选 {n} 项',
    'scans2.bulk.compare': '对比',
    'scans2.bulk.archive': '归档',
    'scans2.bulk.unarchive': '取消归档',
    'scans2.bulk.rerun': '重新扫描',
    'scans2.bulk.export': '导出',
    'scans2.bulk.delete': '删除',
    'scans2.bulk.restore': '恢复',
    'scans2.bulk.purge': '永久删除',
    'scans2.bulk.clear': '取消选择',

    // 对比弹窗
    'scans2.compare.title': '扫描对比',
    'scans2.compare.target': '目标 {target}',
    'scans2.compare.close': '关闭',
    'scans2.compare.failed': '对比失败：{error}',
    'scans2.compare.loading': '正在对比…',
    'scans2.compare.diffTargetsPrefix': '这两次扫描的目标不同（',
    'scans2.compare.diffTargetsMid': ' 与 ',
    'scans2.compare.diffTargetsSuffix':
      '）—— 下面的「新增 / 消失」是按漏洞标题比出来的，跨目标时通常不能当作"问题变多 / 已修好"的证据。',
    'scans2.compare.added': '新增',
    'scans2.compare.addedHint': '这次才出现 —— 可能是新引入的问题',
    'scans2.compare.removed': '消失',
    'scans2.compare.removedHint': '上次有、这次没有 —— 可能是已经修好了',
    'scans2.compare.common': '两次都有（{n}）',

    // 线索
    'scans2.hint.sent': '线索已送出，下一轮生效',
    'scans2.hint.failed': '发送失败',
    'scans2.hint.noneRunning': '没有运行中的扫描',
    'scans2.hint.placeholder': '例如：登录接口在 /api/v2，不要在 /index.php 上继续',
    'scans2.hint.sending': '发送中…',
    'scans2.hint.send': '发送线索',

    // 扫描列表
    'scans2.list.fail.userStopped': '用户终止',
    'scans2.list.fail.systemStopped': '系统中断',
    'scans2.list.fail.stopped': '已终止',
    'scans2.list.fail.infra': '基础设施中断',
    'scans2.list.terminated.user': '用户本人',
    'scans2.list.terminated.system': '系统',
    'scans2.list.terminated.unknown': '无法判定',
    'scans2.list.status.pendingConfirmation': '候选待确认',
    'scans2.list.status.unsolved': '未解出',
    'scans2.list.select': '选择 {target}',
    'scans2.list.viewActivity': '查看 {name} 的智能体活动',
    'scans2.list.terminatedByTitle': '谁终止了这次扫描',
    'scans2.list.terminatedBy': '终止方：{by}',
    'scans2.list.stalledHint':
      '服务端很久没有收到这次扫描的新活动事件了。常见原因是某个命令迟迟不返回，或目标协议与实际不符。如果它其实还在跑（只是安静），有新事件后这个标记会自动消失。',
    'scans2.list.stalled': '疑似卡死 · {silent}',
    'scans2.list.noEntriesHint.ir':
      '这次复盘没有产出漏洞条目。它的产物是入侵时间线与处置建议，不以漏洞条数衡量。',
    'scans2.list.noEntriesHint.flag':
      '这次扫描没有产出漏洞条目。夺旗任务的成败看是否拿到 flag，不以漏洞条数衡量。',
    'scans2.list.noEntriesHint.verify':
      '这次复测没有产出新的漏洞条目。复测的结论看已知问题是否已修复，不以漏洞条数衡量。',
    'scans2.list.irConclusion': '结论见复盘报告',
    'scans2.list.severityCount': '{severity} {n} 条',
    'scans2.list.incompleteTitle':
      '扫描被中途终止，这个数字只是已跑完部分的结果，不代表全部',
    'scans2.list.incomplete': '· 不完整',
    'scans2.list.running': '进行中',
    'scans2.list.viewScanActivity': '查看该扫描的智能体活动',
    'scans2.list.writeupTitle': '查看这次夺旗的题解（Markdown）',
    'scans2.list.writeup': '题解',
    'scans2.list.rerun': '重跑（新建一条记录）',
    'scans2.list.resumeTitle': '接回上次的会话继续跑 —— 不重复已经做完的推理',
    'scans2.list.resume': '续跑（接着上次的会话）',
    'scans2.list.abort': '终止扫描',

    // 「我的视图」
    'scans2.views.activeTitle': '当前视图：{name}（{desc}）',
    'scans2.views.saveHint': '把当前筛选条件存下来，下次一键套用',
    'scans2.views.saveCurrent': '保存当前筛选',
    'scans2.views.namePlaceholder': '例如：内网高危未修',
    'scans2.views.filterFirst': '先设置筛选条件',
    'scans2.views.save': '保存',
    'scans2.views.limit': '最多 {n} 个视图，先删掉不用的。',
    'scans2.views.loadFailed': '加载失败',
    'scans2.views.retry': '重试',
    'scans2.views.empty': '还没有保存的视图。设好筛选条件后在上面起个名字即可。',
    'scans2.views.remove': '删除视图 {name}',
    'scans2.views.removeTitle': '删除这个视图（不影响扫描数据）',

    // 页面
    'scans2.page.toast.abortFailed': '终止失败：{error}',
    'scans2.page.toast.rerunFailed': '重新扫描失败：{error}',
    'scans2.page.toast.resuming': '正在续跑：{name}…',
    'scans2.page.toast.resumed': '已续跑：{name}',
    'scans2.page.toast.resumeFailed': '续跑失败：{error}',
    'scans2.page.toast.undo': '撤销',
    'scans2.page.toast.undoFailed': '撤销失败：{error}',
    'scans2.page.bulk.archived': '已归档 {n} 项',
    'scans2.page.bulk.archiveFailed': '归档失败',
    'scans2.page.bulk.unarchived': '已取消归档 {n} 项',
    'scans2.page.bulk.unarchiveFailed': '取消归档失败',
    'scans2.page.bulk.trashed': '已移入回收站 {n} 项',
    'scans2.page.bulk.deleteFailed': '删除失败',
    'scans2.page.bulk.restored': '已恢复 {n} 项',
    'scans2.page.bulk.restoreFailed': '恢复失败',
    'scans2.page.bulk.purged': '已永久删除 {n} 项',
    'scans2.page.bulk.purgePartial': '已删除 {ok} 项，{failed} 项被拒绝：{error}',
    'scans2.page.bulk.rerunCreated': '已重新发起 {n} 项',
    'scans2.page.bulk.rerunFailedCount': '{n} 项失败',
    'scans2.page.bulk.exported': '已导出 {n} 项为 CSV',
    'scans2.page.stall.title': '{n} 个运行中扫描在重复同一动作（可能是原地打转）：',
    'scans2.page.stall.repeats': '连续 {n} 次 · ',
    'scans2.page.filterAria': '按状态筛选',
    'scans2.page.onlyScan': '只看扫描 {id}',
    'scans2.page.only': '只看 ',
    'scans2.page.sort.desc': '当前降序，点击切换升序',
    'scans2.page.sort.asc': '当前升序，点击切换降序',
    'scans2.page.confirm.deleteTitle': '删除 {n} 次扫描？',
    'scans2.page.confirm.deleteBody':
      '它们会被移入回收站，可以随时恢复；只有「永久删除」才会真正抹掉数据。保留天数在设置里可调。',
    'scans2.page.confirm.trash': '移入回收站',
    'scans2.page.confirm.purgeTitle': '永久删除 {n} 次扫描？',
    'scans2.page.confirm.purgeBody':
      '这一步不可撤销。相关的漏洞、事件与智能体记录会一并删除；若某份报告引用了它们，该条会被拒绝并告诉你具体是哪份报告。',
    'scans2.page.confirm.purge': '永久删除',
    'scans2.page.created.title': '扫描已加入队列',
    'scans2.page.created.viewActivity': '查看活动',
    'scans2.page.created.dismiss': '关闭提示',
    'scans2.page.error.loadList': '扫描列表加载失败',
    'scans2.page.error.load': '加载失败',
    'scans2.page.empty.archived.title': '没有已归档的扫描',
    'scans2.page.empty.archived.body':
      '归档把不再关注的记录从默认列表里移走 —— 数据不会删，随时可以取消归档。可在「设置 → 扫描保留规则」里让失败与已终止的扫描到期自动归档。',
    'scans2.page.empty.deleted.title': '回收站是空的',
    'scans2.page.empty.deleted.body':
      '从列表里删除的扫描会先到这里，保留期满后才会被真正清除 —— 保留天数在「设置 → 扫描保留规则」里可调。期间随时可以恢复。',
    'scans2.list.status.pendingTitle': 'CTF2 题目已有候选 flag，等待确认；底层扫描记录状态为失败',
    'scans2.list.status.unsolvedTitle': 'CTF2 题目未解出；底层扫描记录状态为失败',
  },

  'zh-TW': {
    'scans2.list.status.pendingTitle': 'CTF2 題目已有候選 flag，等待確認；底層掃描記錄狀態為失敗',
    'scans2.list.status.unsolvedTitle': 'CTF2 題目未解出；底層掃描記錄狀態為失敗',
  },

  en: {
    // Target kinds
    'scans2.kind.url': 'Deployed app',
    'scans2.kind.repo': 'Git repository',
    'scans2.kind.path': 'Local code path',
    'scans2.kind.service': 'TCP service',
    'scans2.kind.artifact': 'Challenge artifact',
    'scans2.kind.url.desc': 'Sends requests to a running service and judges by the actual responses.',
    'scans2.kind.repo.desc':
      'Clones the repository, reads the code first, then dynamically verifies suspicious spots.',
    'scans2.kind.path.desc': 'Analyzes code in a local directory directly, without network access.',
    'scans2.kind.service.desc':
      'Talks to raw TCP ports directly (Pwn, network Misc, crypto oracles). No HTTP layer.',
    'scans2.kind.artifact.desc':
      'Reverse engineering, file-only Misc, cryptography — everything is in the attachment; there is no address to connect to.',
    'scans2.kindHint.url.placeholder': 'https://api.example.com',
    'scans2.kindHint.url.example': 'https://api.acme.internal',
    'scans2.kindHint.repo.placeholder': 'https://github.com/org/repo',
    'scans2.kindHint.repo.example': 'https://github.com/acme/backend',
    'scans2.kindHint.path.placeholder': '/srv/app or D:\\projects\\acme',
    'scans2.kindHint.path.example': '/srv/app',
    'scans2.kindHint.service.placeholder': 'host:port, or paste the platform connection command',
    'scans2.kindHint.service.example': '1.2.3.4:9999',
    'scans2.kindHint.artifact.placeholder': 'Not required',
    'scans2.kindHint.artifact.example': 'Only the attachment is needed for this challenge type',

    // Skills
    'scans2.skill.web.label': 'Web app',
    'scans2.skill.web.desc': 'Forms, authentication, sessions, and injection issues.',
    'scans2.skill.api.label': 'API endpoints',
    'scans2.skill.api.desc':
      'Tests authentication and input handling endpoint by endpoint from OpenAPI / GraphQL descriptions.',
    'scans2.skill.container.label': 'Container config',
    'scans2.skill.container.desc':
      'Permission and configuration flaws in Dockerfiles, compose, and K8s manifests.',
    'scans2.skill.cloud.label': 'Cloud config',
    'scans2.skill.cloud.desc':
      'Public buckets, overly broad IAM, and missing encryption in IaC templates.',
    'scans2.skill.dependencies.label': 'Dependency vulnerabilities',
    'scans2.skill.dependencies.desc':
      'Compares component versions in lockfiles against known vulnerability databases.',
    'scans2.skill.secrets.label': 'Secret leaks',
    'scans2.skill.secrets.desc':
      'Finds hardcoded credentials, tokens, and private keys in code and config.',
    'scans2.skill.protocol.label': 'Protocol analysis',
    'scans2.skill.protocol.desc':
      'Protocol reversing and binary interaction over raw TCP ports (Pwn, network Misc, crypto oracles).',
    'scans2.skill.needsCredentials': 'Needs credentials',

    // Goals
    'scans2.goal.findVulns.label': 'Routine security assessment',
    'scans2.goal.findVulns.hint':
      'Find real issues as thoroughly as possible, rate their severity, and suggest fixes.',
    'scans2.goal.captureFlag.label': 'CTF capture the flag',
    'scans2.goal.captureFlag.hint':
      'Prioritize paths that lead to the flag over broadly enumerating low-severity issues.',
    'scans2.goal.verifyFix.label': 'Retest (confirm known issues fixed)',
    'scans2.goal.verifyFix.hint':
      'Verify each known issue is fixed; list new findings separately at the end.',

    // Wizard steps
    'scans2.step.target': 'Target',
    'scans2.step.skills': 'Scope',
    'scans2.step.provider': 'Model',
    'scans2.step.brief': 'Context',
    'scans2.step.confirm': 'Confirm',

    // Step 1 · target
    'scans2.target.whatLabel': 'What to test',
    'scans2.target.whatHelp':
      'Suvex can test a deployed app directly, or read a codebase for white-box analysis.',
    'scans2.target.address': 'Target address',
    'scans2.target.artifactBody':
      'Upload the challenge files in the next step, Context (zip / ELF / packet capture all work). The attachment is unpacked into a working directory and analyzed as a code directory — no network requests are sent at all.',
    'scans2.target.uploaded': 'Uploaded {n} attachment(s): {names}',
    'scans2.target.noAttachment':
      'No attachments yet. This challenge type requires at least one before you can submit.',
    'scans2.name.label': 'Scan name',
    'scans2.name.artifactHelp': 'Leave blank to use the challenge attachment name.',
    'scans2.name.help': 'Leave blank to use the target address.',
    'scans2.name.autoFromAttachment': 'Named automatically after upload',
    'scans2.name.optional': 'Optional',

    // Step 2 · skills
    'scans2.skills.scope': 'Scope',
    'scans2.skills.scopeHelp':
      'The more you select, the slower the scan and the more tokens it consumes. Start with a core set.',
    'scans2.skills.inapplicable': 'These skills do not apply to {kind}:',
    'scans2.skills.selectOne': 'Select at least one, otherwise the scan has no detection capabilities',

    // Step 3 · model
    'scans2.provider.none':
      'No usable model is configured yet. A scan needs a working LLM to plan steps and judge results.',
    'scans2.provider.goSettings': 'Go to settings to configure a model',
    'scans2.provider.label': 'Which model to use',
    'scans2.provider.help': 'Determines reasoning quality and cost during the scan.',
    'scans2.provider.local': 'Local',
    'scans2.provider.configure': 'Configure',

    // Step 4 · context
    'scans2.brief.introBefore': 'This section is passed ',
    'scans2.brief.introAfter':
      'to the agent as task context. Put the challenge description, hints, test scope, and test accounts here — it shapes the result more than any scan parameter.',
    'scans2.brief.introOptional': '. If you only give a URL, the agent can only scan blindly.',
    'scans2.brief.label': 'Task context',
    'scans2.brief.placeholder':
      'Example:\nChallenge: LogVault — tainted data flow in a log template engine (PWN, 500 pts)\nHint: watch the %hhn and varargs boundary conditions; the "security note" in the prompt is a decoy.\nGoal: get the flag, format flag{...}\nAttachment below (binary chall).',
    'scans2.brief.credentialsNote':
      'Passwords are fine: they never appear in the process list (stored in an instruction file readable only on this machine).',
    'scans2.brief.attachments': 'Challenge attachments',
    'scans2.brief.attachmentsRequired': ' (required for this challenge type)',
    'scans2.brief.attachmentsOptional': ' (optional)',
    'scans2.brief.remove': 'Remove {name}',
    'scans2.brief.uploading': 'Uploading…',
    'scans2.brief.chooseFile': 'Choose files',
    'scans2.brief.uploadBefore':
      'Any type (binary, source, wordlist, packet capture). They are placed in the sandbox workspace',
    'scans2.brief.uploadAfter': 'directory, where the agent can read them directly.',
    'scans2.brief.requiredNote':
      ' This challenge type has no target address; the attachment is the subject under test.',
    'scans2.brief.missingAttachment':
      'This challenge type requires at least one attachment — without one there is nothing to analyze. Click "Choose files" above.',

    // Step 5 · confirm
    'scans2.confirm.artifactBefore': 'Will run a white-box analysis on the uploaded attachment ',
    'scans2.confirm.artifactAfter': ' using ',
    'scans2.confirm.targetBefore': 'Will start an active scan against ',
    'scans2.confirm.targetAfter': ' using ',
    'scans2.confirm.beforeSkills': ', enabling ',
    'scans2.confirm.capabilities': ' ({n} detection capabilities).',
    'scans2.confirm.artifactNote':
      'The attachment is unpacked into a sandbox working directory and analyzed; no packets are sent anywhere. Confirm that you are allowed to inspect these files.',
    'scans2.confirm.networkNote':
      'The scan sends real requests to the target and may trigger alerts or log entries on their side. Confirm that you are authorized to test this target.',
    'scans2.confirm.kind': 'Target type',
    'scans2.confirm.target': 'Target',
    'scans2.confirm.model': 'Model',
    'scans2.confirm.authNote':
      'Scanning an unauthorized target is illegal in many jurisdictions. This confirmation is recorded in the scan history together with the server time and source — it is not just a checkbox in the UI.',
    'scans2.common.unselected': 'Not selected',
    'scans2.common.edit': 'Edit',
    'scans2.common.required': 'Required',
    'scans2.common.listSep': ', ',

    // Launch and wizard chrome
    'scans2.launch.start': 'Start scan',
    'scans2.launch.starting': 'Starting…',
    'scans2.launch.needAttachment':
      'This challenge type requires a challenge attachment; go back to the Context step to upload one',
    'scans2.launch.invalidTarget': 'The target address is invalid; go back to the first step to fix it',
    'scans2.launch.noSkills': 'No detection capabilities selected',
    'scans2.launch.noProvider': 'No usable model selected',
    'scans2.launch.needAuthorization': 'Confirm that you are authorized to test this target',
    'scans2.wizard.title': 'New scan',
    'scans2.wizard.close': 'Close',
    'scans2.wizard.cancel': 'Cancel',
    'scans2.wizard.back': 'Back',
    'scans2.wizard.next': 'Next',
    'scans2.wizard.progress': 'Step {current} of {total}: {step}',
    'scans2.wizard.stepsAria': 'New scan steps',

    // Bulk actions
    'scans2.bulk.selected': '{n} selected',
    'scans2.bulk.compare': 'Compare',
    'scans2.bulk.archive': 'Archive',
    'scans2.bulk.unarchive': 'Unarchive',
    'scans2.bulk.rerun': 'Rescan',
    'scans2.bulk.export': 'Export',
    'scans2.bulk.delete': 'Delete',
    'scans2.bulk.restore': 'Restore',
    'scans2.bulk.purge': 'Delete permanently',
    'scans2.bulk.clear': 'Clear selection',

    // Compare dialog
    'scans2.compare.title': 'Scan comparison',
    'scans2.compare.target': 'Target {target}',
    'scans2.compare.close': 'Close',
    'scans2.compare.failed': 'Comparison failed: {error}',
    'scans2.compare.loading': 'Comparing…',
    'scans2.compare.diffTargetsPrefix': 'These scans have different targets (',
    'scans2.compare.diffTargetsMid': ' vs ',
    'scans2.compare.diffTargetsSuffix':
      ') — the "added / removed" lists are matched by finding title, so across targets they usually are not evidence that issues appeared or were fixed.',
    'scans2.compare.added': 'Added',
    'scans2.compare.addedHint': 'Appeared this time — possibly a newly introduced issue',
    'scans2.compare.removed': 'Removed',
    'scans2.compare.removedHint': 'Present last time, absent now — possibly fixed',
    'scans2.compare.common': 'In both ({n})',

    // Hints
    'scans2.hint.sent': 'Hint sent; it takes effect next round',
    'scans2.hint.failed': 'Failed to send',
    'scans2.hint.noneRunning': 'No running scans',
    'scans2.hint.placeholder':
      'e.g. the login endpoint is at /api/v2; stop probing /index.php',
    'scans2.hint.sending': 'Sending…',
    'scans2.hint.send': 'Send hint',

    // Scan list
    'scans2.list.fail.userStopped': 'Stopped by user',
    'scans2.list.fail.systemStopped': 'System interruption',
    'scans2.list.fail.stopped': 'Stopped',
    'scans2.list.fail.infra': 'Infrastructure failure',
    'scans2.list.terminated.user': 'The user',
    'scans2.list.terminated.system': 'The system',
    'scans2.list.terminated.unknown': 'Unknown',
    'scans2.list.status.pendingConfirmation': 'Candidate pending confirmation',
    'scans2.list.status.unsolved': 'Unsolved',
    'scans2.list.select': 'Select {target}',
    'scans2.list.viewActivity': 'View agent activity for {name}',
    'scans2.list.terminatedByTitle': 'Who stopped this scan',
    'scans2.list.terminatedBy': 'Stopped by: {by}',
    'scans2.list.stalledHint':
      'The server has not received new activity events for this scan for a long time. Common causes are a command that never returns or a target protocol that differs from the actual one. If it is still running (just quiet), this marker disappears once new events arrive.',
    'scans2.list.stalled': 'Possibly stalled · {silent}',
    'scans2.list.noEntriesHint.ir':
      'This review produced no finding entries. Its outputs are the intrusion timeline and remediation advice, not a finding count.',
    'scans2.list.noEntriesHint.flag':
      'This scan produced no finding entries. A capture-the-flag run is judged by whether the flag was obtained, not by finding count.',
    'scans2.list.noEntriesHint.verify':
      'This retest produced no new finding entries. A retest is judged by whether the known issues are fixed, not by finding count.',
    'scans2.list.irConclusion': 'See the review report for conclusions',
    'scans2.list.severityCount': '{severity}: {n}',
    'scans2.list.incompleteTitle':
      'The scan was stopped midway; this number covers only the completed part and is not the full result',
    'scans2.list.incomplete': '· Incomplete',
    'scans2.list.running': 'Running',
    'scans2.list.viewScanActivity': 'View agent activity for this scan',
    'scans2.list.writeupTitle': 'View the writeup for this capture-the-flag run (Markdown)',
    'scans2.list.writeup': 'Writeup',
    'scans2.list.rerun': 'Run again (creates a new record)',
    'scans2.list.resumeTitle': 'Resume the previous session — no repeated reasoning',
    'scans2.list.resume': 'Resume (continue the previous session)',
    'scans2.list.abort': 'Stop scan',

    // Saved views
    'scans2.views.activeTitle': 'Active view: {name} ({desc})',
    'scans2.views.saveHint': 'Save the current filters for one-click reuse',
    'scans2.views.saveCurrent': 'Save current filters',
    'scans2.views.namePlaceholder': 'e.g. Internal high-severity unfixed',
    'scans2.views.filterFirst': 'Set filters first',
    'scans2.views.save': 'Save',
    'scans2.views.limit': 'At most {n} views; delete an unused one first.',
    'scans2.views.loadFailed': 'Failed to load',
    'scans2.views.retry': 'Retry',
    'scans2.views.empty': 'No saved views yet. Set your filters above, then give this view a name.',
    'scans2.views.remove': 'Delete view {name}',
    'scans2.views.removeTitle': 'Delete this view (does not affect scan data)',

    // Page
    'scans2.page.toast.abortFailed': 'Failed to stop: {error}',
    'scans2.page.toast.rerunFailed': 'Failed to rescan: {error}',
    'scans2.page.toast.resuming': 'Resuming: {name}…',
    'scans2.page.toast.resumed': 'Resumed: {name}',
    'scans2.page.toast.resumeFailed': 'Failed to resume: {error}',
    'scans2.page.toast.undo': 'Undo',
    'scans2.page.toast.undoFailed': 'Undo failed: {error}',
    'scans2.page.bulk.archived': 'Archived {n} item(s)',
    'scans2.page.bulk.archiveFailed': 'Failed to archive',
    'scans2.page.bulk.unarchived': 'Unarchived {n} item(s)',
    'scans2.page.bulk.unarchiveFailed': 'Failed to unarchive',
    'scans2.page.bulk.trashed': 'Moved {n} item(s) to trash',
    'scans2.page.bulk.deleteFailed': 'Failed to delete',
    'scans2.page.bulk.restored': 'Restored {n} item(s)',
    'scans2.page.bulk.restoreFailed': 'Failed to restore',
    'scans2.page.bulk.purged': 'Permanently deleted {n} item(s)',
    'scans2.page.bulk.purgePartial': 'Deleted {ok} item(s); {failed} rejected: {error}',
    'scans2.page.bulk.rerunCreated': 'Started {n} new scan(s)',
    'scans2.page.bulk.rerunFailedCount': '{n} failed',
    'scans2.page.bulk.exported': 'Exported {n} item(s) as CSV',
    'scans2.page.stall.title':
      '{n} running scan(s) keep repeating the same action (possibly spinning in place):',
    'scans2.page.stall.repeats': '{n} in a row · ',
    'scans2.page.filterAria': 'Filter by status',
    'scans2.page.onlyScan': 'Only scan {id}',
    'scans2.page.only': 'Only ',
    'scans2.page.sort.desc': 'Currently descending; click to switch to ascending',
    'scans2.page.sort.asc': 'Currently ascending; click to switch to descending',
    'scans2.page.confirm.deleteTitle': 'Delete {n} scan(s)?',
    'scans2.page.confirm.deleteBody':
      'They will be moved to trash and can be restored at any time; only "Delete permanently" actually erases data. Retention days are configurable in settings.',
    'scans2.page.confirm.trash': 'Move to trash',
    'scans2.page.confirm.purgeTitle': 'Permanently delete {n} scan(s)?',
    'scans2.page.confirm.purgeBody':
      'This cannot be undone. Related findings, events, and agent records are deleted too; if a report references them, that item is rejected and the report is named in the error.',
    'scans2.page.confirm.purge': 'Delete permanently',
    'scans2.page.created.title': 'Scan added to the queue',
    'scans2.page.created.viewActivity': 'View activity',
    'scans2.page.created.dismiss': 'Dismiss',
    'scans2.page.error.loadList': 'Failed to load scan list',
    'scans2.page.error.load': 'Failed to load',
    'scans2.page.empty.archived.title': 'No archived scans',
    'scans2.page.empty.archived.body':
      'Archiving moves records you no longer follow out of the default list — data is not deleted, and you can unarchive anytime. In Settings → Scan retention you can auto-archive failed and aborted scans when they expire.',
    'scans2.page.empty.deleted.title': 'Trash is empty',
    'scans2.page.empty.deleted.body':
      'Scans deleted from the list land here first and are only erased after the retention period — adjustable in Settings → Scan retention. You can restore them anytime before then.',
    'scans2.list.status.pendingTitle': 'CTF2 challenge has a candidate flag awaiting confirmation; the underlying scan is marked failed',
    'scans2.list.status.unsolvedTitle': 'CTF2 challenge unsolved; the underlying scan is marked failed',
  },

  ja: {
    // ターゲット種別
    'scans2.kind.url': '公開アプリ',
    'scans2.kind.repo': 'Git リポジトリ',
    'scans2.kind.path': 'ローカルコードパス',
    'scans2.kind.service': 'TCP サービス',
    'scans2.kind.artifact': 'チャレンジ添付ファイル',
    'scans2.kind.url.desc': '稼働中のサービスにリクエストを送り、実際の応答で判断します。',
    'scans2.kind.repo.desc':
      'リポジトリをクローンしてコードを読み、疑わしい箇所を動的に検証します。',
    'scans2.kind.path.desc': 'ローカルディレクトリのコードをネットワークを介さず直接分析します。',
    'scans2.kind.service.desc':
      '生の TCP ポートに直接接続してバイトを送受信します（Pwn、ネットワーク Misc、Crypto オラクル）。HTTP 層はありません。',
    'scans2.kind.artifact.desc':
      'リバースエンジニアリング、ファイルのみの Misc、暗号 — すべて添付ファイル内にあり、接続先アドレスはありません。',
    'scans2.kindHint.url.placeholder': 'https://api.example.com',
    'scans2.kindHint.url.example': 'https://api.acme.internal',
    'scans2.kindHint.repo.placeholder': 'https://github.com/org/repo',
    'scans2.kindHint.repo.example': 'https://github.com/acme/backend',
    'scans2.kindHint.path.placeholder': '/srv/app または D:\\projects\\acme',
    'scans2.kindHint.path.example': '/srv/app',
    'scans2.kindHint.service.placeholder': 'ホスト:ポート。プラットフォームの接続コマンドを貼り付けても可',
    'scans2.kindHint.service.example': '1.2.3.4:9999',
    'scans2.kindHint.artifact.placeholder': '入力不要',
    'scans2.kindHint.artifact.example': 'この種のチャレンジは添付ファイルのみで完了します',

    // スキル
    'scans2.skill.web.label': 'Web アプリ',
    'scans2.skill.web.desc': 'フォーム、認証、セッション、インジェクション系の問題。',
    'scans2.skill.api.label': 'API エンドポイント',
    'scans2.skill.api.desc':
      'OpenAPI / GraphQL 記述に従い、エンドポイントごとに認可と入力処理を検査します。',
    'scans2.skill.container.label': 'コンテナ設定',
    'scans2.skill.container.desc': 'Dockerfile、compose、K8s マニフェストの権限・設定不備。',
    'scans2.skill.cloud.label': 'クラウド設定',
    'scans2.skill.cloud.desc': 'IaC テンプレートの公開バケット、過剰な IAM、暗号化漏れ。',
    'scans2.skill.dependencies.label': '依存関係の脆弱性',
    'scans2.skill.dependencies.desc':
      'ロックファイル内のコンポーネント版と既知の脆弱性データベースを照合します。',
    'scans2.skill.secrets.label': 'シークレット漏洩',
    'scans2.skill.secrets.desc':
      'コードと設定からハードコードされた認証情報、トークン、秘密鍵を探します。',
    'scans2.skill.protocol.label': 'プロトコル解析',
    'scans2.skill.protocol.desc':
      '生の TCP ポートでのプロトコルリバースとバイナリ操作（Pwn、ネットワーク Misc、Crypto オラクル）。',
    'scans2.skill.needsCredentials': '認証情報が必要',

    // 成功基準
    'scans2.goal.findVulns.label': '定例セキュリティ評価',
    'scans2.goal.findVulns.hint':
      '実在する問題をできるだけ洗い出し、深刻度を評価して修正案を示します。',
    'scans2.goal.captureFlag.label': 'CTF キャプチャー・ザ・フラッグ',
    'scans2.goal.captureFlag.hint':
      '低重大度の問題を広く列挙するより、flag に通じる経路を深掘りします。',
    'scans2.goal.verifyFix.label': '再テスト（既知の問題が修正済みか確認）',
    'scans2.goal.verifyFix.hint':
      '既知の問題が修正されたかを一つずつ確認し、新たな発見は末尾に別掲します。',

    // ウィザードのステップ
    'scans2.step.target': 'ターゲット',
    'scans2.step.skills': '検査範囲',
    'scans2.step.provider': 'モデル',
    'scans2.step.brief': 'タスク背景',
    'scans2.step.confirm': '確認',

    // ステップ 1 · ターゲット
    'scans2.target.whatLabel': '何をテストするか',
    'scans2.target.whatHelp':
      'Suvex は公開済みアプリを直接テストでき、コードベースを読んでホワイトボックス分析もできます。',
    'scans2.target.address': 'ターゲットアドレス',
    'scans2.target.artifactBody':
      '次のステップ「タスク背景」で問題ファイルをアップロードしてください（zip / ELF / パケットキャプチャ可）。添付は作業ディレクトリに展開され、コードディレクトリとして分析されます — ネットワークリクエストは一切送信しません。',
    'scans2.target.uploaded': '{n} 個の添付ファイルをアップロード済み：{names}',
    'scans2.target.noAttachment':
      '添付ファイルがまだありません。この種のチャレンジは少なくとも 1 つ必要で、ないと送信できません。',
    'scans2.name.label': 'スキャン名',
    'scans2.name.artifactHelp': '空欄の場合は問題の添付ファイル名を使用します。',
    'scans2.name.help': '空欄の場合はターゲットアドレスを使用します。',
    'scans2.name.autoFromAttachment': '添付後に自動命名',
    'scans2.name.optional': '任意',

    // ステップ 2 · スキル範囲
    'scans2.skills.scope': '検査範囲',
    'scans2.skills.scopeHelp':
      '選択が多いほどスキャンは遅くなり、消費トークンも増えます。まずはコア項目から実行してください。',
    'scans2.skills.inapplicable': '次のスキルは {kind} には適用されません：',
    'scans2.skills.selectOne': '少なくとも 1 つ選択してください。でないとスキャンに検出能力がありません',

    // ステップ 3 · モデル
    'scans2.provider.none':
      '利用可能なモデルがまだ設定されていません。スキャンには手順の計画と結果の判断に使える LLM が必要です。',
    'scans2.provider.goSettings': '設定でモデルを構成する',
    'scans2.provider.label': '使用するモデル',
    'scans2.provider.help': 'スキャン時の推論品質とコストを決めます。',
    'scans2.provider.local': 'ローカル',
    'scans2.provider.configure': '設定する',

    // ステップ 4 · 背景
    'scans2.brief.introBefore': 'このセクションは',
    'scans2.brief.introAfter':
      'タスク背景としてエージェントに渡されます。問題文、ヒント、テスト範囲、テストアカウントをここに書けます — どのスキャンパラメータよりも結果を左右します。',
    'scans2.brief.introOptional': '。URL だけを渡すと、エージェントは盲目的にスキャンするしかありません。',
    'scans2.brief.label': 'タスク背景',
    'scans2.brief.placeholder':
      '例：\n問題：LogVault — ログテンプレートエンジンの汚染データフロー（PWN、500 点）\nヒント：%hhn と varargs の境界条件に注目。問題文の「セキュリティメモ」はデコイ。\n目標：flag を取得、形式は flag{...}\n添付は下記（バイナリ chall）。',
    'scans2.brief.credentialsNote':
      'パスワードを書いても構いません：プロセス一覧には現れません（命令ファイルに保存され、このマシンでのみ読めます）。',
    'scans2.brief.attachments': '問題の添付ファイル',
    'scans2.brief.attachmentsRequired': '（この種のチャレンジでは必須）',
    'scans2.brief.attachmentsOptional': '（任意）',
    'scans2.brief.remove': '{name} を削除',
    'scans2.brief.uploading': 'アップロード中…',
    'scans2.brief.chooseFile': 'ファイルを選択',
    'scans2.brief.uploadBefore':
      '種類は任意（バイナリ、ソース、辞書、パケットキャプチャ）。サンドボックスワークスペースの',
    'scans2.brief.uploadAfter': 'ディレクトリに置かれ、エージェントが直接読めます。',
    'scans2.brief.requiredNote':
      ' この種のチャレンジにはターゲットアドレスがなく、添付ファイル自体が検査対象です。',
    'scans2.brief.missingAttachment':
      'この種のチャレンジは少なくとも 1 つの添付ファイルが必要です — ないと分析対象がありません。上の「ファイルを選択」をクリックしてください。',

    // ステップ 5 · 確認
    'scans2.confirm.artifactBefore': 'アップロードされた問題の添付ファイル ',
    'scans2.confirm.artifactAfter': ' に対してホワイトボックス分析を実行し、',
    'scans2.confirm.targetBefore': '',
    'scans2.confirm.targetAfter': ' に対してアクティブスキャンを開始し、',
    'scans2.confirm.beforeSkills': '、次を有効にします：',
    'scans2.confirm.capabilities': ' など {n} 項目の検出能力。',
    'scans2.confirm.artifactNote':
      '添付ファイルはサンドボックスの作業ディレクトリに展開して分析され、どこにもパケットを送りません。これらのファイルを閲覧する権限があることを確認してください。',
    'scans2.confirm.networkNote':
      'スキャンはターゲットに実際のリクエストを送るため、相手側のアラートやログを引き起こす可能性があります。このターゲットをテストする権限があることを確認してください。',
    'scans2.confirm.kind': 'ターゲット種別',
    'scans2.confirm.target': 'ターゲット',
    'scans2.confirm.model': 'モデル',
    'scans2.confirm.authNote':
      '未承認のターゲットへのスキャンは多くの法域で違法です。この確認はサーバー時刻と送信元とともにスキャン履歴に記録されます — 画面だけのチェックではありません。',
    'scans2.common.unselected': '未選択',
    'scans2.common.edit': '変更',
    'scans2.common.required': '必須',
    'scans2.common.listSep': '、',

    // 起動ボタンとウィザード枠
    'scans2.launch.start': 'スキャン開始',
    'scans2.launch.starting': '起動中…',
    'scans2.launch.needAttachment':
      'この種のチャレンジは問題の添付ファイルが必要です。「タスク背景」ステップに戻ってアップロードしてください',
    'scans2.launch.invalidTarget': 'ターゲットアドレスが無効です。最初のステップに戻って修正してください',
    'scans2.launch.noSkills': '検出能力が選択されていません',
    'scans2.launch.noProvider': '利用可能なモデルが選択されていません',
    'scans2.launch.needAuthorization': 'このターゲットをテストする権限があることを先に確認してください',
    'scans2.wizard.title': '新規スキャン',
    'scans2.wizard.close': '閉じる',
    'scans2.wizard.cancel': 'キャンセル',
    'scans2.wizard.back': '戻る',
    'scans2.wizard.next': '次へ',
    'scans2.wizard.progress': 'ステップ {current}/{total}：{step}',
    'scans2.wizard.stepsAria': '新規スキャンのステップ',

    // 一括操作
    'scans2.bulk.selected': '{n} 件選択中',
    'scans2.bulk.compare': '比較',
    'scans2.bulk.archive': 'アーカイブ',
    'scans2.bulk.unarchive': 'アーカイブ解除',
    'scans2.bulk.rerun': '再スキャン',
    'scans2.bulk.export': 'エクスポート',
    'scans2.bulk.delete': '削除',
    'scans2.bulk.restore': '復元',
    'scans2.bulk.purge': '完全削除',
    'scans2.bulk.clear': '選択解除',

    // 比較ダイアログ
    'scans2.compare.title': 'スキャン比較',
    'scans2.compare.target': 'ターゲット {target}',
    'scans2.compare.close': '閉じる',
    'scans2.compare.failed': '比較に失敗しました：{error}',
    'scans2.compare.loading': '比較中…',
    'scans2.compare.diffTargetsPrefix': 'この 2 回のスキャンはターゲットが異なります（',
    'scans2.compare.diffTargetsMid': ' と ',
    'scans2.compare.diffTargetsSuffix':
      '）— 下の「追加 / 消失」は脆弱性タイトルで照合したもので、ターゲットが異なる場合は「問題が増えた / 直った」の証拠には通常なりません。',
    'scans2.compare.added': '追加',
    'scans2.compare.addedHint': '今回初めて出現 — 新たに混入した問題の可能性',
    'scans2.compare.removed': '消失',
    'scans2.compare.removedHint': '前回はあり今回なし — 修正済みの可能性',
    'scans2.compare.common': '両方に存在（{n}）',

    // ヒント
    'scans2.hint.sent': 'ヒントを送信しました。次ラウンドから有効になります',
    'scans2.hint.failed': '送信に失敗しました',
    'scans2.hint.noneRunning': '実行中のスキャンはありません',
    'scans2.hint.placeholder':
      '例：ログイン API は /api/v2 にあります。/index.php への試行はやめてください',
    'scans2.hint.sending': '送信中…',
    'scans2.hint.send': 'ヒントを送信',

    // スキャン一覧
    'scans2.list.fail.userStopped': 'ユーザーによる停止',
    'scans2.list.fail.systemStopped': 'システム中断',
    'scans2.list.fail.stopped': '停止済み',
    'scans2.list.fail.infra': 'インフラ障害',
    'scans2.list.terminated.user': 'ユーザー本人',
    'scans2.list.terminated.system': 'システム',
    'scans2.list.terminated.unknown': '判定不能',
    'scans2.list.status.pendingConfirmation': '候補の確認待ち',
    'scans2.list.status.unsolved': '未解決',
    'scans2.list.select': '{target} を選択',
    'scans2.list.viewActivity': '{name} のエージェント活動を表示',
    'scans2.list.terminatedByTitle': 'このスキャンを停止した主体',
    'scans2.list.terminatedBy': '停止主体：{by}',
    'scans2.list.stalledHint':
      'サーバーがこのスキャンの新しい活動イベントを長く受信していません。よくある原因は、コマンドがいつまでも返らない、またはターゲットのプロトコルが実際と異なることです。実際にはまだ実行中（静かなだけ）なら、新しいイベントが届くとこの表示は自動的に消えます。',
    'scans2.list.stalled': '停止疑い · {silent}',
    'scans2.list.noEntriesHint.ir':
      'このレビューは脆弱性項目を出力していません。成果物は侵入タイムラインと対応提案であり、脆弱性の件数では測りません。',
    'scans2.list.noEntriesHint.flag':
      'このスキャンは脆弱性項目を出力していません。キャプチャー・ザ・フラッグの成否は flag を取得できたかで判断し、件数では測りません。',
    'scans2.list.noEntriesHint.verify':
      'この再テストは新しい脆弱性項目を出力していません。既知の問題が修正されたかで判断し、件数では測りません。',
    'scans2.list.irConclusion': '結論はレビュー報告を参照',
    'scans2.list.severityCount': '{severity} {n} 件',
    'scans2.list.incompleteTitle':
      'スキャンが途中で停止されたため、この数値は完了した部分のみで全体を表しません',
    'scans2.list.incomplete': '· 不完全',
    'scans2.list.running': '実行中',
    'scans2.list.viewScanActivity': 'このスキャンのエージェント活動を表示',
    'scans2.list.writeupTitle': 'このキャプチャー・ザ・フラッグの Writeup を表示（Markdown）',
    'scans2.list.writeup': 'Writeup',
    'scans2.list.rerun': '再実行（新しい記録を作成）',
    'scans2.list.resumeTitle': '前回のセッションに戻って続行 — 済んだ推論は繰り返しません',
    'scans2.list.resume': '続行（前回のセッションから）',
    'scans2.list.abort': 'スキャンを停止',

    // 保存済みビュー
    'scans2.views.activeTitle': '現在のビュー：{name}（{desc}）',
    'scans2.views.saveHint': '現在の絞り込み条件を保存し、次回ワンクリックで適用',
    'scans2.views.saveCurrent': '現在の絞り込みを保存',
    'scans2.views.namePlaceholder': '例：社内 高重大度 未修正',
    'scans2.views.filterFirst': '先に絞り込み条件を設定',
    'scans2.views.save': '保存',
    'scans2.views.limit': 'ビューは最大 {n} 個です。不要なものを先に削除してください。',
    'scans2.views.loadFailed': '読み込みに失敗しました',
    'scans2.views.retry': '再試行',
    'scans2.views.empty':
      '保存済みのビューはありません。上で絞り込み条件を設定して名前を付けてください。',
    'scans2.views.remove': 'ビュー {name} を削除',
    'scans2.views.removeTitle': 'このビューを削除（スキャンデータには影響しません）',

    // ページ
    'scans2.page.toast.abortFailed': '停止に失敗しました：{error}',
    'scans2.page.toast.rerunFailed': '再スキャンに失敗しました：{error}',
    'scans2.page.toast.resuming': '続行中：{name}…',
    'scans2.page.toast.resumed': '続行しました：{name}',
    'scans2.page.toast.resumeFailed': '続行に失敗しました：{error}',
    'scans2.page.toast.undo': '元に戻す',
    'scans2.page.toast.undoFailed': '元に戻せませんでした：{error}',
    'scans2.page.bulk.archived': '{n} 件をアーカイブしました',
    'scans2.page.bulk.archiveFailed': 'アーカイブに失敗しました',
    'scans2.page.bulk.unarchived': '{n} 件のアーカイブを解除しました',
    'scans2.page.bulk.unarchiveFailed': 'アーカイブ解除に失敗しました',
    'scans2.page.bulk.trashed': '{n} 件をごみ箱に移動しました',
    'scans2.page.bulk.deleteFailed': '削除に失敗しました',
    'scans2.page.bulk.restored': '{n} 件を復元しました',
    'scans2.page.bulk.restoreFailed': '復元に失敗しました',
    'scans2.page.bulk.purged': '{n} 件を完全削除しました',
    'scans2.page.bulk.purgePartial': '{ok} 件を削除、{failed} 件は拒否されました：{error}',
    'scans2.page.bulk.rerunCreated': '{n} 件を再実行しました',
    'scans2.page.bulk.rerunFailedCount': '{n} 件失敗',
    'scans2.page.bulk.exported': '{n} 件を CSV としてエクスポートしました',
    'scans2.page.stall.title':
      '実行中のスキャン {n} 件が同じ操作を繰り返しています（空回りの可能性）：',
    'scans2.page.stall.repeats': '連続 {n} 回 · ',
    'scans2.page.filterAria': 'ステータスで絞り込み',
    'scans2.page.onlyScan': 'スキャン {id} のみ表示',
    'scans2.page.only': 'のみ表示 ',
    'scans2.page.sort.desc': '現在は降順。クリックで昇順に切り替え',
    'scans2.page.sort.asc': '現在は昇順。クリックで降順に切り替え',
    'scans2.page.confirm.deleteTitle': 'スキャン {n} 件を削除しますか？',
    'scans2.page.confirm.deleteBody':
      'ごみ箱に移動され、いつでも復元できます。データが実際に消えるのは「完全削除」のみです。保持日数は設定で変更できます。',
    'scans2.page.confirm.trash': 'ごみ箱に移動',
    'scans2.page.confirm.purgeTitle': 'スキャン {n} 件を完全削除しますか？',
    'scans2.page.confirm.purgeBody':
      'この操作は取り消せません。関連する脆弱性、イベント、エージェント記録も併せて削除されます。いずれかのレポートが参照している場合、その項目は拒否され、どのレポートかがエラーに示されます。',
    'scans2.page.confirm.purge': '完全削除',
    'scans2.page.created.title': 'スキャンをキューに追加しました',
    'scans2.page.created.viewActivity': '活動を表示',
    'scans2.page.created.dismiss': '通知を閉じる',
    'scans2.page.error.loadList': 'スキャン一覧の読み込みに失敗しました',
    'scans2.page.error.load': '読み込みに失敗しました',
    'scans2.page.empty.archived.title': 'アーカイブ済みのスキャンはありません',
    'scans2.page.empty.archived.body':
      'アーカイブすると、追跡しない記録がデフォルト一覧から外れます — データは削除されず、いつでも解除できます。「設定 → スキャン保持ルール」で、失敗・中止したスキャンを期限で自動アーカイブできます。',
    'scans2.page.empty.deleted.title': 'ごみ箱は空です',
    'scans2.page.empty.deleted.body':
      '一覧から削除したスキャンはまずここに来て、保持期間が過ぎてから実際に消去されます — 保持日数は「設定 → スキャン保持ルール」で変更できます。それまではいつでも復元できます。',
    'scans2.list.status.pendingTitle': 'CTF2 の課題に候補 flag があり確認待ちです。基盤のスキャン状態は失敗です',
    'scans2.list.status.unsolvedTitle': 'CTF2 の課題は未解決です。基盤のスキャン状態は失敗です',
  },

  ru: {},
  fr: {},
  de: {},
}
