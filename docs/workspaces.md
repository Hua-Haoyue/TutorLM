# 持久工作区

当前准入与分享集成见 [v4 契约](../contracts/instance-runtime-v4.md)：
分享字符串只允许复制项目，不授予计算权限。邀请模式下，导入副本申请槽位仍必须提供
系统邀请码；历史 trial grant 不再绕过此规则。受控应用会话使用 `/control/app-session`，
不要求第二次输入应用体验码。下文涉及旧 trial grant 的说明以本段与 v4 为准。

## 模型

```text
User UUID + recovery credential
  +-- Workspace A -> durable volume A -> temporary compute lease/container
  +-- Workspace B -> durable volume B -> temporary compute lease/container
  +-- Workspace C -> durable volume C -> no compute required while stored
```

一个 workspace 就是一个持久项目。用户身份与项目元数据保存在控制面 SQLite，
业务文件保存在项目自己的 Docker 具名卷中。容器只是临时计算载体，不承担项目身份。

当前已实现控制面的身份、项目、租约绑定、数据清理及前端页面生命周期模块；完整工作台尚未实现。
真正的文档、节点、学习进度恢复，需要应用团队按 `../contracts/instance-runtime-v2.md` 接入。
不符合持久就绪协议的镜像会被拒绝，不会被当作已具备持久化能力。

## 身份与恢复

首次 `POST /control/users` 返回用户 UUID 和一次性展示的 `recovery_token`，
并设置作用域为 `/control` 的 HttpOnly 用户 Cookie。
数据库只保存恢复凭据的 HMAC，应用容器不会收到这类外层凭据。

再次登录调用 `POST /control/users/session`，请求体包含 `user_id` 和 `recovery_token`。
用户 UUID 用于定位身份，不足以单独授权。恢复凭据丢失时目前没有重置流程。
这是一种匿名持久身份机制，不是邮箱、密码或 OAuth 账号系统。
HTTP 只适合本地开发；对外提供恢复凭据和私有数据前必须配置 TLS 与 Secure Cookie。

脚本也可以通过 `X-User-ID` 和 `X-User-Token` 传递用户凭据；它们与应用/租约 Bearer 不混用。
同一浏览器切换用户身份时会撤销并清除前一个用户的计算租约，但保留其持久项目数据。

## API 流程

| 方法与路径 | 行为 |
| --- | --- |
| `POST /control/users` | 创建用户，签发 UUID 与恢复凭据 |
| `POST /control/users/session` | 用 UUID + 恢复凭据重新登录 |
| `GET /control/users/me` | 当前用户，不刷新项目时间 |
| `DELETE /control/users/session` | 退出，释放当前浏览器的项目计算租约，保留项目数据 |
| `POST /control/workspaces` | 创建项目，JSON 包含 `title`，无需占用计算槽位 |
| `GET /control/workspaces` | 列出当前用户未过期的项目，不续期 |
| `GET /control/workspaces/{uuid}` | 项目元数据，不续期 |
| `POST /control/workspaces/{uuid}/touch` | 显式标记该项目正在使用 |
| `POST /control/leases` | JSON 包含 `workspace_id`，鉴权后挂载项目卷并启动计算 |
| `DELETE /control/leases/current` | 只回收计算，不删除持久项目 |

每个浏览器 Cookie 上下文当前绑定一个计算租约。切换项目先释放当前租约，再打开另一项目。
同一用户可以保存多个项目，独立浏览器上下文或脚本也可以为不同项目申请独立计算租约。
不能用旧租约 Cookie 自动切换到另一项目；同项目重复申请则幂等复用。

新客户端应显式传 `workspace_id`。未指定该字段的旧入口仍是匿名临时租约，释放时删除临时卷，
不提供项目持久化。项目 UUID 不会被无权限的客户端用于挂载数据。

## 两个独立时钟

- 计算时钟：`CONTROL_PLANE_LEASE_TTL_SECONDS`，默认 3600 秒，届满拒绝准入并由 GC 回收容器。
- 项目时钟：`CONTROL_PLANE_WORKSPACE_IDLE_TTL_SECONDS`；设为 `inf` 时永久保留，
  重新登录后仍可列出并打开历史项目；设为正整数时才按最后使用时间自动清理。

