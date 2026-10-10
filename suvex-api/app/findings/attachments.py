"""漏洞附件的落盘与元信息（任务 10 P6-d）。

## 这个模块只做三件事

上传、删除、读回。它**不做**鉴权与业务校验 —— 那些在 service/router 层，
因为附件本身不理解"谁能看这条漏洞"。

## ★ 路径安全：绝不用上传的文件名拼路径

这条在前面 `FindingAttachment` 的 docstring 里写过一遍，这里给出实现，
因为**它是这个模块存在的唯一理由**：

    stored_path = <root>/<finding_id>/<attachment_id><ext>
    filename    = 用户上传时的原名（只显示）

两个洞一起关掉：路径穿越（`../../etc/passwd`）与互相覆写
（两个 `screenshot.png`）。见 `_safe_extension` —— 连扩展名都不直接用
用户给的，而是先**消毒**再拼。

## 为什么扩展名要消毒而不是只用 id

下载时浏览器靠扩展名决定用什么打开，而附件里最可能是截图/PDF。
全用 id（无扩展名）会让下载下来的文件没有类型提示，用户得手工改
才知道是什么。所以保留扩展名 —— 但只保留"看起来正常的那一小段"。
"""

from __future__ import annotations

import logging
import re
import shutil
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.errors import ApiError
from app.core.ids import new_id
from app.core.timeutil import now_utc
from app.findings.models import FindingAttachment

logger = logging.getLogger("suvex.findings")

# 允许的扩展名白名单。**空扩展名也允许**（有些导出文件没有扩展名）。
#
# ★ 用白名单不用黑名单：黑名单永远漏（`.php5`、`.phtml`、`.svgz`…），
#   而这里的目标不是"拦住危险文件"（附件不会被服务端执行），
#   而是"确保下载时类型可信"。白名单让代价可预测。
ALLOWED_EXTENSIONS = frozenset(
    {
        ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg",
        ".pdf", ".txt", ".log", ".json", ".xml", ".csv", ".md",
        ".pcap", ".har", ".zip", ".html", ".http",
    }
)
# 允许的 MIME 前缀。同样用白名单。
ALLOWED_CONTENT_PREFIXES = ("image/", "text/", "application/pdf",
                            "application/json", "application/xml",
                            "application/zip", "application/octet-stream",
                            "application/x-pcap")

_EXT_PATTERN = re.compile(r"^\.[A-Za-z0-9]{1,10}$")


def _safe_extension(filename: str) -> str:
    """从用户给的文件名里取一个**安全的**扩展名（含点号），取不到就返回空串。

    只认"点号 + 1–10 位字母数字"这一种形态。这样：
      · `a.png`        → `.png`
      · `a.PNG`        → `.png`（统一小写，避免同类型文件在
                        大小写不敏感的文件系统上撞名）
      · `a.png/../../x`→ 取不到（含斜杠，正则不匹配）→ 空串
      · `a.php%00.png` → `%` 不在允许字符里 → 空串
      · `.bashrc`      → 取到 `.bashrc`? 不 —— `bashrc` 是 1–10 位，
                         所以会得到 `.bashrc`。但它在白名单外，
                         所以下面还会被过滤成空串。

    ⚠️ 结果**还要过一遍白名单**（在 `store` 里做）—— 正则只保证
    "形态安全"，不保证"是我们认识的文件类型"。
    """
    candidate = filename.strip().lower()
    # 只看最后一段：`dir/a.png` 与 `a.png` 都取 `.png`
    candidate = candidate.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    dot = candidate.rfind(".")
    if dot <= 0:  # 没点号，或点号在开头（`.bashrc` 这种隐藏文件）
        return ""
    ext = candidate[dot:]
    if not _EXT_PATTERN.match(ext):
        return ""
    return ext if ext in ALLOWED_EXTENSIONS else ""


