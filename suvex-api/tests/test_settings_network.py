"""`GET /settings/network`（v1.90）：把生效中的出站白名单摊开。

★ 它不是配置端点，所以只测两件事：值真的来自同一个来源、权限是 admin。
"""

from __future__ import annotations

API = "/api/v1"


def test_network_restrictions_reports_the_effective_allowlist(ready_client) -> None:  # noqa: ANN001
    resp = ready_client.get(f"{API}/settings/network")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["guardEnabled"] is True
    # 内置基础设施与环回必须来自 `app.chat.scope` 的同一份常量
    assert "pypi.org" in body["builtinHosts"]
    assert "127.0.0.0/8" in body["builtinCidrs"]


def test_network_restrictions_is_admin_only(viewer_client) -> None:  # noqa: ANN001
    assert viewer_client.get(f"{API}/settings/network").status_code in (401, 403)
