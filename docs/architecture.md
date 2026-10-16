# Architecture

## 核心边界

```text
Browser -> Nginx -> admission -> Control Plane -> Docker API
              |
              +-> selected instance -> Runtime Host -> plugins/services
                                             |
                                             +-> per-session SQLite/files
                                             +-> model provider
```

`runtime-host/` 是本仓库的外层开发边界，应用的 `backend/host` 属于容器内部。
控制面本身可以运行在独立基础设施容器中，和访客应用容器有不同权限与生命周期。

## 所有权

| 开发范围 | 所有权 | 不应承担 |
| --- | --- | --- |
| `runtime-host/control-plane` | 租约、容量、原子分配、代次、回收、Docker 适配器 | Prompt、文档解析、应用 session |
| `runtime-host/gateway` | 静态制品托管、准入、受控转发、SSE 代理 | 前端业务交互、模型调用 |
| `runtime-host/deploy` | 外层镜像、网络、卷、端口与配置 | 应用镜像内部实现 |
| `instance-app/backend` | 实例内 Host、插件、业务分层、任务、模型与存储 | Docker 调度、租约数据库 |
| `instance-app/frontend` | 阅读、节点图谱、解释、串讲、浏览器身份接入 | 决定上游容器地址 |
| `instance-app/deploy` | 应用镜像及前端制品构建 | 外层运维配置 |
| `contracts` | 两侧共同评审的协议 | 共享私有实现 |

## 数据和权限

控制面 SQLite 保存槽位、租约和事件；业务实例拥有独立数据卷，实例内部再按会话
分开 SQLite 和文件目录。两侧不共享数据库，不相互 import，也不共享可变“当前用户”。

Docker socket 等价于高权限宿主机访问，只授予控制面，不授予网关或业务实例。
开发部署默认绑定 loopback 并使用独立资源前缀；不同环境不共用数据库、网络和卷。

## 当前范围

本仓库当前仅包含外层实现。应用目录是开发占位，尚未包含业务实现。
空闲探针、可信 drain、自动会话交换与 HTTPS 尚不能作为已完成能力依赖。
