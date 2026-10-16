# Gateway And Lease v1

状态：当前控制面与 Nginx 网关的接口约定。

## 路由

| 路径 | 归属与行为 |
| --- | --- |
| 静态页面与 assets | 网关直接读取应用团队的前端制品，无需租约 |
| `/healthz` | 网关自身存活 |
| `/control/health`、`/control/capacity` | 控制面存活和池容量，二者含义不同 |
| `POST /control/leases` | 申请租约，有效凭据重复申请时复用；满池 503 + Retry-After |
| `GET /control/leases/current` | 当前租约，刷新 Cookie 但不延长绝对租约 TTL |
| `DELETE /control/leases/current` | 撤销租约并回收，清除 Cookie |
| `/control/admin/*` | 公网网关拒绝，运维从本地控制面入口访问并提供管理 token |
| `/api/*`、`/health`、`/health/ready` | 先校验租约，再代理到对应实例 |

网关内部的 `/_lease_auth` 调用 `/control/admission`。
有效准入返回 204，包含 `X-TutorLM-Upstream`、租约、槽位和代次响应头；
无效、过期、撤销或尚不可用的租约返回 401。
上游地址必须与控制面生成的本槽位、本代次容器地址完全一致。

## 凭据

浏览器使用默认名为 `tutorlm_lease` 的 HttpOnly、Path=/ Cookie，名称可配置。
脚本可以使用租约 Bearer；同时提供时 Cookie 优先。应用 Bearer 不能替代租约。
租约 token 只以哈希存储；公开租约响应不包含内部 upstream 或 token_hash。

HTTP 本地开发使用 `Secure=false`；面向公网提供保密性前必须接入 TLS 并配置 Secure Cookie。
HTTP 开发模式不提供传输保密性。

## 代理约束

客户端提供的槽位或 upstream 不得决定转发目标。网关清除相关转发请求头，
保留应用 Authorization 和 Cookie；SSE 禁用代理缓冲并给予足够读超时。

注意：当前配置未单独禁止公网直接访问 `/control/admission`，
不能将内部路由头“不被有效租约持有者读取”视为既有安全属性。