## 页面关闭释放计算

计算租约在全部使用页面关闭或达到绝对到期时间时回收，保留项目卷与用户身份。
前端在取得租约后安装 `instance-app/frontend/assets/lease-lifecycle.js`，
为当前 document 登记独立 page UUID，默认每 20 秒报告一次页面存活。
当前完整工作台尚不存在，模块接入是工作台实现的必要步骤，不会自动注入任意页面。

| 接口 | 行为 |
| --- | --- |
| `POST /control/leases/{lease_id}/pages` | JSON `page_id`，登记文档并返回心跳周期 |
| `POST /control/leases/{lease_id}/pages/{page_id}/heartbeat` | 只刷新页面存活，不延长计算绝对期限或项目闲置期限 |
| `POST /control/leases/{lease_id}/pages/{page_id}/close` | 标记该文档已关闭，支持无请求体 beacon |

接口验证当前租约 Cookie 或租约 Bearer，并严格绑定 lease UUID。
延迟到达的旧页面通知不能释放新租约，也不清除共享 Cookie。
用户身份和项目数据都不会因为关闭页面而删除；匿名临时卷仍按 v1 回收。

`pagehide` 发送通知；服务端确认这是最后一个页面后直接释放计算租约，不再等待关闭宽限期。
若浏览器没有送达 `pagehide`，仍由心跳超时和 GC 兜底，不承诺浏览器关闭瞬间一定送达通知。
刷新、后退缓存恢复或新页面在宽限期内登记则继续使用租约；其他标签页还有心跳时不回收。
不能用切换标签、窗口最小化等可见性变化直接判定用户已关闭页面。

丢失关闭通知时，默认 120 秒没有任何存活页面心跳后回收，并有最多一个 GC 周期延迟。
后台浏览器冻结心跳或长时间离线也可能触发这一兜底；重新进入时重新获取计算租约即可。
上述策略是显式计算释放，不因实例仍有任务而无限延后停止，项目数据保留时间独立计算。
未登记页面的脚本客户端保持原行为，按绝对租约期限或显式 DELETE 释放。

Compose 中通过 `TUTORLM_PAGE_CLOSE_GRACE_SECONDS` 和
`TUTORLM_PAGE_HEARTBEAT_TIMEOUT_SECONDS` 配置上述时间。

## 项目闲置计时

项目创建、计算申请、业务准入和显式 touch 更新项目使用时间。
首次登记新的页面视为打开项目；后续仅表示页面存活的自动 heartbeat 不算业务使用。
查看列表、登录、查询租约和后台管理轮询不会替全部项目续期。
前端只应为正在实际使用的项目发 touch，不应后台为所有项目续期。

刚好闲置 4 小时不清理，严格超过后在下一轮 GC 清理，通常还有一个 GC 周期的调度延迟。
`expires_at` 是闲置清理资格时间，不承诺精确到该秒执行物理删除。
项目有运行中容器或隔离残留时不清理；先停止并删除容器，再删除到期项目卷。

GC 使用数据库事务防止“重新打开”和“删除数据”同时成功，单项目同一时刻只有一个写入容器。
数据卷删除失败会保留删除中状态，禁止重新打开并在后续 GC 重试。
项目卷丢失不会被静默替换为空卷。已删除的项目 UUID 不能复活为新项目。

## 运行边界

控制面应保持一个活动进程，通过线程处理请求和 GC；多副本生命周期协调不在当前实现范围内。
配置 `GC_ENABLED=false` 会关闭后台清理，需要显式调用管理 GC；生产使用不得依赖关闭 GC 的测试配置。
Docker 具名卷保证容器生命周期之外的数据保留，不等于跨主机备份或高可用存储。
fake adapter 的模拟卷只在内存中，用于开发和测试，不是真实持久化存储。
Compose 的 `.env` 使用 `TUTORLM_LEASE_TTL_SECONDS`、`TUTORLM_WORKSPACE_IDLE_TTL_SECONDS` 和
`TUTORLM_GC_INTERVAL_SECONDS` 配置对应时钟。备份时必须同时保护控制面身份/项目索引和项目卷。
不要用 Compose 删卷、全局 prune 或人工删除项目卷绕过生命周期管理。

