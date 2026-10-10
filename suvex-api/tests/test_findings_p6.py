"""任务 10 P6：漏洞软删除、CSV 导出、统计跟随筛选、附件。

四项各占一节。写这个文件时的两个判断值得记下来：

## 一、统计跟随筛选为什么要测"facet 口径"

这一项最容易做错的地方不是"数字算得准不准"，而是**筛选器还能不能用**。
用户筛了「高危」之后，如果「中危」的角标变成 0 且按钮被禁用，
他就切不过去了 —— 必须先取消高危。所以真正要验的是那条规则本身。

## 二、附件为什么要测路径安全

`attachments.py` 的路径**完全由服务端从 id 生成**，所以"路径穿越"
看起来不可能发生。但那正是要测它的理由：这条保证是**结构性的**，
而结构性保证最怕的是将来有人为了"保留原始文件名"把 `_safe_extension`
换成一个直接拼用户输入的版本 —— 那时不会有任何现有测试失败。
这里把那个洞先堵上。
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.findings import attachments as attachments_mod
from app.findings import service
from app.findings.models import Finding, FindingAttachment

from tests.conftest import vuln_record, write_vulns  # noqa: F401
from tests.factories import make_scan

API = "/api/v1"
AUTH_STATEMENT = "我有权对此目标进行安全测试"


# ══════════════════════════════════════════════════════════
# P6-a 软删除
# ══════════════════════════════════════════════════════════


def _make_findings(db: Session, scan, specs: list[tuple[str, str]]) -> list[Finding]:
    """直接落库 `(severity, status)` 指定的漏洞，返回它们（按传入顺序）。

    不走导入器：这些测试要的是**确定的输入**，而导入器有自己的
    幂等与合并逻辑（那些在 `test_findings.py` 里各自被测过）。
    """
    import uuid

    from app.core.timeutil import now_utc

    rows = []
    for severity, status in specs:
        rows.append(
            Finding(
                id=f"finding_{uuid.uuid4().hex[:12]}",
                scan_id=scan.id,
                severity=severity,
                category="injection",
                title=f"{severity} 问题",
                target="api.example.com",
                verification="verified",
                status=status,
                discovered_at=now_utc(),
            )
        )
    db.add_all(rows)
    db.commit()
    for row in rows:
        db.refresh(row)
    return rows


def test_soft_delete_hides_from_list_but_keeps_the_row(
    db_session: Session, scan
) -> None:
    """★ 软删除后列表里没有它，但**行还在**。

    这是"误删不会立刻丢数据"的全部实现。断言两件事缺一不可：
    列表看不到（否则删除没生效）+ 行还在（否则那是物理删除，
    而回收站就无从谈起了）。
    """
    rows = _make_findings(db_session, scan, [("high", "confirmed")])
    target = rows[0]

    changed, skipped = service.batch_set_deleted(db_session, ids=[target.id])

    assert len(changed) == 1
    assert skipped == 0
    assert target.deleted_at is not None, "行必须还在，只是打了标记"

    visible = service.list_findings(db_session)
    assert target.id not in [f.id for f in visible], "默认列表不该看到它"

    trashed = service.list_findings(db_session, deleted=True)
    assert [f.id for f in trashed] == [target.id], "回收站里能看到"


def test_soft_delete_is_idempotent(db_session: Session, scan) -> None:
    """已是回收站里的条目算 `skipped`，不重复改时间戳。

    ⚠️ 与状态流转的幂等是同一条规则（契约 §2.3）：重复操作要
    返回成功但 `changed: 0`。不这样做的话，用户在删除按钮上
    点两下，"已删除 1 项"会变成"已删除 2 项"，而实际只删了一条。
    """
    rows = _make_findings(db_session, scan, [("high", "confirmed")])
    target = rows[0]

    service.batch_set_deleted(db_session, ids=[target.id])
    first_stamp = target.deleted_at

    changed, skipped = service.batch_set_deleted(db_session, ids=[target.id])

    assert changed == []
    assert skipped == 1
    db_session.expire_all()
    assert db_session.get(Finding, target.id).deleted_at == first_stamp


def test_restore_does_not_touch_status(db_session: Session, scan) -> None:
    """★ 恢复只把记录放回来，**不动处置状态**。

    漏洞在回收站里期间状态没有变过（谁也不会去改一条已删的），
    所以恢复要还原成删除前的样子。顺手改成 `new` 会抹掉分诊成果，
    而那正是这个模块最值钱的东西。
    """
    rows = _make_findings(db_session, scan, [("high", "false_positive")])
    target = rows[0]

    service.batch_set_deleted(db_session, ids=[target.id])
    changed, skipped = service.batch_restore(db_session, ids=[target.id])

    assert len(changed) == 1
    assert skipped == 0
    db_session.expire_all()
    again = db_session.get(Finding, target.id)
    assert again.deleted_at is None
    assert again.status == "false_positive", "恢复不该把状态重置成 new"


def test_trashed_findings_are_excluded_from_stats(db_session: Session, scan) -> None:
    """★ 删掉的条目**不再进统计**。

    这是软删除最容易漏的一处：只让列表看不到、统计还数着它们，
    于是"列表 2 条、角标 3 条"。用户无法解释这个差值，
    接着就不再相信角标 —— 那这个功能就白做了。
    """
    rows = _make_findings(
        db_session, scan, [("high", "confirmed"), ("medium", "new"), ("low", "new")]
    )
    assert service.stats(db_session).total == 3

    service.batch_set_deleted(db_session, ids=[rows[0].id])

    assert service.stats(db_session).total == 2, "回收站里的不该计入"
    assert service.stats(db_session, deleted=True).total == 1, "回收站视图只数那一条"


def test_purge_requires_trash_first(db_session: Session, scan) -> None:
    """★ 永久删除必须先经过回收站（两道闸）。

    合成一步等于把 purge 变成一个不经过回收站的 `rm -rf` ——
    而那个按钮与"删除"长得几乎一样，点错就没有回头路。
    """
    import pytest

    from app.core.errors import ApiError

    rows = _make_findings(db_session, scan, [("high", "confirmed")])

    with pytest.raises(ApiError) as excinfo:
        service.purge_finding(db_session, rows[0].id)

    assert excinfo.value.code == "FINDING_NOT_IN_TRASH"
    assert db_session.get(Finding, rows[0].id) is not None, "拒绝时不能已经删了"


def test_purge_is_refused_when_a_report_references_the_finding(
    db_session: Session, scan, ready_client: TestClient
) -> None:
    """★ 被报告引用时拒绝永久删除，并说出是哪几份报告。

    真实破坏路径：`report_findings.finding_id → findings ON DELETE CASCADE`。
    物理删除会连带删掉报告与它的关联行 —— 报告里那一格被**掏空**，
    而报告是交付物（有编号、客户拿去对账），悄悄少一条不可接受。
    """
    import pytest

    from app.core.errors import ApiError

    rows = _make_findings(db_session, scan, [("high", "confirmed")])
    target = rows[0]
    service.batch_set_deleted(db_session, ids=[target.id])

    # 造一份引用了这条漏洞的报告
    created = ready_client.post(
        f"{API}/reports",
        json={
            "title": "P6 引用测试",
            "scope": {"kind": "findings", "findingIds": [target.id]},
            "options": {},
            "format": "markdown",
        },
    )
    assert created.status_code == 201, created.text
    code = created.json()["code"]

    with pytest.raises(ApiError) as excinfo:
        service.purge_finding(db_session, target.id)

    assert excinfo.value.code == "FINDING_REFERENCED_BY_REPORT"
    assert code in excinfo.value.message, "要说清是哪份报告挡住了"
    assert db_session.get(Finding, target.id) is not None


def test_purge_removes_the_row_and_its_attachments(
    db_session: Session, scan
) -> None:
    """★ 永久删除要把附件一起清掉。

    `ON DELETE CASCADE` 只删数据库行、**不会删磁盘上的文件**。
    不显式清一遍的话，磁盘上会永久留下一批谁也引用不到的文件 ——
    而它们占的是真实空间。
    """
    rows = _make_findings(db_session, scan, [("high", "confirmed")])
    target = rows[0]

    row = attachments_mod.store(
        db_session,
        finding_id=target.id,
        filename="proof.png",
        content=b"PNGDATA",
        content_type="image/png",
    )
    path = attachments_mod.absolute_path(row)
    assert path.exists()

    service.batch_set_deleted(db_session, ids=[target.id])
    service.purge_finding(db_session, target.id)

    assert db_session.get(Finding, target.id) is None
    assert not path.exists(), "磁盘上的文件也必须被清掉"
    assert db_session.execute(select(FindingAttachment)).scalars().all() == []


def test_delete_endpoint_writes_an_audit_row(
    ready_client: TestClient, db_session: Session, scan
) -> None:
    """软删除要留痕（`finding.delete`）。

    它把条目从默认视图里移走，事后必须能回答"那条漏洞怎么不见了" ——
    与 `scan.archive` 同一条理由。
    """
    rows = _make_findings(db_session, scan, [("high", "confirmed")])

    response = ready_client.post(
        f"{API}/findings/batch/delete", json={"ids": [rows[0].id]}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["changed"] == 1
    assert body["skipped"] == 0

    audit = ready_client.get(f"{API}/audit?limit=5")
    assert audit.status_code == 200, audit.text
    # ⚠️ 响应字段是 `entries`（不是 `items`）—— 契约 §3.9 的形状
    actions = [row["action"] for row in audit.json()["entries"]]
    assert "finding.delete" in actions


def test_restore_endpoint_round_trips(
    ready_client: TestClient, db_session: Session, scan
) -> None:
    """删除 → 恢复走 HTTP 一趟，两条审计动作都在。"""
    rows = _make_findings(db_session, scan, [("high", "confirmed")])
    target = rows[0]

    assert (
        ready_client.post(f"{API}/findings/batch/delete", json={"ids": [target.id]})
        .status_code
        == 200
    )
    response = ready_client.post(f"{API}/findings/batch/restore", json={"ids": [target.id]})
    assert response.status_code == 200, response.text
    assert response.json()["changed"] == 1

    db_session.expire_all()
    assert db_session.get(Finding, target.id).deleted_at is None


# ══════════════════════════════════════════════════════════
# P6-b 统计跟随筛选
# ══════════════════════════════════════════════════════════


def test_stats_follows_the_same_filters_as_the_list(
    db_session: Session, scan
) -> None:
    """★ 同一套筛选下，`total` 必须等于列表长度。

    这是"统计跟着筛选走"的字面要求。两边各写一遍筛选逻辑时
    这个等式会静默失效 —— 所以现在它们编译条件是**同一个函数**。
    """
    _make_findings(
        db_session,
        scan,
        [
            ("high", "confirmed"),
            ("high", "new"),
            ("medium", "confirmed"),
            ("low", "false_positive"),
        ],
    )

    for kwargs in (
        {"severity": "high"},
        {"status": "confirmed"},
        {"severity": "high", "status": "confirmed"},
        {"q": "high"},
    ):
        listed = service.list_findings(db_session, **kwargs)
        counted = service.stats(db_session, **kwargs)
        assert counted.total == len(listed), (
            f"筛选 {kwargs}：列表 {len(listed)} 条、统计 {counted.total} 条"
        )


def test_stats_excludes_trashed_by_default_even_with_filters(
    db_session: Session, scan
) -> None:
    """带筛选时也不能把回收站里的数进去。"""
    rows = _make_findings(
        db_session, scan, [("high", "confirmed"), ("high", "confirmed")]
    )
    service.batch_set_deleted(db_session, ids=[rows[0].id])

    assert service.stats(db_session, severity="high").total == 1


def test_facets_keep_other_options_clickable(db_session: Session, scan) -> None:
    """★ facet 口径：筛了「高危」之后，「中危」的角标不能变成 0。

    这是本功能最要紧的一条。若中危角标是 0 且按钮被禁用，
    用户**根本切不过去** —— 必须先取消高危再选中危。
    这不是"数字准不准"，而是筛选器还能不能用。
    """
    _make_findings(
        db_session, scan, [("high", "confirmed"), ("medium", "new"), ("low", "new")]
    )

    # 关掉 facets：中危确实是 0（当前筛选结果里没有它）
    plain = service.stats(db_session, severity="high")
    assert plain.bySeverity.medium is None, "非 facet 口径下 0 的档键不存在"

    # 打开 facets：中危仍显示它的真实数量
    faceted = service.stats(db_session, severity="high", facets=True)
    assert faceted.bySeverity.medium == 1, "facet 口径下必须还能看到中危有多少"
    assert faceted.bySeverity.low == 1
    assert faceted.bySeverity.high == 1
    # 但 `total` 仍是"筛出来几条" —— facet 只影响角标，不影响结果集
    assert faceted.total == 1, "total 必须还是当前筛选的结果数"


def test_facets_still_respect_other_dimensions(db_session: Session, scan) -> None:
    """★ facet 只忽略**自己那一维**，其余条件照旧生效。

    搜索"严重"之后，角标就该只统计命中的那些 —— 否则角标会指着一批
    被搜索排除掉的条目，用户点进去发现数量对不上。
    """
    _make_findings(
        db_session,
        scan,
        [("high", "confirmed")],  # title = "high 问题"
    )
    other_scan = make_scan(db_session, status="completed", target="other.example.com")
    _make_findings(db_session, other_scan, [("medium", "new")])

    # 限定 scan 之后，facet 不该把别的扫描的条目算进来
    faceted = service.stats(db_session, scan_id=scan.id, facets=True)
    assert faceted.bySeverity.medium is None, "别的扫描的条目不该出现在 facet 里"
    assert faceted.total == 1


def test_stats_endpoint_accepts_the_filter_params(
    ready_client: TestClient, db_session: Session, scan
) -> None:
    """HTTP 层：`/findings/stats` 与 `/findings` 收同一套参数。"""
    _make_findings(
        db_session, scan, [("high", "confirmed"), ("medium", "new")]
    )

    listed = ready_client.get(f"{API}/findings?severity=high")
    assert listed.status_code == 200, listed.text

    stats = ready_client.get(f"{API}/findings/stats?severity=high")
    assert stats.status_code == 200, stats.text
    assert stats.json()["total"] == len(listed.json())

    faceted = ready_client.get(f"{API}/findings/stats?severity=high&facets=true")
    assert faceted.status_code == 200, faceted.text
    assert faceted.json()["bySeverity"]["medium"] == 1


def test_overview_metrics_unchanged_by_the_refactor(db_session: Session, scan) -> None:
    """★ 重构不能让 Overview 的口径变掉。

    `overview_metrics` 内部调 `stats()`，而它**不传**筛选参数 ——
    所以那些图表拿到的仍是全量口径。这条测试盯的就是这一点：
    给 `stats` 加了一堆可选参数之后，Overview 那边必须一个都不受影响。
    """
    _make_findings(
        db_session,
        scan,
        [
            ("critical", "confirmed"),
            ("high", "confirmed"),
            ("high", "false_positive"),
            ("low", "new"),
        ],
    )

    metrics = service.overview_metrics(db_session)
    # 四个档位都在（含误报那条），只是指标卡会排除误报。
    # ⚠️ 键名是 `severityCounts`（不是 `bySeverity`）—— 那是 Overview
    # 契约 §3.6 的字段名，与 `/findings/stats` 的 `bySeverity` 不同名。
    assert metrics["severityCounts"].critical == 1
    assert metrics["severityCounts"].high == 2
    assert metrics["falsePositiveCount"] == 1


# ══════════════════════════════════════════════════════════
# P6-c CSV 导出
# ══════════════════════════════════════════════════════════


def test_export_respects_the_filters(ready_client: TestClient, db_session: Session, scan) -> None:
    """★ 导出必须跟随筛选。

    这是这个端点最容易被做错的地方：用户在界面上筛出 3 条高危、
    点导出却拿到全部 200 条，而他会把那份表**发出去**。
    错误的数据比没有数据更糟。
    """
    _make_findings(
        db_session,
        scan,
        [("high", "confirmed"), ("high", "new"), ("low", "new")],
    )

    response = ready_client.get(f"{API}/findings/export?severity=high")

    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/csv")
    text = response.content.decode("utf-8-sig")
    body_lines = [line for line in text.strip().splitlines() if line]
    # 1 行表头 + 2 行高危
    assert len(body_lines) == 3, f"导出条数不对：\n{text}"
    assert "low 问题" not in text, "不该带出被筛掉的低危"


def test_export_has_bom_for_excel(db_session: Session, scan) -> None:
    """★ 带 UTF-8 BOM。

    不带的话，简体中文 Windows 上的 Excel 会按本地编码读，
    中文列名变成乱码 —— 而"导出给别人看"正是这个功能的用途，
    打开是乱码等于没做。
    """
    _make_findings(db_session, scan, [("high", "confirmed")])
    rows = service.list_findings(db_session)
    body = service.export_csv(db_session, rows=rows)

    assert body.startswith("\ufeff"), "缺 BOM，Excel 打开会乱码"
    # 列名是**英文**（契约 §3.4 的字段名），值也是枚举 —— 见
    # `service.export_csv` 里"为什么不新造一份中文标签表"的说明。
    assert body.splitlines()[0].lstrip("\ufeff").startswith("id,severity,title")


def test_export_escapes_commas_and_quotes(db_session: Session, scan) -> None:
    """含逗号 / 引号 / 换行的值必须被正确转义。

    标题里带逗号是常见的事（"SQL 注入，可读取任意文件"）。
    不转义的话那一行会**多出一列**，而 Excel 里表现为整张表错位。
    """
    from app.core.timeutil import now_utc

    row = Finding(
        id="finding_csv_escape",
        scan_id=scan.id,
        severity="high",
        category="injection",
        title='注入,可读文件 "含引号"\n还有换行',
        target="api.example.com",
        verification="verified",
        status="confirmed",
        discovered_at=now_utc(),
    )
    db_session.add(row)
    db_session.commit()

    body = service.export_csv(db_session, rows=[row])
    assert '""' in body, "内部引号要翻倍"
    # 那一条记录被完整包在一个带引号的字段里
    assert '"注入,可读文件 ""含引号""' in body


def test_export_filename_is_ascii(ready_client: TestClient, db_session: Session, scan) -> None:
    """`Content-Disposition` 的文件名必须是 ASCII。

    中文文件名直接放进 `filename="..."` 时某些客户端会乱码或截断
    （正解是 RFC 5987 的 `filename*=UTF-8''...`，但这里不值得为
    文件名引入编码复杂度）。
    """
    _make_findings(db_session, scan, [("high", "confirmed")])

    response = ready_client.get(f"{API}/findings/export")
    disposition = response.headers["content-disposition"]

    assert "suvex-findings-" in disposition
    assert disposition.isascii(), f"文件名含非 ASCII：{disposition}"


def test_export_refuses_to_route_as_a_finding_id(
    ready_client: TestClient, db_session: Session, scan
) -> None:
    """★ 路由顺序：`/findings/export` 必须在 `/{findingId}` 之前。

    声明反了的话，`export` 会被当成一个 finding id，症状是
    「导出接口 404 说找不到叫 export 的漏洞」—— 看起来像数据不存在，
    实际是声明顺序问题。这个坑在本项目已经踩过三次
    （见 `router.py` 顶部与 `/stats`、`/groups` 的注释）。
    """
    _make_findings(db_session, scan, [("high", "confirmed")])
    response = ready_client.get(f"{API}/findings/export")
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/csv")


# ══════════════════════════════════════════════════════════
# P6-d 附件
# ══════════════════════════════════════════════════════════


def test_upload_and_download_round_trip(
    ready_client: TestClient, db_session: Session, scan
) -> None:
    """上传 → 列表 → 下载，内容一致。"""
    rows = _make_findings(db_session, scan, [("high", "confirmed")])
    target = rows[0]

    upload = ready_client.post(
        f"{API}/findings/{target.id}/attachments",
        files={"files": ("proof.png", b"\x89PNG\r\n\x1a\nDATA", "image/png")},
    )
    assert upload.status_code == 201, upload.text
    created = upload.json()
    assert len(created) == 1
    assert created[0]["filename"] == "proof.png"
    assert created[0]["sizeBytes"] == 12

    # ⚠️ 响应里**不该有**存储路径 —— 那是服务端内部的事
    assert "storedPath" not in created[0], "存储路径不该暴露给客户端"
    assert "stored_path" not in created[0]

    listed = ready_client.get(f"{API}/findings/{target.id}/attachments")
    assert listed.status_code == 200
    assert len(listed.json()) == 1

    download = ready_client.get(
        f"{API}/findings/{target.id}/attachments/{created[0]['id']}/content"
    )
    assert download.status_code == 200, download.text
    assert download.content == b"\x89PNG\r\n\x1a\nDATA"
    assert download.headers["content-type"].startswith("image/png")


def test_attachment_path_ignores_the_uploaded_filename(
    db_session: Session, scan
) -> None:
    """★★ 存储路径**完全由 id 生成**，与上传的文件名无关。

    这是这一项最要紧的安全保证。用上传文件名拼路径会同时打开两个洞：
    路径穿越 + 互相覆写。这条测试直接盯住那个保证 ——
    将来若有人为了"保留原始文件名"改成直接拼用户输入，它会失败。
    """
    rows = _make_findings(db_session, scan, [("high", "confirmed")])
    target = rows[0]

    # 一个恶意文件名：试图穿越到附件根之外
    row = attachments_mod.store(
        db_session,
        finding_id=target.id,
        filename="../../../../etc/passwd.png",
        content=b"x",
        content_type="image/png",
    )

    assert ".." not in row.stored_path, "路径里不能有 .."
    assert "/etc/" not in row.stored_path
    assert row.stored_path.startswith(f"{target.id}/"), "路径必须落在该漏洞的目录下"
    # 真实路径确实在附件根之内
    resolved = attachments_mod.resolve_stored_path(row.stored_path)
    from app.core.config import get_settings

    assert resolved.is_relative_to(get_settings().attachments_dir.resolve())

    # 而给用户看的名字被消毒成"只剩文件名本身"
    assert row.filename == "passwd.png", "显示名去掉路径成分"
    # 扩展名被保留（下载时浏览器才知道用什么打开）
    assert resolved.suffix == ".png"


def test_two_files_with_the_same_name_do_not_overwrite(
    db_session: Session, scan
) -> None:
    """★ 两个人传同名的 `screenshot.png`，不能互相覆盖。

    "证据被另一个人的截图替换了"是不可接受的 —— 而用文件名当存储名
    必然会导致这件事。
    """
    rows = _make_findings(db_session, scan, [("high", "confirmed")])
    target = rows[0]

    first = attachments_mod.store(
        db_session, finding_id=target.id, filename="screenshot.png",
        content=b"FIRST", content_type="image/png",
    )
    second = attachments_mod.store(
        db_session, finding_id=target.id, filename="screenshot.png",
        content=b"SECOND", content_type="image/png",
    )

    assert first.stored_path != second.stored_path
    assert attachments_mod.absolute_path(first).read_bytes() == b"FIRST"
    assert attachments_mod.absolute_path(second).read_bytes() == b"SECOND"


def test_unsafe_extension_is_dropped(db_session: Session, scan) -> None:
    """白名单之外的扩展名被丢掉，而不是原样拼进路径。

    用白名单不用黑名单：黑名单永远漏（`.php5`、`.phtml`…），
    而这里的目标不是"拦住危险文件"（附件不会被服务端执行），
    而是"确保下载时类型可信"。
    """
    rows = _make_findings(db_session, scan, [("high", "confirmed")])
    target = rows[0]

    row = attachments_mod.store(
        db_session, finding_id=target.id, filename="evil.php5",
        content=b"<?php ?>", content_type="application/octet-stream",
    )

    assert row.stored_path.endswith(row.id), "白名单外的扩展名不该出现在路径里"
    assert ".php5" not in row.stored_path


def test_size_limit_is_enforced_on_real_bytes(
    ready_client: TestClient, db_session: Session, scan, monkeypatch
) -> None:
    """★ 上限按**真实字节数**判，不是按 `Content-Length`。

    路由层无法信任 `Content-Length`（分块传输、或者客户端直接撒谎），
    所以真正的闸门在 `attachments.store` 里。
    """
    from app.core.config import get_settings

    rows = _make_findings(db_session, scan, [("high", "confirmed")])
    target = rows[0]

    settings = get_settings()
    monkeypatch.setattr(settings, "attachment_max_bytes", 4, raising=False)

    response = ready_client.post(
        f"{API}/findings/{target.id}/attachments",
        files={"files": ("big.png", b"12345678", "image/png")},
    )
    assert response.status_code == 400, response.text
    assert "上限" in response.json()["error"]["message"]


def test_download_is_forced_as_attachment_not_inline(
    ready_client: TestClient, db_session: Session, scan
) -> None:
    """★ `Content-Disposition: attachment`。

    这一条对 SVG 尤其要紧：SVG 可以内嵌 `<script>`，
    内联渲染等于让一个**上传的文件**在本站的原点上执行。
    加 `attachment` 之后它只会被存下来，不会被执行。
    """
    rows = _make_findings(db_session, scan, [("high", "confirmed")])
    target = rows[0]

    created = ready_client.post(
        f"{API}/findings/{target.id}/attachments",
        files={"files": ("x.svg", b"<svg onload='alert(1)'/>", "image/svg+xml")},
    )
    assert created.status_code == 201, created.text

    download = ready_client.get(
        f"{API}/findings/{target.id}/attachments/{created.json()[0]['id']}/content"
    )
    assert download.headers["content-disposition"].startswith("attachment")
    assert download.headers["content-disposition"].isascii()


def test_attachment_belongs_to_its_finding(
    ready_client: TestClient, db_session: Session, scan
) -> None:
    """★ 不能用 A 漏洞的路径去读 B 漏洞的附件。

    现在只有单管理员、谈不上越权，但**接口的形状不该依赖这一点** ——
    将来加了第二个账号，这条路径就是"用 A 的权限读 B 的附件"。
    """
    first = _make_findings(db_session, scan, [("high", "confirmed")])[0]
    second = _make_findings(db_session, scan, [("low", "new")])[0]

    created = ready_client.post(
        f"{API}/findings/{first.id}/attachments",
        files={"files": ("a.png", b"DATA", "image/png")},
    )
    attachment_id = created.json()[0]["id"]

    crossed = ready_client.get(
        f"{API}/findings/{second.id}/attachments/{attachment_id}/content"
    )
    assert crossed.status_code == 404, "换一条漏洞的路径就不该读到"


def test_delete_attachment_removes_file_and_row(
    ready_client: TestClient, db_session: Session, scan
) -> None:
    """删除附件：文件与行都要没。"""
    rows = _make_findings(db_session, scan, [("high", "confirmed")])
    target = rows[0]

    created = ready_client.post(
        f"{API}/findings/{target.id}/attachments",
        files={"files": ("a.png", b"DATA", "image/png")},
    )
    attachment_id = created.json()[0]["id"]
    path = attachments_mod.absolute_path(
        db_session.get(FindingAttachment, attachment_id)
    )
    assert path.exists()

    response = ready_client.delete(f"{API}/findings/{target.id}/attachments/{attachment_id}")
    assert response.status_code == 204, response.text
    assert not path.exists()
    assert db_session.get(FindingAttachment, attachment_id) is None


def test_upload_rejects_when_count_exceeds_limit(
    ready_client: TestClient, db_session: Session, scan, monkeypatch
) -> None:
    """数量上限：一次传太多会占满磁盘与请求时间。"""
    from app.core.config import get_settings

    rows = _make_findings(db_session, scan, [("high", "confirmed")])
    target = rows[0]

    settings = get_settings()
    monkeypatch.setattr(settings, "attachment_max_count", 2, raising=False)

    response = ready_client.post(
        f"{API}/findings/{target.id}/attachments",
        files=[
            ("files", ("a.png", b"A", "image/png")),
            ("files", ("b.png", b"B", "image/png")),
            ("files", ("c.png", b"C", "image/png")),
        ],
    )
    assert response.status_code == 400, response.text


def test_list_attachments_404s_for_unknown_finding(
    ready_client: TestClient,
) -> None:
    """"这条漏洞没有附件"与"这条漏洞不存在"是两件事。

    对不存在的 id 返回空列表会让前端把一个笔误显示成"暂无附件"。
    """
    response = ready_client.get(f"{API}/findings/finding_does_not_exist/attachments")
    assert response.status_code == 404


def test_upload_writes_an_audit_row(
    ready_client: TestClient, db_session: Session, scan
) -> None:
    """上传要留痕：附件可能含敏感信息（内网截图、凭据抓包）。"""
    rows = _make_findings(db_session, scan, [("high", "confirmed")])

    created = ready_client.post(
        f"{API}/findings/{rows[0].id}/attachments",
        files={"files": ("a.png", b"DATA", "image/png")},
    )
    assert created.status_code == 201, created.text

    audit = ready_client.get(f"{API}/audit?limit=5")
    # ⚠️ 响应字段是 `entries`（不是 `items`）—— 契约 §3.9 的形状
    actions = [row["action"] for row in audit.json()["entries"]]
    assert "finding.attach" in actions


def _routes_accepting_uploads() -> list[tuple[str, str]]:
    """从 OpenAPI 规格里找出所有「请求体里含二进制文件」的端点。

    返回 `(方法, 路由模板)` —— 模板取自 OpenAPI 的 path key
    （形如 `/api/v1/scans/{scanId}/attachments`），可以直接与
    `MULTIPART_WRITE_ENDPOINTS` 逐字比对。

    ⚠️ **不能遍历 `app.routes` 找 `APIRoute`** —— 本项目的 FastAPI 版本
    把 `include_router` 的子路由包成 `_IncludedRouter`，直接
    `isinstance(route, APIRoute)` 会**一个都匹配不到**（实测 14 个顶层
    路由里 11 个是 `_IncludedRouter`）。我第一版就是这么写的，
    而它失败的样子是"探测到 0 个上传端点"，看起来像扫描逻辑写错了。

    走 OpenAPI 规格反而更稳：它是**对外承诺的契约**，
    不随 FastAPI 内部路由对象的结构变化而变。

    判据是 `requestBody.content` 里的 `multipart/form-data` ——
    那正是浏览器传文件时发出的类型，也是 CSRF 防线拦的那一种。
    """
    from app.main import app

    spec = app.openapi()
    found: list[tuple[str, str]] = []
    for path, operations in spec.get("paths", {}).items():
        for method, operation in operations.items():
            if not isinstance(operation, dict):
                continue
            content = (operation.get("requestBody") or {}).get("content") or {}
            if "multipart/form-data" in content:
                found.append((method.upper(), path))
    return found


def test_every_upload_endpoint_is_registered_for_multipart() -> None:
    """★★ 自检：所有收文件的端点都必须在 multipart 白名单里。

    ## 为什么需要这条测试

    `app/auth/deps.py` 的 CSRF 防线拒绝一切 `multipart/form-data`
    的写请求（跨站表单是最经典的 CSRF 载体，而"本 API 不收表单"
    让整类攻击无处落脚）。上传端点是**仅有的例外**，靠一张显式
    登记表 `MULTIPART_WRITE_ENDPOINTS` 放行。

    那张表会腐烂：新增一个上传端点、忘了登记，症状是它的上传
    被 400 挡掉（"写操作必须使用 Content-Type: application/json"），
    而那条消息与"忘了登记"这件事**看起来毫无关系** ——
    下一个人会去查前端是不是发错了 Content-Type。

    ## ★ 这条测试真的抓到过（任务 12）

    加暂存端点 `POST /scans/attachments/staging` 时，当时那张表用的是
    **后缀匹配**（`endswith("/attachments")`），而这个新路径
    **不以 `/attachments` 结尾** —— 于是它没被放行，上传被 400 挡掉。

    修法是把登记表改成**精确匹配路由模板**（`request.scope["route"].path`），
    而下面这条断言正是"漏登记会被抓住"的机制 —— 它当时确实红了。
    """
    from app.auth.deps import FORM_CONTENT_TYPES, MULTIPART_WRITE_ENDPOINTS

    endpoints = _routes_accepting_uploads()
    assert endpoints, (
        "一个收文件的端点都没找到 —— 探测逻辑失效了，这条测试在空转"
        "（它必须能找到上传端点，否则它什么都没在保护）"
    )

    # ⚠️ OpenAPI 的 path 是**对外路径**（含 `/api/v1`），而登记表用的是
    # `route.path`（子路由模板，**不含前缀** —— 前缀是 include_router 加的）。
    # 两边对齐一次，否则这条测试会因为"前缀"而永远红。
    from app.core.config import get_settings

    prefix = get_settings().api_prefix.rstrip("/")

    def normalize(path: str) -> str:
        return path[len(prefix):] if prefix and path.startswith(prefix) else path

    unregistered = [
        f"{m} {p}" for m, p in endpoints if (m, normalize(p)) not in MULTIPART_WRITE_ENDPOINTS
    ]
    assert not unregistered, (
        "这些端点接受文件上传，但没有登记进 MULTIPART_WRITE_ENDPOINTS，"
        f"它们的上传会被 CSRF 防线 400 挡掉：{unregistered}\n"
        "（登记表用的是**完整的路由模板**，如 "
        "'/api/v1/scans/{scanId}/attachments'）"
    )
    # 顺带确认那道防线本身还在 —— 别把这个例外变成了"表单全放开"
    assert "multipart/form-data" in FORM_CONTENT_TYPES


def test_attachments_survive_soft_delete(
    ready_client: TestClient, db_session: Session, scan
) -> None:
    """软删除**不动**附件。

    进回收站不是"销毁证据" —— 那是永久删除才做的事。
    删掉附件会让"恢复"变得不完整（记录回来了、证据没了）。
    """
    rows = _make_findings(db_session, scan, [("high", "confirmed")])
    target = rows[0]

    created = ready_client.post(
        f"{API}/findings/{target.id}/attachments",
        files={"files": ("a.png", b"DATA", "image/png")},
    )
    assert created.status_code == 201

    ready_client.post(f"{API}/findings/batch/delete", json={"ids": [target.id]})

    # 附件行仍在（跟着漏洞一起进回收站，而不是被删掉）
    remaining = db_session.execute(select(FindingAttachment)).scalars().all()
    assert len(remaining) == 1
    assert Path(attachments_mod.absolute_path(remaining[0])).exists()
