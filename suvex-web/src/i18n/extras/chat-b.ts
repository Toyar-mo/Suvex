import type { ExtraLocaleMap } from './index'

/** 对话控件、子代理、会话菜单与弹窗的写死文案。 */
export const chat_b: ExtraLocaleMap = {
  'zh-CN': {
    // ── 人格弹窗 ──
    'chat.b.personaIntroLead': '角色会改变它',
    'chat.b.personaIntroFocus': '关注什么、怎么说',
    'chat.b.personaIntroTail':
      '；如果这个角色在 Agent 配置里限定了工具，可用工具也会相应收窄。选中后，这段角色说明会进入本轮系统提示词。',

    // ── 模型接入点 ──
    'chat.b.providerDefault': '平台默认模型',

    // ── 归属项目 ──
    'chat.b.projectTitle': '项目：{name}',
    'chat.b.projectHint': '归到一个项目里（侧栏会分组）',
    'chat.b.noProject': '无项目',

    // ── 附件条 ──
    'chat.b.attachment': '附件',
    'chat.b.uploading': '上传中…',
    'chat.b.removeAttachment': '移除附件 {name}',

    // ── 审批开关 ──
    'chat.b.approvalOnHint': '有副作用的操作（建扫描、改技能库、加 MCP）会先停下来等你确认',
    'chat.b.approvalOffHint': '它可以直接执行有副作用的操作，不再问你',
    'chat.b.approvalOnLabel': '执行前确认',
    'chat.b.approvalOffLabel': '直接执行',

    // ── 审批卡 ──
    'chat.b.approvalHint.startCompetitionRace':
      '赛事自动托管：自动拉取整场比赛的题，用多个模型对每道题赛跑解题，先解出的模型胜出，并会把 flag 自动提交（每题有提交次数上限）。会消耗较多算力/费用。',
    'chat.b.approvalHint.stopCompetitionRace':
      '停止正在进行的赛事自动托管（正在跑的那道题会在工具返回后结束）。',
    'chat.b.unknownTool': '（未知）',
    'chat.b.approve': '执行',
    'chat.b.reject': '拒绝',

    // ── 子代理 ──
    'chat.b.toolFailedSuffix': ' · 失败',
  },
  'zh-TW': {
    'chat.b.personaIntroLead': '角色會改變它',
    'chat.b.personaIntroFocus': '關注什麼、怎麼說',
    'chat.b.personaIntroTail':
      '；如果這個角色在 Agent 設定裡限定了工具，可用工具也會相應收窄。選中後，這段角色說明會進入本輪系統提示詞。',
    'chat.b.providerDefault': '平台預設模型',
    'chat.b.projectTitle': '專案：{name}',
    'chat.b.projectHint': '歸到一個專案裡（側欄會分組）',
    'chat.b.noProject': '無專案',
    'chat.b.attachment': '附件',
    'chat.b.uploading': '上傳中…',
    'chat.b.removeAttachment': '移除附件 {name}',
    'chat.b.approvalOnHint': '有副作用的操作（建掃描、改技能庫、加 MCP）會先停下來等你確認',
    'chat.b.approvalOffHint': '它可以直接執行有副作用的操作，不再問你',
    'chat.b.approvalOnLabel': '執行前確認',
    'chat.b.approvalOffLabel': '直接執行',
    'chat.b.approvalHint.startCompetitionRace':
      '賽事自動託管：自動拉取整場比賽的題，用多個模型對每道題賽跑解題，先解出的模型勝出，並會把 flag 自動提交（每題有提交次數上限）。會消耗較多算力／費用。',
    'chat.b.approvalHint.stopCompetitionRace':
      '停止正在進行的賽事自動託管（正在跑的那道題會在工具返回後結束）。',
    'chat.b.unknownTool': '（未知）',
    'chat.b.approve': '執行',
    'chat.b.reject': '拒絕',
    'chat.b.toolFailedSuffix': ' · 失敗',
  },
  en: {
    'chat.b.personaIntroLead': 'A role changes what it ',
    'chat.b.personaIntroFocus': 'pays attention to and how it speaks',
    'chat.b.personaIntroTail':
      '; if the role restricts tools in Agent settings, the available tools narrow accordingly. Once selected, this role description goes into this round’s system prompt.',
    'chat.b.providerDefault': 'Platform default model',
    'chat.b.projectTitle': 'Project: {name}',
    'chat.b.projectHint': 'Group under a project (sidebar groups sessions)',
    'chat.b.noProject': 'No project',
    'chat.b.attachment': 'Attachment',
    'chat.b.uploading': 'Uploading…',
    'chat.b.removeAttachment': 'Remove attachment {name}',
    'chat.b.approvalOnHint':
      'Side-effecting actions (creating scans, editing the skill library, adding MCP) will pause for your confirmation',
    'chat.b.approvalOffHint': 'It can run side-effecting actions directly without asking',
    'chat.b.approvalOnLabel': 'Confirm first',
    'chat.b.approvalOffLabel': 'Run directly',
    'chat.b.approvalHint.startCompetitionRace':
      'Competition autopilot: pulls every challenge in the race, runs multiple models on each challenge in parallel, and automatically submits flags as soon as a model solves one (each challenge has a submission limit). Uses significant compute and cost.',
    'chat.b.approvalHint.stopCompetitionRace':
      'Stops the running competition autopilot (the challenge currently in progress ends when the tool returns).',
    'chat.b.unknownTool': '(unknown)',
    'chat.b.approve': 'Run',
    'chat.b.reject': 'Reject',
    'chat.b.toolFailedSuffix': ' · Failed',
  },
  ru: {},
  fr: {},
  ja: {
    'chat.b.personaIntroLead': 'ロールは、',
    'chat.b.personaIntroFocus': '何に注目し、どう話すか',
    'chat.b.personaIntroTail':
      'を変えます。ロールが Agent 設定でツールを制限している場合、使えるツールもそれに合わせて絞り込まれます。選択すると、このロール説明が今回のシステムプロンプトに含まれます。',
    'chat.b.providerDefault': 'プラットフォーム既定モデル',
    'chat.b.projectTitle': 'プロジェクト：{name}',
    'chat.b.projectHint': 'プロジェクトにまとめる（サイドバーでグループ化）',
    'chat.b.noProject': 'プロジェクトなし',
    'chat.b.attachment': '添付ファイル',
    'chat.b.uploading': 'アップロード中…',
    'chat.b.removeAttachment': '添付ファイル {name} を削除',
    'chat.b.approvalOnHint':
      '副作用のある操作（スキャン作成、スキルライブラリ変更、MCP 追加）は実行前に確認を待ちます',
    'chat.b.approvalOffHint': '副作用のある操作を確認なしで直接実行できます',
    'chat.b.approvalOnLabel': '実行前に確認',
    'chat.b.approvalOffLabel': '直接実行',
    'chat.b.approvalHint.startCompetitionRace':
      'コンペティション自動運用：レース全体の問題を自動取得し、複数モデルで各問題を並行して解き、最初に解いたモデルのフラグを自動提出します（問題ごとに提出回数上限あり）。多くの計算資源と費用を消費します。',
    'chat.b.approvalHint.stopCompetitionRace':
      '実行中のコンペティション自動運用を停止します（実行中の問題はツールが戻った時点で終了します）。',
    'chat.b.unknownTool': '（不明）',
    'chat.b.approve': '実行',
    'chat.b.reject': '拒否',
    'chat.b.toolFailedSuffix': ' · 失敗',
  },
  de: {},
}