def _sanitize_filename(raw: str) -> str:
    """给用户看的名字：去掉路径分隔符与控制字符，限长。

    ⚠️ 它**不参与**路径构造（那是 id 的事），所以这里只需要
    "显示出来不奇怪、下载时不会让浏览器误解"。
    但控制字符必须去掉：文件名里的 `\\r\\n` 会注入到
    `Content-Disposition` 响应头里，**那是响应头注入**。
    """
    name = (raw or "").strip()
    name = name.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    # 去掉控制字符（含 \r \n \0）与引号 —— 后者会破坏
    # `Content-Disposition: attachment; filename="..."` 的结构
    name = re.sub(r'[\x00-\x1f\x7f"\\]', "", name)
    name = name.strip(". ")  # Windows 不允许结尾是点或空格
    return name[:255] or "未命名附件"


@dataclass(frozen=True, slots=True)
class StoredFile:
    relative_path: str
    size_bytes: int


def attachment_dir(finding_id: str):
    """某条漏洞的附件目录（绝对路径）。"""
    return get_settings().attachments_dir / finding_id


def absolute_path(attachment: FindingAttachment):
    """附件的绝对路径。

    ⚠️ 用 `stored_path`（服务端生成的相对路径）拼，且**拼完要验**
    它确实落在附件根之内 —— 见 `resolve_stored_path`。
    """
    return get_settings().attachments_dir / attachment.stored_path


def resolve_stored_path(stored_path: str):
    """把数据库里的相对路径解析成绝对路径，并**验证它没跑出根目录**。

    ★ 最后这道校验看起来多余（`stored_path` 是服务端自己写的），
    但它防的是**数据库被别的方式改过**（手工 update、迁移写错、
    将来另一个写入点引入了用户输入）。纵深防御的代价是一行解析，
    而漏掉它的代价是任意文件读取 —— 通过 `GET /attachments/{aid}/content`
    就能把 `SUPEX_SECRET_KEY` 读走。

    用 `resolve()` 之后再比前缀，而不是比字符串：符号链接
    与 `..` 都会被解析掉，字符串比较挡不住它们。
    """
    root = get_settings().attachments_dir.resolve()
    target = (root / stored_path).resolve()
    # `is_relative_to` 是 3.9+；不用字符串 startswith —— 那会把
    # `/data/uploads-evil` 误判成在 `/data/uploads` 之内。
    if not target.is_relative_to(root):
        logger.error("附件路径越界，已拒绝：%s", stored_path)
        raise ApiError.not_found("附件")
    return target


def store(
    db: Session,
    *,
    finding_id: str,
    filename: str,
    content: bytes,
    content_type: str = "",
    uploaded_by: str | None = None,
) -> FindingAttachment:
    """写入一个附件（文件 + 元信息行）。

    ## 顺序：**先写文件、后写库**

    反过来（先 commit 行、再写文件）的话，写文件失败会留下一条
    "指向不存在文件"的记录 —— 用户点下载得到 500，而列表还说它有两个附件。
    先写文件则最坏情况是一个**孤儿文件**：不占用户可见的位置，
    也不影响任何功能。所以宁可留孤儿文件。

    ## 上限在这里兜底

    路由层已经按 `Content-Length` 挡了一道，但那可以是假的
    （分块传输、或者客户端撒谎）。这里是**按真实字节数**判 ——
    真正的闸门在这。
    """
    settings = get_settings()
    if len(content) > settings.attachment_max_bytes:
        raise ApiError.validation(
            f"附件超过大小上限（{settings.attachment_max_bytes // (1024 * 1024)} MB）",
            {"file": f"实际 {len(content) / (1024 * 1024):.1f} MB"},
        )
    if content_type and not content_type.startswith(ALLOWED_CONTENT_PREFIXES):
        raise ApiError.validation(
            f"不支持的文件类型：{content_type}",
            {"file": "请上传图片、PDF、文本或压缩包"},
        )

    attachment_id = new_id("att")
    ext = _safe_extension(filename)
    # ★ 路径完全由 id 生成 —— 与用户给的文件名无关
    relative = f"{finding_id}/{attachment_id}{ext}"

    directory = attachment_dir(finding_id)
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"{attachment_id}{ext}"
    target.write_bytes(content)

    row = FindingAttachment(
        id=attachment_id,
        finding_id=finding_id,
        filename=_sanitize_filename(filename),
        content_type=content_type or _guess_content_type(ext),
        size_bytes=len(content),
        stored_path=relative,
        uploaded_by=uploaded_by,
        uploaded_at=now_utc(),
    )
    try:
        db.add(row)
        db.commit()
        db.refresh(row)
    except Exception:
        # 库写失败 → 把刚写的文件删掉，别留孤儿
        target.unlink(missing_ok=True)
        db.rollback()
        raise
    return row


