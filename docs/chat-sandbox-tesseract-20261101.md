# v1.42：沙箱内置 tesseract OCR + 提示词要求「缺工具自己装」

日期：2026-11-01

## 背景

用户反馈：对话里让 agent 识别图片文字时，它说 **tesseract 没装** 就放弃了。两个问题：

1. 镜像里确实**没有** tesseract（`command -v tesseract` 为空）。
2. 提示词虽然告诉模型「能出网、能装包」，但**没有一句明确的"缺工具就自己装"指令**，模
   型遇到缺失工具倾向于直接回绝，而不是 `apt-get install` 装上继续。

## 事实核查（真跑，部署前）

- 对话沙箱镜像 `suvex-sandbox:latest`：`command -v tesseract` → 空；`apt-get` 在。
- 沙箱容器以 **root（`--user 0:0`）** 运行（`app/kernel/runner.py` `_argv` 注释：「容器内
  root：`apt-get install` 需要」），且对话档 `unrestricted=True` → `--network` 走
  `AGENT_NETWORK`（`suvex-agent`，**有公网出站**）。即「能装」没有障碍。
- 体积（apt-cache 实测）：`tesseract-ocr` 416KB + `tesseract-ocr-chi-sim` 1.6MB +
  `tesseract-ocr-eng` 1.6MB + `libtesseract5` 1.4MB + 少量依赖 ≈ 下载 ~5–6MB、
  装好占盘 ~15–20MB。很小。

## 改动

### A. 镜像内置 OCR（`suvex-api/docker/suvex-sandbox/Dockerfile`）

在「取证 / misc 题」的 `apt-get install` 里，`imagemagick` 之后加：

```
tesseract-ocr tesseract-ocr-chi-sim tesseract-ocr-eng \
```

（中文图必须带 `-chi-sim`，否则中文识别成乱码。）

### B. 提示词（`suvex-api/app/chat/prompts.py`）

在「沙箱里有哪些命令行工具」一节新增「### 缺工具就自己装，别回头说「没装」」：

- 系统工具缺：`apt-get update && apt-get install -y --no-install-recommends <包名>`；
- Python 库缺：`pip install <包名>`；
- 装完再跑一次确认；
- 以 OCR 为例给出具体命令（含 `-chi-sim`），并明确「不要因为没装 tesseract 就停在这里
  让用户自己去装」；只有**确实装不上**才如实说明原因。

## 验证（真跑）

### 镜像
- `docker build -t suvex-sandbox:latest` 成功 → 新镜像 `f1618091457d`，5.03GB。
- 镜像内实测：`tesseract 5.5.0`；`tesseract --list-langs` 含 `chi_sim / eng / osd`；
  生成一张带文字图片 `tesseract img.png stdout -l eng` 能读出文字。

### 平台真实代码路径
- 用 `app.chat.sandbox.get_sandbox(SimpleNamespace(id="chat_tess_probe_demo"))` 起真实
  对话沙箱 `ctf2-chat-s_probe_demo-…`，`box.shell(...)`：
  `/usr/bin/tesseract`、`tesseract 5.5.0`、`chi_sim/eng`、`EXIT=0` → 通过后 `drop_sandbox`。

### 端到端（前端真传图）
- 在对话页新建会话、上传一张带文字图片、要求「识别图里的文字」。
- 模型**主动** `ls /work/attachments/` → `tesseract --list-langs` → 说「tesseract 已经
  在，直接跑识别（英文+中文都试一遍）」→ 跑 OCR 并交叉验证，**全程没有**"没装
  tesseract"的回绝。截图：`artifacts/ocr-verify-tesseract.png`。
- （模型对合成图里下划线较多的 flag 做了较多像素级复核，属模型严谨，非工具问题；本轮
  已中断并清理测试会话。）

### 提示词回归
- `pytest tests/test_prompt_tool_names.py tests/chat/test_plan_detection.py` → **26 passed**。
- `prompts.py` 导入正常，含新指引与 tesseract 示例。

## 部署

- `prompts.py` 部署到 Kali `/root/suvex`（备份 `.bak-20261101-v142`），prod sha
  `f4f22a871c8fd3f31f5dd612732fba4d014f5c852d4fda649af57545cd4957f1` 与本地一致；
  nsenter 重启 → `Application startup complete`、health 200。
- 镜像 `suvex-sandbox:latest` 在 Kali 本地 docker 重建，**新会话起容器自动用新镜像**，
  API 无需为此重启（prompts 改动的重启已覆盖）。

## 说明 / 边界

- 老会话如果沙箱是在旧镜像上起的，需要等它空闲回收（30 分钟）或归档后重建，才会用带
  tesseract 的新镜像；新建会话立即生效。
- 提示词让模型自己装包，等同于「模型在沙箱里能 apt/pip」——这本就是该档（root + 出网）
  的设计意图，非新增特权。真正的边界（无 docker.sock、无宿主文件、无平台 DB）不变。
- 这两条一起才构成「遇到缺工具自己解决」：内置常见工具省时间去装，提示词兜住非常见工具。
