# Outer Deployment

本目录只构建控制面和配置网关。应用镜像由 `instance-app/deploy/` 团队提供，
前端制品由 `instance-app/frontend/` 团队提供。
所有实例统一由控制面按租约动态创建，Compose 只声明外层服务。
持久项目需要应用镜像实现 `contracts/instance-runtime-v2.md`；要使用项目分享还需实现
`contracts/instance-runtime-v3.md`（数据子目录 + 卷外会话目录 + 存储布局声明）。
计算到期保留项目卷，项目闲置超过默认 14400 秒后由控制面 GC 删除。
不要人工把项目卷当临时容器附件删除。

## 两种配置模式

控制面配置分层为：内置默认 < TOML 文件 < `CONTROL_PLANE_*` 环境变量 < 显式覆盖。
因此 Compose 用哪种模式，决定了 **谁能真正生效**：

| 模式 | 文件 | 谁生效 | 适用 |
| --- | --- | --- | --- |
| 环境模式 | `compose.yml` | 固定的 `CONTROL_PLANE_*` 环境变量**覆盖**任何文件 | 单栈、简单、一切走 `.env` |
| 文件模式 | `compose.file.yml` | 只注入网络/DB/socket 等文件无法表达的接线，策略由 TOML 文件拥有 | 需要用一个 TOML 统一管理策略 |

关键事实（不要被"统一配置"的说法误导）：**环境变量总是覆盖文件。** `compose.yml` 里每个
`CONTROL_PLANE_SLOTS`、`CONTROL_PLANE_LEASE_TTL_SECONDS` 等固定值都会盖掉同名文件键。
所以不要既用 `compose.yml` 又指望 `CONTROL_PLANE_CONFIG` 里的策略生效——那样只有未被
`compose.yml` 显式设置的键才来自文件。

### 环境模式（默认）

```powershell
Copy-Item runtime-host/deploy/.env.example runtime-host/deploy/.env
# 编辑 .env：随机 token/pepper、已有应用镜像、已构建的前端目录。
docker compose --env-file runtime-host/deploy/.env -f runtime-host/deploy/compose.yml config --quiet
docker compose --env-file runtime-host/deploy/.env -f runtime-host/deploy/compose.yml up -d --build
```

### 文件模式（TOML 统一配置）

```powershell
docker compose --env-file runtime-host/deploy/.env -f runtime-host/deploy/compose.file.yml config --quiet
docker compose --env-file runtime-host/deploy/.env -f runtime-host/deploy/compose.file.yml up -d --build
```

文件模式需要 `.env` 至少提供：`TUTORLM_CONFIG_FILE`（TOML 的绝对路径，只读挂载到
`/etc/tutorlm/control-plane.toml`）、`TUTORLM_INSTANCE_IMAGE`、`TUTORLM_FRONTEND_DIR`、
`TUTORLM_STACK_NAME`，以及 `TUTORLM_CP_ADMIN_TOKEN` / `TUTORLM_CP_TOKEN_PEPPER`。
TOML 结构见仓库根 `config.example.toml`，字段说明见 `../../docs/configuration.md`。

> 这里刻意**不使用** `!reset` / 覆盖合并语法：它需要较新的 Compose CLI，无法在任意
> Compose v2 上假定可用。两个独立文件在任意版本上都能验证。

## 邀请码与分享（运维要点）

- `CONTROL_PLANE_INVITATION_ONLY=true` 时，新访客必须出示有效邀请码。
- **系统码**（`TLM-SYS-v1-<43 base64url>`）用 CLI 生成并入库：
  `python -m control_plane invitations create --label ops`（明文只回显一次）。
  也可在受保护的 TOML/环境中用 `system_invitation_codes` /
  `CONTROL_PLANE_SYSTEM_INVITATION_CODES` 预置，启动时种入；已撤销的码不会复活。
- **分享码**（`TLM-SHARE-v1-<43 base64url>`）由项目所有者对自己的项目签发，只用于导入副本。
- `TUTORLM_INVITE_CODE`（透传给容器的 `TUTORLM_INVITE_CODE`）是**应用自己的体验码**，
  与控制面系统码/分享码无关，也不决定准入。
- 分享/克隆复制卷需要一个**本机已存在**的 helper 镜像（`CONTROL_PLANE_COPY_HELPER_IMAGE`，
  留空回退到实例镜像）。控制面绝不隐式拉取该镜像。

## 验证与环境

需要 Linux Docker daemon（Windows 使用 Linux containers）和已有应用镜像。
默认模板中的前端目录只是占位，没有实际页面；先交付静态制品再启动网关。
镜像标签由应用团队约定；模板中的 `tutorlm-app:local` 是示例，需要自行构建或提供。

开发部署只绑定 `127.0.0.1`。改成公网入口前须自行完成 TLS、Cookie、密钥和权限评审。
控制面需要 Docker socket，Compose 以 root 运行以兼容该 socket；这是高权限服务。
不要把这组配置作为低权限安全隔离保证。

管理接口 `/control/admin/*` 由网关直接拒绝公网访问（`../gateway/nginx.conf`）。
运维请从本地控制面入口（`127.0.0.1:${TUTORLM_CONTROL_PLANE_PORT}`）带 `X-Admin-Token` 访问，
不要为了远程管理放开网关的 admin 路由。

每位开发者设置唯一 `TUTORLM_STACK_NAME`、端口和随机 token/pepper。
这同时隔离网络名、实例前缀、资源归属标签、控制面卷和租约 Cookie 名。
不同开发环境不共用 `.env`、运行数据或资源归属标签。

停止外层服务前先通过租约或管理 API 回收动态实例。
Compose 不直接管理控制面动态创建的容器；仅停止 Compose 不等于所有访客实例已被删除。
不要使用全局 prune 或批量删卷清理其他人的资源。