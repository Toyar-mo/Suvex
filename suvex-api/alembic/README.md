# 迁移（Alembic）

命令都在项目根目录执行，用的是项目虚拟环境里的 `alembic`。

```bash
alembic current                 # 当前版本
alembic history --verbose       # 全部版本
alembic upgrade head            # 升级到最新
alembic downgrade -1            # 回退一步
alembic revision --autogenerate -m "描述"   # 按模型差异生成迁移
alembic upgrade head --sql      # 离线模式：只输出 SQL，不连库
```

## 为什么 `alembic.ini` 里一个中文都没有

**`alembic.ini` 必须保持纯 ASCII。** Alembic 用 `encoding="locale"` 读它
（`alembic/util/compat.py`），中文 Windows 上 locale 是 GBK，于是文件里
任何一个中文字符都会让**所有** alembic 命令崩在启动阶段：

```
UnicodeDecodeError: 'gbk' codec can't decode byte 0x82 in position 18
```

英语 Windows（cp1252）遇到任何非 ASCII 字节同样会崩。
这不是理论风险 —— 本项目第一次跑迁移就是这么失败的，
而且报错信息指向 `configparser`，乍看像是配置写错了。

所以：解释写在**这份 README**（UTF-8，只给人看），
`alembic.ini` 只留机器要读的键值。

## 为什么 `sqlalchemy.url` 是空的

写在 ini 里等于把数据库口令复制一份进 git，而那份永远不会被更新 ——
表现是「改了 `.env` 但迁移还是连旧库」。

`alembic/env.py` 从 `app.core.config.Settings` 读（也就是 `.env`）。
临时指向别的库：

```bash
alembic -x db_url=postgresql+psycopg://user:pass@host:5432/db upgrade head
```

## 为什么从第一个迁移就版本化

不用 `Base.metadata.create_all()` 建表。那样建出来的库**没有版本记录**，
之后任何一次结构变更都只能靠人工比对，而这是「开发机能跑、
部署机跑不起来」最常见的成因。

`versions/` 里的第一个文件就是初始结构，因此：

* 部署流程永远是 `alembic upgrade head`，不需要「空库就 create_all、
  非空库就 upgrade」这种分支；
* `downgrade` 从第一天起就是可验证的（`upgrade → downgrade → upgrade`
  这条回路跑得通，才说明每个迁移都写对了）。

## autogenerate 的前提：模型必须先被导入

`alembic/env.py` 里 `from app.models import __all__` **不是装饰**。
`Base.metadata` 只包含已经被导入过的模型类；少导入一个领域，
autogenerate 会认为那些表「多余」，于是生成一个**把它们全删掉**的迁移。

加新模型的流程固定为：

1. 在领域包里定义模型；
2. 在 `app/models.py` 里加一行 import 与 `__all__` 条目；
3. `alembic revision --autogenerate -m "..."`；
4. **打开生成的迁移文件读一遍**。autogenerate 不会识别重命名 ——
   它看到的是「删一列 + 加一列」，会丢数据。这类改动必须手写。

## 命名约定

约束名由 `app/core/db.py` 的 `NAMING_CONVENTION` 从表名与列名**算出来**，
不用数据库自动生成的 `findings_scan_id_fkey` 这种名字。
理由：等到某个迁移要 `drop_constraint` 时，就不必去猜数据库当时编了什么名，
而那个名字在不同环境还可能不同。
