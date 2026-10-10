# 迁移版本目录。
#
# 每个文件是一个 revision。**从第一个迁移开始就有版本管理** ——
# 不用 `Base.metadata.create_all()` 建表：那样建出来的库没有版本记录，
# 之后任何一次结构变更都要靠人工比对，而这正是「开发机能跑、部署机跑不起来」
# 最常见的成因。
#
# 查看历史：`alembic history --verbose`
# 当前版本：`alembic current`
# 升级：    `alembic upgrade head`
# 回退一步：`alembic downgrade -1`