## 邀请码准入

控制面可用 `CONTROL_PLANE_INVITATION_ONLY`（TOML `invitationonly`）开启"仅邀请码入场"。
关闭时（默认）匿名入口按普通优先级放行，保持旧行为。

- **系统码**（`TLM-SYS-v<版本>-<43 位 base64url>`）：最高优先级。满槽时可抢占一个低优先级
  活动实例，**只回收其计算、保留其持久卷**；会打断被抢占者进行中的任务，不声称无损。
- **分享码**（`TLM-SHARE-v<版本>-<43 位 base64url>`）：只用于项目导入，不授予租约入场，
  也不会提升优先级。

两类码由 `control_plane.invitations` 生成（32 随机字节 / 256 bit），前缀、密码空间与哈希域都独立。
明文只在签发响应出现一次；数据库只存哈希；日志与事件不出现明文。系统码可由 CLI 生成并入库，
也可在受保护的配置文件中用 `system_invitation_codes` 预置（启动时种入，已撤销的不复活）。
不要用可猜测的字符串充当邀请码。

| 接口 | 鉴权 | 行为 |
| --- | --- | --- |
| `GET /control/access-policy` | 公开 | `{invitation_only, system_invitation_supported, project_sharing_supported}`，不含任何码 |
| `GET/POST /control/admin/invitations` | `X-Admin-Token` | 列出 / 签发系统码（明文仅签发时一次） |
| `DELETE /control/admin/invitations/{invitation_id}` | `X-Admin-Token` | 撤销系统码（写持久 tombstone） |

外层的邀请码门与"应用自身的体验码"（`TUTORLM_INVITE_CODE`）是**两回事**：前者是控制面准入，
后者属于应用内部逻辑。以控制面准入为唯一邀请门的新业务不应要求用户再输入一遍应用体验码；
但应用自己的身份鉴权仍然保留，控制面尚未实现受信的会话交换，也不伪造应用 session。

## 项目分享与导入

项目所有者可为自己的项目签发分享码，接收用户导入后得到一个**独立副本**：

| 接口 | 鉴权 | 行为 |
| --- | --- | --- |
| `POST /control/workspaces/{uuid}/invitations` | 用户 Cookie / 头 | 签发分享码，返回 `{invitation, code, workspace}` |
| `GET /control/workspaces/{uuid}/invitations` | 用户 Cookie / 头 | 列出该项目的分享码（不含明文） |
| `DELETE /control/workspaces/{uuid}/invitations/{invitation_id}` | 用户 Cookie / 头 | 撤销分享码 |
| `POST /control/workspace-imports` | 用户 Cookie / 头 | 用分享码导入，返回 `{workspace, reused}`，支持 `Idempotency-Key` |

- 副本是接收用户的新 UUID、新卷，源项目不共享挂载。导入只复制内容，不授予计算权限；
  `invitation_only=true` 时接收用户还需系统邀请码申请副本的计算租约。
- 只有声明了 v3 `content_only` 存储布局的项目才能签发分享码；否则拒绝。
- 导入会先停止源项目的计算再复制，源端进行中的任务可能被打断；失败不发布半成品。

## 项目数据目录与会话目录

持久项目的数据根由控制面注入 `TUTORLM_DATA_DIR`：

- 旧项目：等于挂载点（`/data`），行为与 v2 一致，无需搬迁。
- 新项目：`挂载点/users/<owner_uuid>/workspaces/<workspace_uuid>`，
  相对挂载点的子目录另见 `TUTORLM_WORKSPACE_SUBDIRECTORY`。

应用 session 与鉴权数据必须放在卷外临时目录 `TUTORLM_SESSION_DATA_DIR`，不能写进项目数据目录：
分享复制按目录整树拷贝，卷内任何 session 或凭证都会变成接收者的身份。
容器内接口细节见 `../contracts/instance-runtime-v3.md`。
