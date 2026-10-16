# Gateway And Lease v2

状态：浏览器 / 网关 / 控制面的当前租约契约。v1 描述"申请租约并代理"；v2 在其上冻结
**邀请码准入**与**项目分享导入**。v1 的路由、凭据与代理约束继续有效，本文件只写增量。

## 邀请码准入

`POST /control/leases` 请求体新增可选字段：

| 字段 | 说明 |
| --- | --- |
| `workspace_id` | 持久项目 UUID（可选；不传即匿名临时租约） |
| `invitation_code` | 邀请码（可选）。明文规则见下 |
| `label` | 诊断标签，不参与鉴权 |

服务端规则（客户端提交的类型/优先级一律忽略）：

- 提交**有效系统码**（`TLM-SYS-v<版本>-<43 位 base64url>`）：允许入场，**最高优先级**；
  满槽时可由控制面**抢占一个**低优先级活动实例（优先抢占最低优先级、同级最旧），
  **只回收其计算、保留其持久卷**。会打断被抢占者的进行中任务，不声称无损。
- 提交**有效分享码**（`TLM-SHARE-v<版本>-<43 位 base64url>`）：**不会**通过本接口获得
  入场或抢占；分享码只用于 `POST /control/workspace-imports`。把它当租约邀请码提交一律拒绝。
- 提交**格式合法但无效/已撤销/已过期**的码：**显式拒绝**，绝不退化为"无码入场"。
- 未提交码：
  - `invitation_only=false`：按普通优先级入场（保持旧行为）。
  - `invitation_only=true`：仅当该 `user_id` + `workspace_id` 已有导入获得的普通 trial 授权
    才允许；否则 `403 invitation_required`。
- 已持有有效租约凭据时的幂等复用**也要**经过准入判定，旧 Cookie 不能绕过 invitation-only 门。

码格式与安全：两类码前缀、密码空间与 HMAC 域隔离独立；明文只在签发响应出现一次；
DB 只存哈希。日志、事件与响应（除签发外）都不得出现明文码。

## 公开能力探针

`GET /control/access-policy`（无需鉴权，只回答能力，不含任何码）：

```json
{"invitation_only": true, "system_invitation_supported": true, "project_sharing_supported": true}
```

前端用 `invitation_only` 决定邀请码输入框是否必填；请求失败或字段缺失时**不放行**（fail closed）。
注意：这只影响 UI 提示，真正的门在 `POST /control/leases` 服务端。

## 项目分享导入

| 方法与路径 | 归属与行为 |
| --- | --- |
| `POST /control/workspaces/{workspace_id}/invitations` | 项目所有者签发分享码，201 返回 `{invitation, code, workspace}`；明文码仅此一次 |
| `GET /control/workspaces/{workspace_id}/invitations` | 列出该项目分享码（不含明文） |
| `DELETE /control/workspaces/{workspace_id}/invitations/{invitation_id}` | 撤销分享码 |
| `POST /control/workspace-imports` | 用分享码导入，200 返回 `{workspace, reused}`；可选 `Idempotency-Key` 头 |

导入语义：

- 只有声明了 v3 `content_only` 布局的项目才能签发分享码；否则 `409 share_source_unsupported`。
- 导入得到一个**属于接收用户的独立项目副本**（新 UUID、新卷），源项目不被共享挂载。
- 导入成功后控制面为该副本记录一条**普通 trial 授权**，因此即使 `invitation_only=true`，
  接收用户之后打开自己的副本也满足准入（普通优先级，绝不提升到系统码优先级）。
- 幂等：带 `Idempotency-Key` 时同一用户重试返回同一副本；不带时同一用户对同一源项目重复导入
  也返回已有副本（`reused=true`）。
- 导入会**先停止源项目的计算**再复制，源端进行中的任务可能被打断。

## 管理路由

`/control/admin/*` 仍由公网网关拒绝（`nginx.conf` 对 `/control/admin/` 返回 403）；
运维从本地控制面入口带 `X-Admin-Token` 访问。系统邀请码的签发/列表/撤销：

| 方法与路径 | 行为 |
| --- | --- |
| `GET /control/admin/invitations` | 列出系统码（不含明文） |
| `POST /control/admin/invitations` | 签发系统码，201 返回 `{invitation, code}`，明文仅此一次 |
| `DELETE /control/admin/invitations/{invitation_id}` | 撤销系统码，写入持久 tombstone |

撤销是持久的：即使配置文件中仍预置同一码，重启也不会复活它。

## 凭据（不变）

浏览器用 HttpOnly、Path=/ 的租约 Cookie（默认名 `tutorlm_lease`）；脚本用租约 Bearer；
同时提供时 Cookie 优先。应用 Bearer 不能替代租约。HTTP 本地开发 `Secure=false`，
对外提供保密性前必须接入 TLS。