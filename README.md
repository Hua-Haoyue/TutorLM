# TutorLM

容器外调度与容器内业务独立开发，通过版本化 HTTP 契约集成。

```text
runtime-host/                 容器外团队
  control-plane/              用户、持久项目、邀请码准入、临时计算租约与测试
  gateway/                    Nginx 网关
  deploy/                     新结构下的外层部署入口
instance-app/                 容器内团队，含前端租约生命周期与邀请码入口模块
  backend/                    业务 API、Runtime Host、插件、任务、存储
  frontend/                   学习工作台与浏览器端接入逻辑
  deploy/                     应用镜像构建归属
contracts/                    两侧共享的版本化协议，不放共享业务实现
docs/                         架构与协作流程
scripts/                      本地开发与架构边界检查
reference/                    本地参考材料，不纳入 Git
```

这里的“容器外”指相对于访客应用容器；控制面自身仍可以容器化部署。
`backend/host` 属于 **instance-app**，不是外层调度服务。

## 本地开发

Python 3.11+，以下命令在仓库根目录执行：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r runtime-host/control-plane/requirements-dev.txt
.\.venv\Scripts\python.exe -m pytest runtime-host/control-plane/tests -q
.\.venv\Scripts\python.exe scripts/check-boundaries.py
node --test instance-app/frontend/tests/*.test.js
.\scripts\dev-runtime-host.ps1
```

开发脚本在 `127.0.0.1:8090` 启动 fake adapter 的控制面，无需 Docker 或模型密钥。
`/docs` 可查看接口，`/control/capacity` 可查看容量。fake adapter 不会启动真实应用，
因此这个模式用于独立开发调度逻辑，不提供学习工作台。

控制面配置可用 TOML 文件叠加环境变量，见
[docs/configuration.md](docs/configuration.md) 与仓库根的 `config.example.toml`；
CLI 用法见 [docs/cli.md](docs/cli.md)。

Docker 联调见 [runtime-host/deploy/README.md](runtime-host/deploy/README.md)。
容器内业务应用与完整工作台尚未实现；现有前端模块（租约生命周期、邀请码入口）需在工作台中接入。
不能将当前仓库视为完整应用交付物。

## 协作入口

- [架构与责任边界](docs/architecture.md)
- [多人并行开发流程](docs/development.md)
- [持久工作区、邀请码与分享](docs/workspaces.md)
- [控制面配置](docs/configuration.md)
- [控制面 CLI](docs/cli.md)
- [实例运行协议 v1](contracts/instance-runtime-v1.md)
- [持久实例协议 v2](contracts/instance-runtime-v2.md)
- [持久实例协议 v3](contracts/instance-runtime-v3.md)
- [网关与租约协议 v1](contracts/gateway-lease-v1.md)
- [网关与租约协议 v2](contracts/gateway-lease-v2.md)

凭据、本地 `.env`、真实控制面配置文件、数据库、数据卷、日志和镜像不纳入版本控制。
不提交或推送任何内容，直到维护者明确执行 Git 提交与推送。