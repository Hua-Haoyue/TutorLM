# Architecture

## 核心边界

```text
Browser -> Nginx -> admission -> Control Plane -> Docker API
              |
              +-> selected instance -> Runtime Host -> plugins/services
                                             |
                                             +-> per-session data (volume-external)
                                             +-> model provider
```

`runtime-host/` 是本仓库的外层开发边界，应用的 `backend/host` 属于容器内部。
控制面本身可以运行在独立基础设施容器中，和访客应用容器有不同权限与生命周期。

## 所有权

| 开发范围 | 所有权 | 不应承担 |
| --- | --- | --- |
| `runtime-host/control-plane` | 用户身份、持久项目元数据、邀请码准入与分享、计算租约、容量、代次、回收、卷复制与 Docker 适配器 | Prompt、文档解析、应用 session |
| `runtime-host/gateway` | 静态制品托管、准入、受控转发、SSE 代理 | 前端业务交互、模型调用 |
| `runtime-host/deploy` | 外层镜像、网络、卷、端口与配置 | 应用镜像内部实现 |
| `instance-app/backend` | 实例内 Host、插件、业务分层、任务、模型与存储 | Docker 调度、租约数据库 |
| `instance-app/frontend` | 阅读、节点图谱、解释、串讲、浏览器身份接入 | 决定上游容器地址 |
| `instance-app/deploy` | 应用镜像及前端制品构建 | 外层运维配置 |
| `contracts` | 两侧共同评审的协议 | 共享私有实现 |

## 数据和权限

控制面 SQLite 保存用户身份、项目元数据、槽位、租约、邀请码（只存哈希）与事件。
持久项目各自拥有独立卷，可由不同代次和槽位的临时容器重新挂载；计算回收保留项目卷，
项目闲置超过 4 小时才清理。

项目数据位于卷内 `users/<owner_uuid>/workspaces/<workspace_uuid>` 目录，由控制面按 UUID 推导；
应用 session 与凭证放在卷外临时目录，不随项目数据卷复制。分享/克隆在服务端按目录整树复制，
显式排除会话/凭证子树，接收者得到属于自己的新卷与新项目，不会共享挂载源项目。

匿名临时租约兼容 v1 的临时卷策略。两侧不共享数据库，不相互 import。

Docker socket 等价于高权限宿主机访问，只授予控制面，不授予网关或业务实例。
开发部署默认绑定 loopback 并使用独立资源前缀；不同环境不共用数据库、网络和卷。

## 当前范围

本仓库当前包含外层实现和前端租约生命周期模块，完整工作台与容器内业务尚未实现。
持久项目契约已推进到 v3（数据子目录、卷外会话目录与卷复制）；应用端需实现 v3 才能接入分享。
匿名/持久运行协议见 `../contracts/`，持久工作区的身份、恢复、保留与清理流程见 `workspaces.md`。
空闲探针、可信 drain、自动会话交换与 HTTPS 尚不能作为已完成能力依赖。