def _guess_content_type(ext: str) -> str:
    """扩展名 → MIME。只为"下载时的类型提示"，不是安全边界。"""
    table = {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".gif": "image/gif",
        ".webp": "image/webp",
        ".svg": "image/svg+xml",
        ".pdf": "application/pdf",
        ".json": "application/json",
        ".xml": "application/xml",
        ".csv": "text/csv",
        ".txt": "text/plain",
        ".log": "text/plain",
        ".md": "text/markdown",
        ".zip": "application/zip",
        ".har": "application/json",
        ".pcap": "application/x-pcap",
    }
    return table.get(ext, "application/octet-stream")


def list_for(db: Session, finding_id: str) -> list[FindingAttachment]:
    """一条漏洞的附件，**按上传时间正序**（先传的在前）。

    正序而不是倒序：附件通常是按处置顺序传的（截图 → 复测报告），
    倒序会把阅读顺序反过来。这与时间线的"最新在前"不同 ——
    时间线看的是"最近发生了什么"，附件看的是"证据的完整序列"。
    """
    return list(
        db.execute(
            select(FindingAttachment)
            .where(FindingAttachment.finding_id == finding_id)
            .order_by(FindingAttachment.uploaded_at.asc(), FindingAttachment.id)
        ).scalars()
    )


def get_attachment(db: Session, finding_id: str, attachment_id: str) -> FindingAttachment:
    row = db.get(FindingAttachment, attachment_id)
    # ⚠️ 也要校验它属于这条漏洞：否则 `GET /findings/A/attachments/<B的附件>`
    #    会成功 —— 那是一条"用 A 的权限读 B 的附件"的越权路径
    #    （现在只有一个管理员，但接口的形状不该依赖这一点）。
    if row is None or row.finding_id != finding_id:
        raise ApiError.not_found("附件")
    return row


def delete_attachment(db: Session, finding_id: str, attachment_id: str) -> None:
    """删除一个附件（**先删库行、后删文件**）。

    ## 顺序与 `store` 相反，理由也相反

    `store` 是先文件后行（避免"行指向不存在的文件"）。
    删除时若也先删文件、后删行，中途失败会留下"行还在、文件没了" ——
    用户看到 3 个附件、点第 3 个得到 404。反过来（先行后文件）
    最坏留个孤儿文件，用户可见的状态是**一致的**。

    所以两个方向的规则是同一条：**让用户可见的状态保持一致，
    宁可留孤儿文件**。
    """
    row = get_attachment(db, finding_id, attachment_id)
    path = absolute_path(row)
    db.delete(row)
    db.commit()
    try:
        path.unlink(missing_ok=True)
    except OSError:
        # 文件删不掉（权限、被占用）不该让请求失败 —— 库行已经删了，
        # 用户看到的就是"它没了"。留个警告供排查孤儿文件。
        logger.warning("附件文件删除失败（库行已删，磁盘可能残留）：%s", path, exc_info=True)


def delete_all_for_finding(db: Session, finding_id: str) -> None:
    """删掉一条漏洞的**全部**附件（文件 + 行）。永久删除漏洞时用。

    `ON DELETE CASCADE` 会替我们删库行，但**不会删文件** ——
    所以必须在删除漏洞之前显式清一遍，否则磁盘上会永久留下
    一批谁也引用不到的文件（而它们占的是真实空间）。
    """
    rows = list_for(db, finding_id)
    for row in rows:
        try:
            absolute_path(row).unlink(missing_ok=True)
        except OSError:
            logger.warning("附件文件删除失败：%s", row.stored_path, exc_info=True)
        db.delete(row)
    # 目录本身也清掉（一个空的 finding 目录没有意义）
    shutil.rmtree(attachment_dir(finding_id), ignore_errors=True)


__all__ = [
    "ALLOWED_EXTENSIONS",
    "StoredFile",
    "absolute_path",
    "attachment_dir",
    "delete_all_for_finding",
    "delete_attachment",
    "get_attachment",
    "list_for",
    "resolve_stored_path",
    "store",
]
