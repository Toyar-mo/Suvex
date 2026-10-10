"""处理人名单（任务 10 P2）。

## 为什么是独立包而不是塞进 `app/settings/`

`app/settings` 是**配置**（沙箱、通知、保留规则、令牌）—— 它管的是
"系统怎么运行"。成员名单管的是"**谁负责**"，它会被 `findings` 域引用
（`assignee_id`），也会被将来的 SLA、通知、报告署名引用。

放在 settings 里会让"改个沙箱参数"和"加一个同事"共用一个路由文件，
而那个文件已经 300 多行。更要紧的是依赖方向：settings 不应该成为
其他领域都要依赖的公共设施。

## 为什么不是登录账号

见 `app/settings/models.Member` 的 docstring：本系统是单管理员的，
"负责人"是**业务概念**，不是身份概念。
"""

from app.members.models import Member

__all__ = ["Member"]
