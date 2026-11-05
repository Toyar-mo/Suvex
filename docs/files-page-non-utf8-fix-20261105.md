# v1.68.3 文件管理页「加载失败 / INTERNAL_ERROR」修复（非 UTF-8 文件名撑爆 JSON）

## 用户原话

> m02816 后续截图：「文件管理」页显示「加载失败 / 服务器内部错误，请稍后重试」
> 「INTERNAL_ERROR」。

## 复现与根因

- 用 viewer `zzviewer88` 打 `GET /api/v1/chat/files` → **200**。
- 用 admin 打同一接口 → **500**。差别只在两账号各自的会话/产物集合。
- 服务端日志拿到真异常（非业务错误）：

```
pydantic_core._pydantic_core.PydanticSerializationError: Error serializing to JSON:
UnicodeEncodeError: 'utf-8' codec can't encode characters in position 66-70:
surrogates not allowed
```

根因：admin 名下某些会话产物文件名含**非 UTF-8 字节**（CTF 题里解出来的 GBK
乱码文件名，例如 `附件-….iso`、`凯撒-+.txt`、`+….rar`）。`os.scandir`/`Path.rglob`
用 `surrogateescape` 把不可解码的字节映射成**孤立代理项**（U+DC80–U+DCFF）
塞进 Python `str`；这个 str 写 JSON 时 `encode('utf-8')` 直接抛
`surrogates not allowed` —— 整个文件列表接口 500，**一个坏文件名拖垮整页**。

Kali 实测定位到 6 个这样的文件（`/root/suvex/chat-workspaces/...` 下）。

## 修法

`suvex-api/app/chat/workspaces.py`：

1. 新增 `_safe_display(value)`：`value.encode("utf-8","surrogateescape").decode("utf-8","replace")`
   —— 代理项还原成原始字节再替换解码，得到带 `�`(U+FFFD) 的合法字符串，可安全 JSON。
2. `list_files()` 里 `path`、`name` 都过 `_safe_display()`。
3. `resolve_file()` 增加**回退**：直接 `joinpath` 对不上（前端把列表里的显示名
   `…�….iso` 原样带回来下载/删除时）→ 在 `list_files()` 结果里按 `item.path == raw`
   反查真实路径并返回。否则这类文件在界面上点下载/删除会 404。
   回退分支同样做工作区边界 + 普通文件 + 大小上限校验，不放宽安全边界。

> 说明：`_safe_display` 只影响**展示**（列表/下载/删除的入参匹配）；磁盘上文件的
> 真实字节不受影响，下载出来的仍是原文件。

## 测试

`suvex-api/tests/chat/test_workspaces.py` 新增 2 条：
- `test_list_files_sanitizes_non_utf8_filenames`：`_safe_display` 对含孤立代理项的
  字符串产出可 `json.dumps` 的结果、无残留代理项、含替换符 `�`。
- `test_resolve_file_matches_sanitized_display_path`（Linux-only，`skipif(os.name=='nt')`
  —— Windows 无法创建这种文件名）：真实坏字节文件名，用列表返回的显示名
  `resolve_file` 能反查到真实文件、内容一致。

`tests/chat/test_workspaces.py + test_files_router.py` → **40 passed, 2 skipped**
（2 skip = 上述 Windows-only + 1 条预存平台 skip）。

## 线上验证（决定性）

`workspaces.py` sha256 `91737b27…7182c5` 部署到 Kali prod
（`/root/suvex/suvex-api/app/chat/workspaces.py`，prod sha 与本地逐字节一致），
重启 PID 2870278 `Application startup complete`，health 200。

- `GET /api/v1/chat/files`（admin）→ **HTTP 200**（原 500），`count=5000`（命中
  `MAX_LISTED_FILES=5000` 上限），含 `�` 替换符（即原来会炸的那批坏名文件）。
- 浏览器打开 `/files`：页面「共 5000 个文件」正常渲染、表格各列齐全、无
  「加载失败 / INTERNAL_ERROR」。截图 `artifacts/v1683-files-loaded.png`（已 read_image 复核）。

## 诚实说明

- 5000 条是 `MAX_LISTED_FILES` 上限所致（admin 名下产物极多），非本次引入；
  分页/截断是既有设计，未改。
- 纯后端修复，不涉及前端构建/部署。
