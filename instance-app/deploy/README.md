# Instance App Deploy

容器内应用镜像与前端制品的构建归属。目录现含 `Dockerfile` 与 `.dockerignore`（**未实际构建、未拉取镜像、
未执行 Docker**；本仓库不自动跑 Docker）。架构设计见
[`../../docs/instance-app-design.md`](../../docs/instance-app-design.md)。

外层负责创建/启动/停止实例容器；本目录只产出**一份不可变 Linux 应用镜像**（可被镜像标签或 digest 引用）
与供网关只读挂载的**静态前端制品**。
应用后端已落 **可运行骨架**（`domain/`、`application/{ports,format}`、`infrastructure/{sqlite,storage,providers}`、
`adapters/http/`、`host/`、`plugins/`）：除格式**契约/端口/适配**（md/txt 抽取、pdf 结构化抽取、tex 工程校验与
受限编译编排、统一 selector 与码点↔UTF-16 换算）外，**HTTP/API 装配、SQLite 存储与会话、fake/openai 兼容
provider 已就位**，本地可用 `python -m backend` 起服务；工作台前端仍按契约接线（见 `../README.md`）。
本仓库**无**安全编译执行器，TeX 编译**默认 `tex_compile_unavailable`**、`fail closed`、无 subprocess 回退。
编译沙箱属**部署基础设施**（见
[`../../docs/tex-compilation-security.md`](../../docs/tex-compilation-security.md)），**不由本目录或应用创建**。

## 镜像设计（不实际构建）

真实文件为 [`Dockerfile`](Dockerfile)：Node 22 阶段安装锁定依赖并导出静态资源，Python 3.12 阶段运行后端。以下示例仅说明后端入口，完整构建以 Dockerfile 为准。

- **非 root**：`groupadd/useradd` 固定 uid/gid `10001`，与控制面卷复制 helper 交出的属主一致。
- **只装声明依赖**：`COPY instance-app/backend /app/backend`，`requirements.txt` **存在时**才 `pip install`。
  backend 现有 `requirements.txt`（声明 `fastapi`/`starlette`/`pydantic`/`uvicorn` 的版本区间）与
  `pyproject.toml`，故该步骤会安装 HTTP 栈；`requirements-dev.txt` 追加 `pytest`/`httpx` 供测试。
  包含 pypdf 抽取库与前端 PDF.js 静态产物，不内置 TeX 工具链或不受限编译器。
- **构建上下文是仓库根**：`docker build -f instance-app/deploy/Dockerfile -t tutorlm-host:<tag> .`；
  因此 `COPY` 用仓库根相对路径（`instance-app/backend`、`instance-app/frontend`）。
  `Dockerfile.dockerignore` 为该 Dockerfile 限定构建输入，排除运行数据、凭据、测试和本地 node_modules。
- **可写面**：仅 `/data` 与 `/tmp/tutorlm-session`，属主 `10001:10001`；`EXPOSE 8000` 仅容器内网。
- **就绪探针**：`GET /health/ready`（非 `/health`）。
- **入口**：`python -m uvicorn backend.adapters.http.app:create_app --factory --host 0.0.0.0 --port 8000`
  （即下方 `ENTRYPOINT`，与 `Dockerfile` **逐字一致**；`--factory` 因为 `create_app()` 是无必填参数的工厂，
  在 `/app` 下由 `PYTHONPATH=/app` 解析 `backend.*`）。本地入口 `python -m backend`（`backend/__main__.py`）
  导入**同一**工厂 `backend.adapters.http.app.create_app`，与镜像入口**指向一致**、二者均可解析；
  `backend.host.app` 模块**不存在**（Host 组合根在 `backend.host.state`，HTTP 装配在 `backend.adapters.http.app`），
  实现与测试均已不再引用它。`backend/__main__.py` 是本地开发入口，镜像入口仍以 `ENTRYPOINT` 为准。
- **未执行真实 Docker**：本仓库不构建、不拉取、不运行镜像（见 `Dockerfile` 头注释）。上述入口为**静态比对**
  结论（`ENTRYPOINT` 与 `Dockerfile` 行级一致），**不是**运行验证。

```dockerfile
FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
RUN groupadd --gid 10001 app && useradd --uid 10001 --gid app --create-home --shell /usr/sbin/nologin app
WORKDIR /app
COPY instance-app/backend /app/backend
RUN if [ -f /app/backend/requirements.txt ]; then python -m pip install --no-cache-dir -r /app/backend/requirements.txt; fi
COPY instance-app/frontend /app/static
RUN mkdir -p /data /tmp/tutorlm-session && chown -R 10001:10001 /data /tmp/tutorlm-session
USER 10001:10001
EXPOSE 8000
STOPSIGNAL SIGTERM
HEALTHCHECK --interval=10s --timeout=3s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health/ready',timeout=2).status==200 else 1)"
ENTRYPOINT ["python", "-m", "uvicorn", "backend.adapters.http.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
```

