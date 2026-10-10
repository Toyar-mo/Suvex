"""encrypt provider api keys

把 `provider_accounts.api_key`（明文）换成两列：

* `api_key_encrypted` —— Fernet 密文，唯一持有凭据的地方；
* `api_key_hint`      —— 可展示掩码（`sk-****abcd`），让读接口不必解密。

## 为什么可以直接 DROP 掉明文列（不迁数据）

因为**那里不可能有真实凭据**。任务 1 交付时 provider 的全部端点都是
`501 NOT_IMPLEMENTED` 占位，没有任何代码路径写进过 `api_key`；
库里若有值，只可能来自测试夹具里那些 `sk-not-a-real-key` 之类的假串。

这条判断是可查的，而不是推测：`git log` 里任务 1 的 `providers/router.py`
六个端点全部 `pending(...)`。

所以要做的不是"把明文加密后搬过去"（那需要在这条迁移里引入运行时密钥依赖，
而迁移脚本**不应该**依赖 `SUPEX_SECRET_KEY` —— 它会让"在没配密钥的机器上
跑迁移"变成不可能）。直接换代是更干净、也更容易验证的做法。

## 降级会丢数据（如实记）

`downgrade()` 能把列加回来，但**恢复不了明文** —— 它已经不存在了。
这里也不假装能恢复：加回来的 `api_key` 是空的，降级之后必须重新填写凭据。

替代方案是"降级时把密文解密写回"，但那要求迁移脚本能拿到密钥，
而上一条刚说过那是个坏主意。宁可让降级明确地丢一次数据，
也不要让迁移脚本持有解密能力。

Revision ID: 54eaafabd74c
Revises: 5912c13304a2
Create Date: 2026-09-23 16:58:20.891248+00:00

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# 迁移版本标识，由 alembic 分配，不要手改
revision: str = '54eaafabd74c'
down_revision: str | None = '5912c13304a2'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 可空：没有凭据时是 NULL，而不是"加密过的空串"。
    # 本地模型（Ollama）合法地不需要 key，所以 NULL 是一个正常状态 ——
    # 用空串代替会让 `hasKey` 变成"密文列非空"这种间接判断。
    op.add_column('provider_accounts', sa.Column('api_key_encrypted', sa.Text(), nullable=True))

    # 可展示掩码。单独存一列而不是每次解密算出来：
    # 解密就要把明文带进内存、越过 service 层，而读接口根本不需要明文。
    # 预先存好掩码 ⇒ 明文只在真的要调 LLM 的那一刻才被解开。
    op.add_column('provider_accounts', sa.Column('api_key_hint', sa.String(length=40), nullable=True))

    # 明文列就地删除。理由见文件头：那里不可能有真实凭据。
    op.drop_column('provider_accounts', 'api_key')


def downgrade() -> None:
    op.drop_column('provider_accounts', 'api_key_hint')
    op.drop_column('provider_accounts', 'api_key_encrypted')

    # ⚠️ autogenerate 生成的这一行是 `nullable=False` 且没有默认值 ——
    # 在 PostgreSQL 上，给一张**已有数据**的表加 NOT NULL 无默认值列会直接失败
    # （`column "api_key" contains null values`）。
    # 这是 autogenerate 的经典陷阱：它只看模型定义，不看表里有没有行。
    #
    # 正确写法是"先带 server_default 加上去，再把默认值摘掉"。
    # 保留 `server_default=""` 也说得通（空串在前端 = 未配置），
    # 但模型上并没有这个默认值，留着它会让下一次 autogenerate 报差异 ——
    # 所以加完就摘。
    op.add_column(
        'provider_accounts',
        sa.Column('api_key', sa.Text(), nullable=False, server_default=''),
    )
    op.alter_column('provider_accounts', 'api_key', server_default=None)

    # 到这里 `api_key` 是空的：明文恢复不了（见文件头的说明）。
    # 降级之后必须重新填写每个 provider 的凭据。
