# Instance Application

本目录属于容器内团队，当前仅预留开发目录，尚未包含应用实现。

预留归属：

- `backend/`：应用 Runtime Host、业务分层、插件和任务管理。
- `frontend/`：学习工作台，以及浏览器端租约和应用会话接入逻辑。
- `deploy/`：应用 Dockerfile、镜像构建与交付脚本，不放网关或控制面。

内层团队可以独立提供镜像和静态前端制品供外层联调；不需要依赖外层源码，
也不需要共享数据库或安装控制面的 Python 包。

接入前先阅读 `../contracts/instance-runtime-v1.md`。
`/internal/status`、可信 drain 与自动租约到应用会话交换尚未实现，不应作为现有能力依赖。