`.dockerignore` 见 [`.dockerignore`](.dockerignore)：排除 `.env`/密钥/数据库/测试/文档/`reference/`/外层目录，
避免把凭据或外层源码打进镜像。注意构建上下文为仓库根时，Docker 只读取 **上下文根** 的 `.dockerignore`；
若要让本目录的忽略文件生效，请用 BuildKit 的 `deploy/Dockerfile.dockerignore` 约定，或将其复制到上下文根。

## 运行加固（由控制面按 v3 施加，应用需配合）

控制面创建实例时施加（外层已实现侧）：

- `read_only=true` 根文件系统；`cap_drop=["ALL"]`；`no-new-privileges:true`；`init=true`。
- `tmpfs={'/tmp': 'size=<tmpfs_size>,mode=1777'}`：应用会话目录 `TUTORLM_SESSION_DATA_DIR`
  （如 `/tmp/tutorlm-session/sNN`）位于该 tmpfs，**卷外、不落盘、不随卷复制**。
- 资源：`mem_limit`（默认 `2g`）、`nano_cpus`（默认 `2.0`）、`pids_limit`（默认 `256`）。
- 具名卷挂到 `instance_mount_path`（默认 `/data`）；**无 Docker socket、无宿主机端口发布**。
- `stop_grace_seconds`（默认 30s）配合 `STOPSIGNAL SIGTERM`，应用需优雅关停收尾。

镜像因此必须：根文件系统只读也能运行（只写卷与 tmpfs）；以非 root `10001` 运行；
不尝试发布端口；不挂 Docker socket。

## 编译执行器不在应用镜像内（边界）

- **应用镜像不内置 TeX 工具链，也不调用编译器**：它只向**独立、受信的编译执行器**提交受限任务。
  这是为满足"编译与 model 不共享 execution environment/secrets"的要求——清 env **不是**安全沙箱。
- **执行器由部署基础设施预置、独立管理、每次构建全新 sandbox**（无网络、只读快照、低权、资源 bounded、
  rootless/命名空间隔离）；沙箱的**创建者与生命周期**是部署基础设施职责，**不是**用户控制面或 App 业务职责。
- 即便**同 Pod/sidecar** 部署，也必须保持独立的 FS/proc/cap 边界；**不能**仅因"清了 env 的 App 子进程"
  就宣称安全。**未部署安全执行器时**，编译端点返回 `503 tex_compile_unavailable`，**不做 unsafe fallback**。
- 只读根、`cap_drop=ALL`、`no-new-privileges`、tmpfs 会话（§运行加固）继续由控制面按 v3 施加；应用配合，不自行放宽。

## 非 root（10001）写入权初始化

新持久卷由控制面创建时卷根属 **root**，容器以 `10001` 运行，首次写入 `TUTORLM_DATA_DIR`
会 `EACCES`。**非 root 不能凭空可写**，属主必须由有权限的一方显式交出：

- **首选**：控制面在挂载/初始化新卷时用一次性 helper（root + `CAP_CHOWN`）把数据根交给 `10001:10001`
  （外层卷复制 helper 已是此模式：`COPY_OWNER_UID=10001`）。
- **备选**：镜像 root 入口脚本仅在首次初始化时 `chown` 数据根后 `exec gosu 10001`
  （与 `USER 10001` 冲突，需评审）。
- 应用侧**只在启动/初始化时探测可写性**：不可写则就绪非 `ok` 并给出可操作原因，
  **不**降级写到容器可写层或 `/tmp`。应用内不做 `chown`（无 `CAP_CHOWN`）。

## 交付与卸载

- 镜像构建后在受控环境按 digest 记录；应用团队可先交付镜像与静态前端制品供外层联调，
  无需等待外层源码合并。
- 镜像与静态前端制品**不提交**到仓库（`.gitignore` 已忽略 `dist/` 等构建物）；不提交 `.env`、密钥、测试数据。

## 测试

```powershell
# 镜像构建与冒烟（实现后；本仓库不自动执行）
# docker build -f instance-app/deploy/Dockerfile -t tutorlm-host:<tag> .
```

- 真实 Docker 集成测试**显式 opt-in**；普通开发用 fake runtime/provider，无需 Docker 与模型密钥。
- 就绪探针应为 `GET /health/ready`（非 `/health`），持久模式须含项目绑定字段，见设计 §8。
