# Outer Deployment

本目录只构建控制面和配置网关。应用镜像由 `instance-app/deploy/` 团队提供，
前端制品由 `instance-app/frontend/` 团队提供。
所有实例统一由控制面按租约动态创建，Compose 只声明外层服务。

在仓库根目录执行：

```powershell
Copy-Item runtime-host/deploy/.env.example runtime-host/deploy/.env
# Edit .env: set independent random tokens, invite code, existing image and built frontend directory.
docker compose --env-file runtime-host/deploy/.env -f runtime-host/deploy/compose.yml config --quiet
docker compose --env-file runtime-host/deploy/.env -f runtime-host/deploy/compose.yml up -d --build
```

需要 Linux Docker daemon（Windows 使用 Linux containers）和已有应用镜像。
默认模板中的前端目录只是占位，没有实际页面；先交付静态制品再启动网关。
镜像标签由应用团队约定；模板中的 `tutorlm-app:local` 是示例，需要自行构建或提供。

开发部署只绑定 `127.0.0.1`。改成公网入口前须自行完成 TLS、Cookie、密钥和权限评审。
控制面需要 Docker socket，Compose 以 root 运行以兼容该 socket；这是高权限服务。
不要把这组配置作为低权限安全隔离保证。

每位开发者设置唯一 `TUTORLM_STACK_NAME`、端口和随机 token/pepper。
这同时隔离网络名、实例前缀、资源归属标签、控制面卷和租约 Cookie 名。
不同开发环境不共用 `.env`、运行数据或资源归属标签。

停止外层服务前先通过租约或管理 API 回收动态实例。
Compose 不直接管理控制面动态创建的容器；仅停止 Compose 不等于所有访客实例已被删除。
不要使用全局 prune 或批量删卷清理其他人的资源。
