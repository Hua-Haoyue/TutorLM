# Instance Runtime v1

状态：控制面与应用团队协作使用的运行接口约定。

适用范围：不指定 `workspace_id` 的匿名临时租约。持久项目使用 `instance-runtime-v2.md`，
其数据卷不随计算租约回收删除。

## 交付与隔离

- 应用团队提供同一份不可变 Linux 应用镜像，外层可以通过镜像标签或 digest 配置。
- 每个槽位独立容器、独立具名数据卷，默认应用端口 `8000`，数据挂载 `/data`。
- 应用不得发布宿主机端口；只通过控制面配置的 Docker 内网访问。
- 实例没有 Docker socket，也不访问控制面 SQLite。控制面不访问实例业务 SQLite。
- 容器名由外层构造为 `<prefix>-s<slot:02d>-g<generation:04d>`。
- 回收旧访客后，删除旧容器和数据卷，再递增 generation 才能重新分配。

## 就绪与生命周期

控制面在 Docker 内网调用 `GET /health/ready`，不携带应用 Bearer。
必须返回 HTTP 200，且 JSON 包含 `{"status":"ok"}` 才能对访客开放。
健康状态不可只由容器进程是否 running 推断。

容器应处理停止信号，允许控制面执行有宽限时间的 stop；任何清理失败的槽位进入隔离，
不可把残留数据交给下一个访客。

`/internal/status`、带鉴权的 drain、全实例活动任务探针目前均不属于已实现契约。
现行控制面使用保守活动探针，不因缺少 HTTP 请求就判定实例空闲。

## 配置透传

`CONTROL_PLANE_INSTANCE_ENV_<SUFFIX>` 在外层转换成应用的 `TUTORLM_<SUFFIX>`。
已使用的配置包括：

| 应用变量 | 含义 |
| --- | --- |
| `TUTORLM_RUN_MODE` | `demo` 或应用支持的其他运行模式 |
| `TUTORLM_INVITE_CODE` | 应用体验码，不是租约令牌 |
| `TUTORLM_DATA_DIR` | 默认 `/data` |
| `TUTORLM_PROVIDER` | `fake` 或 `openai_compatible` |
| `TUTORLM_MODEL_API_BASE` / `TUTORLM_MODEL_API_KEY` | 仅服务端使用的模型配置 |
| `TUTORLM_MODEL_NAME` / `TUTORLM_MODEL_MAX_TOKENS` | 模型与输出限额 |
| `TUTORLM_MODEL_TIMEOUT_SECONDS` | 模型请求超时 |

配置透传不是把密钥提供给浏览器。业务配额和模型调用由实例内实现。

## 会话与前端制品

应用自己维护 `/api/sessions` 与 Bearer 会话，控制面不伪造应用 token。
`lease_id`、`slot_id`、`generation` 和 `session_id` 不可相互替代。

应用团队另外提供完整静态前端目录，由网关只读挂载；当前页面入口为 `index.html`、
`busy.html`、`workspace.html`。前端业务 `/api/*` 请求经过同源网关，
同时携带租约 Cookie 与应用 Bearer。前端实现仍归 `instance-app/frontend/`。
