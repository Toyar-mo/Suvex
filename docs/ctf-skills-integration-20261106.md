# CTF 专项技能包 ljagiello/ctf-skills 导入 + CTF 方向默认调用（v1.71）

日期：2026-11-06

## 一、用户要求

> 安装专项技能包 ljagiello/ctf-skills 到项目，配置好在 CTF 方向默认调用。

三件事：①把 `ljagiello/ctf-skills` 装进项目；②配置好；③在 CTF 方向**默认调用**（不用每次手动挂）。

## 二、上游仓库

- 地址 `https://github.com/ljagiello/ctf-skills`，MIT，作者 Lukasz Jagiello（2026），HEAD `aabc3ef61db9231ba7005e90b11c7b9bcde410f9`。
- 结构：分类目录 `ctf-ai-ml/ ctf-crypto/ ctf-forensics/ ctf-malware/ ctf-misc/ ctf-osint/ ctf-pwn/ ctf-reverse/ ctf-similar-search/ ctf-web/ ctf-writeup/`，每个目录一份 `SKILL.md`（frontmatter：name/description/license/compatibility/allowed-tools/metadata）+ 若干附件 `.md`。
- 另有 `solve-challenge/SKILL.md`：**派发/分诊入口** —— 第一轮 triage 判断题型（web/pwn/reverse/crypto/misc…），再路由到对应 `ctf-*` 专项技能。这是「默认调用」要注入的那一篇。
- 排除：`.git/`、`evals/`、`tests/`、仓库根 meta（README/CONTRIBUTING/…）、`scripts/`（安装脚本，不进技能库；如需单独跑工具安装再说）。

## 三、落地方式

### 1. 技能包目录

复制到 **`suvex-api/prompts/skills/ctf/<skill>/`**（目录包形态，category 由父目录名 `ctf` 得出）：

```
suvex-api/prompts/skills/ctf/
  ctf-ai-ml/  ctf-crypto/  ctf-forensics/  ctf-malware/  ctf-misc/
  ctf-osint/  ctf-pwn/  ctf-reverse/  ctf-similar-search/  ctf-web/
  ctf-writeup/  solve-challenge/
```

共 12 篇技能、179 个 .md、约 4.0MB。与既有加载器完全兼容（`app/kernel/skills.py::_scan_one_root` 认 `<category>/<name>/SKILL.md`+附件；包内其它 .md 是附件，不会被误当独立技能）。加载后 `discover_skills()` 总数 77 → **89**，`category=="ctf"` 12 篇。

### 2. CTF 方向默认调用（核心）

新增 **人格 → 默认技能** 映射 `PERSONA_DEFAULT_SKILLS`（`app/chat/prompts.py`）：

```python
PERSONA_DEFAULT_SKILLS = {
    "ctf": ("solve-challenge",),
}
```

- `build_system_prompt(persona=...)` 末尾调用新函数 `persona_default_skill_texts(persona, mounted_skills)`：
  - 只对登记过的人格生效（目前只有 `ctf`）；
  - 用户已显式挂载的同名技能不重复注入（避免正文出现两遍）；
  - 读不到技能（目录/改名）静默跳过 + warning，不抛异常。
- 注入的那篇（`solve-challenge`，约 10.2K 字符）放在**用户显式挂载之前**，让「用户自己挂的」更靠后（更具体、更优先）。
- **只注入入口路由图（solve-challenge），不注入全部 12 篇**：ctf-pwn 单篇带 70 个附件，全塞会撑爆上下文且大部分与当前题无关；具体题型手册仍由模型按需 `load_skill("ctf-web")` 等拉取（这是「清单 + 按需拉」的既定设计）。

### 3. 内核解题流程无需改动

内核 `runner.py` 的 CTF 解题（`start_ctf_solve` → `build_system_prompt_by_spec(spec)`）本就走 `kernel_system.md:80 {{SKILL_INDEX}}`（`skills_mod.render_index()`）+ `load_skill` 工具（`runner.py:4951`）。导入后这套索引**自动**包含 12 个 `ctf-*` 技能，模型在解题时即可按需 `load_skill` 拉取。故内核侧「CTF 方向可用」顺带达成，无需改其严格「关闭时逐字节相同」的 prompt 拼接逻辑。

## 四、测试

新增 `suvex-api/tests/chat/test_ctf_skills.py`（8 条）：
- 12 篇 ctf 技能被发现且 category=ctf；
- 目录包附件不被当成独立技能（ctf-pwn 有 50+ 附件，但不存在名为附件的技能）；
- solve-challenge 正文可读且含 triage/路由字样；
- persona 默认技能只对 ctf 生效（default/aillm/None 为空）；
- 用户已挂同名时不重复注入；
- CTF 人格 prompt 含 solve-challenge 正文，默认人格不含；
- 技能索引列出 ctf-* 与 solve-challenge。

修正一处既有测试假红：`tests/test_kernel_skills.py::test_real_skills_have_no_unrendered_template_syntax` 原扫全目录，会把 ctf 教学材料里的 SSTI 载荷 `{% include "/flag.txt" %}`（Go 模板注入示例，正是要教模型打的东西）误判为「未渲染 Jinja」。改为跳过 `ctf/` 子树（与「`{{7*7}}` 是 SSTI 载荷」同一条理由）。

全量 `tests/chat/`：**488 passed, 2 skipped**（480 + 8 新增）；`tests/test_kernel_skills.py` 25 passed。

## 五、部署与线上验证

- prod prompts.py sha `53f6bb0571bd0e65594da9c68452a46aa03702aa1fc7411c53ec777b93c488a3`（prod==local）；`ctf/` 子树解压进 `/root/suvex/suvex-api/prompts/skills/`；重启 health 200。
- prod 自身 venv 直接验证：`discover_skills()` 89 篇、`category=="ctf"` 12 篇、`build_system_prompt(persona="ctf")` 含 "CTF Challenge Solver"、`persona="default"` 不含。
- 线上真机（admin，session `chat_01M6RKXSW1EW38836AJ4W4XWD4`，persona 设为 `ctf`）：用户问「默认带了哪些 CTF 专项技能」，模型准确列出 12 个并说明 `solve-challenge` 是入口分诊技能；追问「入口技能叫什么」，干净回答「入口技能叫 `solve-challenge`。」（surface=answer，done）。截图 `artifacts/v171-ctf-skills.png`（已 read_image 复核，底部「CTF 解题」人格选中）确认。

## 六、诚实说明（旁支，非本次改动引入）

线上验证时观察到：这是一个**纯问答**，模型给出完整答案后，既有的 v1.45 plan 闸偶发把"完整答案 + 尾句带‘继续’字样"误判成计划句，补了一句「这一轮停在了下一步上…」并把答案折叠进「过程详情」。这与本次技能包导入无关（纯 Q&A 触发既有 looks_like_plan 判据），已记录，属后续可优化项。

## 七、变更文件

- `suvex-api/prompts/skills/ctf/**`（新增 12 篇技能，179 个 .md）
- `suvex-api/app/chat/prompts.py`（`PERSONA_DEFAULT_SKILLS` + `persona_default_skill_texts` + `build_system_prompt` 注入 + `__all__`）
- `suvex-api/tests/chat/test_ctf_skills.py`（新增 8 条）
- `suvex-api/tests/test_kernel_skills.py`（扫描跳过 ctf/ 子树）
- `artifacts/v171-ctf-skills.png`（截图）
