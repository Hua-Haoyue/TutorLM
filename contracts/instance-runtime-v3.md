# Persistent Instance Runtime v3

状态：持久项目的当前运行契约。v2 描述"持久项目与临时计算分离"；v3 在其上冻结三件事：
**项目数据子目录 + 卷外会话目录**、**就绪声明存储布局**、**服务端卷复制（项目分享/克隆）**。

适用范围：指定了 `workspace_id` 的持久租约。匿名临时租约仍遵循 `instance-runtime-v1.md`。
v1/v2 的其余约定继续有效，本文件只写增量与差异。

> 实现现状：仓库内 `instance-app/backend/` 仍为空目录。v3 是**待应用实现**的契约，
> 控制面侧已按此实现并会校验；不能把本文件当成容器内已具备该能力。

## 数据目录（v3 变更）

v3 把"数据挂载点"和"项目数据目录"分开：

| 变量 | 约定 |
| --- | --- |
| `TUTORLM_DATA_DIR` | **当前项目的数据根**，容器内绝对路径。旧项目等于挂载点；新项目等于 `挂载点/users/<owner_uuid>/workspaces/<workspace_uuid>` |
| `TUTORLM_WORKSPACE_SUBDIRECTORY` | `TUTORLM_DATA_DIR` 相对挂载点的子目录；旧项目为 `.` |
| `TUTORLM_SESSION_DATA_DIR` | **卷外的**临时目录（如 `/tmp/tutorlm-session/sNN`）。应用 session、鉴权 token、缓存等**必须**放这里 |
| `TUTORLM_WORKSPACE_ID` / `TUTORLM_OWNER_ID` | 当前项目 UUID 与所属用户 UUID |
| `TUTORLM_PERSISTENT_WORKSPACE` | `true` |

含义与约束：

- 应用必须把项目业务内容写进 `TUTORLM_DATA_DIR` 之下的稳定路径，不得写进容器可写层或 `/tmp`。
- **应用 session / 凭证不得写进 `TUTORLM_DATA_DIR`。** 因为分享/克隆是**按目录整树复制**，
  卷内任何 session 或凭证都会变成接收者的身份，这是一个安全缺陷而不是便利。
- 子目录只能由控制面用 UUID 推导（`users/<owner>/workspaces/<workspace>`）。
  应用不得接受客户端提交的任意路径，`TUTORLM_DATA_DIR` 之外的写操作不在契约内。
- 兼容：挂载点根目录方式（旧项目，`TUTORLM_WORKSPACE_SUBDIRECTORY="."`）继续受支持，
  旧卷**无需搬迁**即可继续以 v2 行为运行。

## 就绪声明（v3 变更）

`GET /health/ready` 除了 v1/v2 的 `status` 外，持久模式必须返回：

```json
{"status":"ok","storage_mode":"persistent","workspace_id":"<当前项目 UUID>","storage_layout":"content_only"}
```

- `workspace_id` 与控制面注入的不一致时，控制面拒绝把该实例交给访客。
- `storage_layout` 取值：
  - `content_only`：**声明卷内只有项目业务内容**，session/凭证在 `TUTORLM_SESSION_DATA_DIR`。
    只有声明为该布局的项目才允许签发分享码与作为克隆源。
  - 缺省或不返回：控制面按 `legacy_root` 记录，**不允许**作为分享源（拒绝签发分享码）。
- 已初始化（`storage_initialized=1`）的卷缺失时必须报错，不得静默生成空卷冒充原项目。

## 项目分享与克隆（v3 新增）

分享码（`TLM-SHARE-`）由项目所有者签发，接收方导入后得到一个**独立项目副本**：

- 目标项目是**新的** `workspace_id`、**新的**卷、归属于**接收用户**；
  接收用户不能通过分享访问源项目本身，也不能据此获得更高优先级。
- 复制在服务端完成：源容器**先停止/回收计算**，再复制，然后才发布目标项目。
  这是**崩溃一致性**（含已落盘的 WAL/journal），不是应用级热迁移。
  **源项目进行中的任务会被打断**；控制面不声称无损。
- 复制是**目录子树**复制，不是整卷挂载给接收者：显式排除 `sessions`、`session`、`auth`、
  `credentials` 等会话/凭证子树；接收者的容器只挂载自己的卷与自己的数据子目录。
- 失败不发布半成品：目标项目在 `copying` 状态不可分配、不可被存储 GC 回收；
  失败时删除目标卷或标记为不可用，源项目与其卷保持原状。
- helper 镜像必须是**宿主机已存在**的本地镜像，控制面绝不隐式拉取。

## 未实现（不得当作已具备）

- **受信会话交换**：控制面不会代应用签发应用 session，也不会把外层用户身份转成应用身份。
  应用自身的 Bearer 鉴权必须保留。
- 应用级 drain/flush：停止是硬性的计算期限，不等于无损任务迁移。
- 多副本生命周期协调、跨主机备份与高可用。