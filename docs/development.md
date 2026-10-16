# Parallel Development

## 分工与合并顺序

| 工作流 | 主要写入范围 | 独立验证方式 |
| --- | --- | --- |
| 调度与生命周期 | `runtime-host/control-plane/` | fake adapter 单元/API 测试 |
| 网关与外层部署 | `runtime-host/gateway/`、`runtime-host/deploy/` | 配置检查，再用已交付镜像联调 |
| 容器内应用 | `instance-app/backend/`、`instance-app/deploy/` | 应用自己的测试和镜像构建 |
| 工作台前端 | `instance-app/frontend/` | 前端测试，通过契约接入后端 |
| 跨边界集成 | `contracts/`、集成验证与发布文档 | 两侧共同评审 |

持久项目接入以 `contracts/instance-runtime-v3.md` 为准。外层管理项目 UUID、卷、数据子目录、
邀请码与计算租约；内层负责把业务内容保存到 `TUTORLM_DATA_DIR`，把 session 放到
`TUTORLM_SESSION_DATA_DIR`，不得在 session GC 时删除持久项目。

按目录约定负责范围，而不是多人同时重构公共入口。跨层变更先提交契约 PR，
双方确认兼容方式后再并行实现，最后合并集成验证。不要在“共享 utilities”中放业务实现。

## 分支与工作目录

每位开发者使用独立 clone 或 Git worktree，不要共用一个会被同时修改的工作目录。
示例在仓库根执行，分支名仅为建议：

```powershell
git worktree add ..\TutorLM-runtime -b feat/runtime-host
git worktree add ..\TutorLM-instance -b feat/instance-app
git worktree add ..\TutorLM-frontend -b feat/frontend
```

这些命令不会由本次整理自动执行。各工作目录使用独立虚拟环境、端口和运行数据目录。
同一个 Docker daemon 上联调时，各自设置唯一 `TUTORLM_STACK_NAME` 与端口，
避免两个控制面调度同一组标签、容器或数据卷。

## 外层独立开发

根目录 `scripts/dev-runtime-host.ps1` 使用 fake adapter，不需要应用源码或 Docker。
它只模拟容器生命周期与就绪，不能验证真实应用鉴权、数据隔离或模型能力。
它接受 `-ConfigPath`（或已存在的 `CONTROL_PLANE_CONFIG`）：未给文件时注入开发默认值，
给了文件就以文件为准，不再注入这些覆盖。

```powershell
.\.venv\Scripts\python.exe -m pytest runtime-host/control-plane/tests -q
.\.venv\Scripts\python.exe scripts/check-boundaries.py
node --test instance-app/frontend/tests/*.test.js
```

配置分层与 `config.example.toml` 说明见 `configuration.md`；CLI 用法见 `cli.md`。
容器内团队可先交付镜像与静态前端制品，外层无需等待其源码合并即可联调。
真实 Docker 测试仅在明确设置 `CONTROL_PLANE_DOCKER_IT=1` 后运行，
并需要匹配的本地应用镜像和可访问的 Docker daemon（卷复制还需本地已有的 helper 镜像）。

## 提交检查

- 不提交 `reference/`、`.env`、凭据、数据库、卷或镜像归档。
- 不提交真实控制面配置文件（`config.toml` / `*.local.toml` 已被忽略）。
- 外层 PR 通过控制面回归和架构边界检查。
- 应用团队实现业务时补充自己的依赖、测试和 CI。
- 跨层契约改动须同时检查网关、控制面、应用兼容性。
- 不使用会影响其他开发者的 Docker prune 或批量删卷命令。