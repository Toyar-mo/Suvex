import type { ExtraLocaleMap } from './index'

/** 设置页、沙箱/重试/网络面板与密钥字段中剩余的硬编码文案。 */
export const hardcoded_d: ExtraLocaleMap = {
  'zh-CN': {
    'settings2.loadFailed': '设置加载失败',
    'settings2.trust.aria': '凭据处理方式',
    'settings2.trust.lead': '凭据只提交给服务端',
    'settings2.trust.summary': '保存后输入框始终为空 · 明文只在你按住时显示，20 秒后自动隐藏 · 剪贴板 2 秒后清空',
    'settings2.trust.details': '这些承诺在这里怎么兑现',
    'settings2.trust.masked': '密钥输入时即为遮蔽状态；点眼睛图标可看明文，明文 20 秒后自动隐藏，点到别处立即隐藏。',
    'settings2.nav.aria': '设置分区',
    'settings2.section.sandbox': '沙箱',
    'settings2.section.network': '网络限制',
    'settings2.section.data-maintenance': '数据维护',
    'settings2.section.ctf': 'CTF 解题',
    'settings2.section.kernel-core': '内核通用能力',
    'settings2.section.platform': '平台接入',
    'settings2.section.notify': '通知',
    'settings2.section.scan-retention': '扫描保留',
    'settings2.section.benchmark': '评测快照',
    'settings2.section.benchmark-cases': '评测用例',
    'settings2.section.purple': '紫队规则',
    'settings2.section.members': '处理人',
    'settings2.section.account': '账号与安全',
    'settings2.section.users': '用户',
    'kernel2.irCategory.ir-root-cause': '根因分析',
    'kernel2.irCategory.ir-timeline': '时间线还原',
    'kernel2.irCategory.ir-scope': '影响范围',
    'settings2.dataBoundary.body':
      '模型服务、沙箱与执行、通知规则与渠道、扫描保留、访问令牌与登录口令都会真正写到后端并持久化，刷新、换浏览器都还在。',
    'settings2.dataBoundary.secrets': '密钥与令牌',
    'settings2.dataBoundary.storage':
      '（localStorage / Cookie 里都没有）。浏览器存储同源的任何脚本都能读，凭据只应提交给服务端保存。',

    'settings2.sandbox.title': '沙箱与执行环境',
    'settings2.sandbox.lifecyclePrefix': '内核镜像 ',
    'settings2.sandbox.lifecycleSuffix': ' 系列；下面的 CPU / 内存上限作用于这个临时容器。',
    'settings2.sandbox.err.cpu': 'CPU 至少 1 核',
    'settings2.sandbox.err.memory': '内存至少 512 MB',
    'settings2.sandbox.err.timeoutMin': '超时至少 1 分钟',
    'settings2.sandbox.err.timeoutMax': '超时不应超过 24 小时',
    'settings2.sandbox.err.concurrent': '并发应在 1-8 之间',
    'settings2.sandbox.err.turns': '回合上限应在 10-2000 之间',
    'settings2.sandbox.err.invalid': '有配置项超出允许范围，请先修正',
    'settings2.sandbox.unitCore': '核',
    'settings2.sandbox.unitMinute': '分钟',
    'settings2.sandbox.unitScans': '个',
    'settings2.sandbox.unitTurns': '轮',
    'settings2.sandbox.netWarnBefore': '已开启。注意它',
    'settings2.sandbox.netWarnAfter':
      '：https 与 host:port 的扫描仍会联网。真正的出站边界在宿主机的 iptables DOCKER-USER 白名单。',
    'settings2.sandbox.writeWarn': '写入已放行。验证脚本可能修改宿主上的文件，仅在你确信目标代码可丢弃时开启。',
    'settings2.sandbox.execIntroBefore': '这三项',
    'settings2.sandbox.execIntroAfter':
      '：并发几秒内生效；「扫描深度」决定回合的天花板（快速 40 / 标准 80 / 深度 150 轮），「回合上限」在其下再收细 —— 两者取较小值；对之后新发起的扫描生效。都不需要重启服务。',
    'settings2.sandbox.scanDepth': '扫描深度',
    'settings2.sandbox.intensityBefore': '下面这一项会改变 agent 的',
    'settings2.sandbox.intensityAfter': '，不是资源配额。全局生效，不按题目分类设置。',
    'settings2.sandbox.offensive.label': '启用越权与注入强化手法',
    'settings2.sandbox.offensive.hint':
      '打开后，解题与渗透扫描的提示词末尾都会追加一份手法矩阵（注入、越权、文件读取、SSRF、竞态的具体换形写法）。它只扩大试探的覆盖面，不放松任何判定纪律 —— 「不越界访问」「禁止口令爆破与压力测试」照旧成立。',
    'settings2.sandbox.offensive.warn':
      '已启用。agent 会更系统地尝试注入与越权路径，耗时与回合消耗也会相应上升。它不影响判定标准：拿不到证据的结论仍然是「待跟进」，不会因为「打过一遍」就变成「已命中」。',
    'settings2.sandbox.footnote':
      'CPU、内存会下发给沙箱容器；扫描深度决定回合天花板、回合上限再收细；单次超时到点会停掉本次扫描；允许写入挂载卷控制工作区是读写还是只读。禁止出网对 url/服务类目标会被强制放开（否则连不上目标）。',
    'settings2.sandbox.execSource':
      '其中 {labels} 取自服务端环境变量（部署基线），不是你在此处设过的值。保存一次后即以这里为准。',
    'settings2.sandbox.source.concurrent': '并发',
    'settings2.sandbox.source.rounds': '回合上限',
    'settings2.sandbox.source.depth': '扫描深度',

    'settings2.retry.saved': '重试策略已保存',
    'settings2.retry.note':
      '安全窗口（首帧前断流）仍由「未推送则回退非流式」兜底；意图重跑未接入。',
    'settings2.retry.saving': '保存中…',
    'settings2.retry.save': '保存',

    'settings2.network.loadFailed': '网络限制加载失败',
    'settings2.network.empty': '（空）',

    'settings2.secret.required': '必填',
    'settings2.secret.hide': '隐藏密钥',
    'settings2.secret.show': '显示密钥',
    'settings2.secret.hideTitle': '隐藏（20 秒后也会自动隐藏）',
    'settings2.secret.showTitle': '显示明文',
    'settings2.secret.copied': '已复制',
    'settings2.secret.copy': '复制',
    'settings2.secret.autoHide': '明文将在 20 秒后自动隐藏，点击别处也会立即隐藏。',

    'settings2.saveBar.unchanged': '没有需要保存的改动',
  },
  'zh-TW': {},
  en: {
    'settings2.loadFailed': 'Failed to load settings',
    'settings2.trust.aria': 'How credentials are handled',
    'settings2.trust.lead': 'Credentials are only sent to the server',
    'settings2.trust.summary':
      'After saving the field stays empty · plaintext shows only while you press · auto-hides after 20s · clipboard cleared after 2s',
    'settings2.trust.details': 'How these promises hold here',
    'settings2.trust.masked':
      'Secrets are masked while typing; press the eye icon to reveal plaintext, which auto-hides after 20 seconds or when focus leaves.',
    'settings2.nav.aria': 'Settings sections',
    'settings2.section.sandbox': 'Sandbox',
    'settings2.section.network': 'Network restrictions',
    'settings2.section.data-maintenance': 'Data maintenance',
    'settings2.section.ctf': 'CTF solving',
    'settings2.section.kernel-core': 'Kernel capabilities',
    'settings2.section.platform': 'Platform access',
    'settings2.section.notify': 'Notifications',
    'settings2.section.scan-retention': 'Scan retention',
    'settings2.section.benchmark': 'Benchmark snapshots',
    'settings2.section.benchmark-cases': 'Benchmark cases',
    'settings2.section.purple': 'Purple team rules',
    'settings2.section.members': 'Handlers',
    'settings2.section.account': 'Account & security',
    'settings2.section.users': 'Users',
    'kernel2.irCategory.ir-root-cause': 'Root cause analysis',
    'kernel2.irCategory.ir-timeline': 'Timeline reconstruction',
    'kernel2.irCategory.ir-scope': 'Impact scope',
    'settings2.dataBoundary.body':
      'Model services, sandbox and execution, notification rules and channels, scan retention, access tokens and sign-in passwords are all written to the backend and persisted — they survive refreshes and browser changes.',
    'settings2.dataBoundary.secrets': 'Secrets and tokens',
    'settings2.dataBoundary.storage':
      ' (not in localStorage or cookies). Any script on the same origin as browser storage can read it, so credentials should only be stored on the server.',

    'settings2.sandbox.title': 'Sandbox and execution',
    'settings2.sandbox.lifecyclePrefix': 'Kernel images from the ',
    'settings2.sandbox.lifecycleSuffix':
      ' series; the CPU / memory limits below apply to this temporary container.',
    'settings2.sandbox.err.cpu': 'CPU must be at least 1 core',
    'settings2.sandbox.err.memory': 'Memory must be at least 512 MB',
    'settings2.sandbox.err.timeoutMin': 'Timeout must be at least 1 minute',
    'settings2.sandbox.err.timeoutMax': 'Timeout must not exceed 24 hours',
    'settings2.sandbox.err.concurrent': 'Concurrency must be between 1 and 8',
    'settings2.sandbox.err.turns': 'Turn limit must be between 10 and 2000',
    'settings2.sandbox.err.invalid': 'Some settings are out of range — fix them first',
    'settings2.sandbox.unitCore': 'cores',
    'settings2.sandbox.unitMinute': 'min',
    'settings2.sandbox.unitScans': 'jobs',
    'settings2.sandbox.unitTurns': 'turns',
    'settings2.sandbox.netWarnBefore': 'Enabled. Note that it',
    'settings2.sandbox.netWarnAfter':
      ': scans of https and host:port targets still reach the network. The real egress boundary is the iptables DOCKER-USER allowlist on the host.',
    'settings2.sandbox.writeWarn':
      'Write access granted. Verification scripts may modify files on the host — enable this only when you are sure the target code is disposable.',
    'settings2.sandbox.execIntroBefore': 'These three',
    'settings2.sandbox.execIntroAfter':
      ': concurrency takes effect within seconds; scan depth sets the turn ceiling (quick 40 / standard 80 / deep 150 turns), and the turn limit narrows it further — the smaller value wins. They apply to scans started afterwards. No service restart needed.',
    'settings2.sandbox.scanDepth': 'Scan depth',
    'settings2.sandbox.intensityBefore': "The option below changes the agent's",
    'settings2.sandbox.intensityAfter':
      ', not a resource quota. It applies globally, not per challenge category.',
    'settings2.sandbox.offensive.label': 'Enable privilege-escalation and injection techniques',
    'settings2.sandbox.offensive.hint':
      "When enabled, prompts for solving and pentest scans append a technique matrix (concrete variants for injection, privilege escalation, file read, SSRF and race conditions). It only broadens coverage — it never relaxes judgment discipline: 'no out-of-scope access' and 'no password brute-force or stress testing' still hold.",
    'settings2.sandbox.offensive.warn':
      "Enabled. The agent will systematically try injection and privilege-escalation paths, so time and turn consumption rise accordingly. Judgment standards are unchanged: a conclusion without evidence stays 'needs follow-up' and does not become 'confirmed' just because the technique was tried.",
    'settings2.sandbox.footnote':
      'CPU and memory are passed to the sandbox container; scan depth sets the turn ceiling and the turn limit narrows it; a single-run timeout stops that scan when reached; volume write access controls whether the workspace is read-write or read-only. For url/service targets, network isolation is forced off (otherwise the target cannot be reached).',
    'settings2.sandbox.execSource':
      'Of these, {labels} come from server environment variables (deployment baseline), not from values you set here. Once you save, these settings take precedence.',
    'settings2.sandbox.source.concurrent': 'concurrency',
    'settings2.sandbox.source.rounds': 'turn limit',
    'settings2.sandbox.source.depth': 'scan depth',

    'settings2.retry.saved': 'Retry policy saved',
    'settings2.retry.note':
      'The safety window (stream cut before the first frame) is still covered by falling back to non-streaming when nothing was pushed; intent replay is not wired up.',
    'settings2.retry.saving': 'Saving…',
    'settings2.retry.save': 'Save',

    'settings2.network.loadFailed': 'Failed to load network restrictions',
    'settings2.network.empty': '(empty)',

    'settings2.secret.required': 'Required',
    'settings2.secret.hide': 'Hide secret',
    'settings2.secret.show': 'Show secret',
    'settings2.secret.hideTitle': 'Hide (auto-hides after 20s)',
    'settings2.secret.showTitle': 'Show plaintext',
    'settings2.secret.copied': 'Copied',
    'settings2.secret.copy': 'Copy',
    'settings2.secret.autoHide':
      'Plaintext hides automatically after 20 seconds, or immediately when you click elsewhere.',

    'settings2.saveBar.unchanged': 'No changes to save',
  },
  ru: {},
  fr: {},
  ja: {
    'settings2.loadFailed': '設定の読み込みに失敗しました',
    'settings2.trust.aria': '認証情報の取り扱い',
    'settings2.trust.lead': '認証情報はサーバーにのみ送信されます',
    'settings2.trust.summary':
      '保存後は入力欄が常に空 · 平文は押している間だけ表示され 20 秒後に自動で隠れます · クリップボードは 2 秒後に消去',
    'settings2.trust.details': 'これらの約束がここでどう守られるか',
    'settings2.trust.masked':
      'シークレットは入力時からマスクされます。目のアイコンで平文を表示でき、20 秒後に自動で隠れ、別の場所をクリックすると即座に隠れます。',
    'settings2.nav.aria': '設定セクション',
    'settings2.section.sandbox': 'サンドボックス',
    'settings2.section.network': 'ネットワーク制限',
    'settings2.section.data-maintenance': 'データメンテナンス',
    'settings2.section.ctf': 'CTF 問題解決',
    'settings2.section.kernel-core': 'カーネル共通機能',
    'settings2.section.platform': 'プラットフォーム連携',
    'settings2.section.notify': '通知',
    'settings2.section.scan-retention': 'スキャン保持',
    'settings2.section.benchmark': 'ベンチマークスナップショット',
    'settings2.section.benchmark-cases': 'ベンチマークケース',
    'settings2.section.purple': 'パープルチームルール',
    'settings2.section.members': '担当者',
    'settings2.section.account': 'アカウントとセキュリティ',
    'settings2.section.users': 'ユーザー',
    'kernel2.irCategory.ir-root-cause': '根本原因分析',
    'kernel2.irCategory.ir-timeline': 'タイムライン復元',
    'kernel2.irCategory.ir-scope': '影響範囲',
    'settings2.dataBoundary.body':
      'モデルサービス、サンドボックスと実行、通知ルールとチャネル、スキャン保持、アクセストークンとログインパスワードはすべてバックエンドに保存され、更新やブラウザの変更後も残ります。',
    'settings2.dataBoundary.secrets': 'シークレットとトークン',
    'settings2.dataBoundary.storage':
      '（localStorage / Cookie にはありません）。ブラウザストレージは同一オリジンの任意のスクリプトから読めるため、認証情報はサーバー側にのみ保存してください。',

    'settings2.sandbox.title': 'サンドボックスと実行環境',
    'settings2.sandbox.lifecyclePrefix': 'カーネルイメージ ',
    'settings2.sandbox.lifecycleSuffix':
      ' 系。以下の CPU / メモリ上限はこの一時コンテナに適用されます。',
    'settings2.sandbox.err.cpu': 'CPU は最低 1 コア必要です',
    'settings2.sandbox.err.memory': 'メモリは最低 512 MB 必要です',
    'settings2.sandbox.err.timeoutMin': 'タイムアウトは最低 1 分必要です',
    'settings2.sandbox.err.timeoutMax': 'タイムアウトは 24 時間以内にしてください',
    'settings2.sandbox.err.concurrent': '同時実行数は 1〜8 の範囲で指定してください',
    'settings2.sandbox.err.turns': 'ターン上限は 10〜2000 の範囲で指定してください',
    'settings2.sandbox.err.invalid': '範囲外の設定があります。先に修正してください',
    'settings2.sandbox.unitCore': 'コア',
    'settings2.sandbox.unitMinute': '分',
    'settings2.sandbox.unitScans': '件',
    'settings2.sandbox.unitTurns': 'ターン',
    'settings2.sandbox.netWarnBefore': '有効です。ただし',
    'settings2.sandbox.netWarnAfter':
      '：https と host:port のスキャンは引き続きネットワークに接続します。実際の外向き境界はホスト側の iptables DOCKER-USER 許可リストです。',
    'settings2.sandbox.writeWarn':
      '書き込みを許可しました。検証スクリプトはホスト上のファイルを変更する可能性があります。対象コードを破棄してよいと確信できる場合のみ有効にしてください。',
    'settings2.sandbox.execIntroBefore': 'この 3 項目は',
    'settings2.sandbox.execIntroAfter':
      '：同時実行数は数秒で反映されます。「スキャン深度」がターンの上限を決め（クイック 40 / 標準 80 / ディープ 150 ターン）、「ターン上限」がその下でさらに絞ります — 小さい方が優先されます。以降に開始するスキャンに適用され、サービスの再起動は不要です。',
    'settings2.sandbox.scanDepth': 'スキャン深度',
    'settings2.sandbox.intensityBefore': '以下の項目は agent の',
    'settings2.sandbox.intensityAfter':
      'を変えるもので、リソース割り当てではありません。グローバルに適用され、問題カテゴリ別の設定ではありません。',
    'settings2.sandbox.offensive.label': '権限昇格・インジェクション強化手法を有効化',
    'settings2.sandbox.offensive.hint':
      '有効にすると、問題解決とペネトレーションスキャンのプロンプト末尾に手法マトリクス（インジェクション、権限昇格、ファイル読み取り、SSRF、競合状態の具体的な変形）が追加されます。試行の範囲が広がるだけで、判定規律は緩みません —「範囲外アクセス禁止」「パスワード総当たり・負荷テスト禁止」は引き続き有効です。',
    'settings2.sandbox.offensive.warn':
      '有効です。agent はインジェクションと権限昇格の経路をより体系的に試すため、所要時間とターン消費も増えます。判定基準は変わりません。証拠のない結論は「要フォロー」のままで、「一度試した」だけで「検出済み」にはなりません。',
    'settings2.sandbox.footnote':
      'CPU とメモリはサンドボックスコンテナに渡されます。スキャン深度がターンの上限を決め、ターン上限がさらに絞ります。単発タイムアウトに達するとそのスキャンを停止します。ボリューム書き込み許可はワークスペースを読み書き可能にするか読み取り専用にするかを制御します。url/サービス系の対象ではネットワーク遮断は強制的に解除されます（でないと対象に接続できません）。',
    'settings2.sandbox.execSource':
      'このうち {labels} はサーバーの環境変数（デプロイ基準）由来で、ここで設定した値ではありません。一度保存すると、以降はここの値が優先されます。',
    'settings2.sandbox.source.concurrent': '同時実行数',
    'settings2.sandbox.source.rounds': 'ターン上限',
    'settings2.sandbox.source.depth': 'スキャン深度',

    'settings2.retry.saved': 'リトライポリシーを保存しました',
    'settings2.retry.note':
      'セーフティウィンドウ（最初のフレーム前の切断）は「何も送られなければ非ストリーミングにフォールバック」で引き続き担保されます。意図の再実行は未対応です。',
    'settings2.retry.saving': '保存中…',
    'settings2.retry.save': '保存',

    'settings2.network.loadFailed': 'ネットワーク制限の読み込みに失敗しました',
    'settings2.network.empty': '（空）',

    'settings2.secret.required': '必須',
    'settings2.secret.hide': 'シークレットを隠す',
    'settings2.secret.show': 'シークレットを表示',
    'settings2.secret.hideTitle': '隠す（20 秒後にも自動で隠れます）',
    'settings2.secret.showTitle': '平文を表示',
    'settings2.secret.copied': 'コピーしました',
    'settings2.secret.copy': 'コピー',
    'settings2.secret.autoHide':
      '平文は 20 秒後に自動で隠れ、別の場所をクリックすると即座に隠れます。',

    'settings2.saveBar.unchanged': '保存する変更はありません',
  },
  de: {},
}
