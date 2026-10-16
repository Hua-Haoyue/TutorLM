# TeX 编译执行安全设计（CompilerAdapter）

> 状态：**设计文档，尚未实现**。本轮只落盘设计：未执行真实 Docker、未构建或拉取镜像、
> 未访问远程基础设施、未改动 `contracts/` / `runtime-host/` / `sync/` 与既有前端模块、未提交。
> 未实测任何编译器/沙箱行为。文末官方来源为官方站点链接，其内容由命令行 HTTP 抓取（`curl`/`Invoke-WebRequest`，非浏览器、非可视化核对）比对；**未经本次联网可视化核实，亦未做版本级核对**，不代表本项目已验证。
> 依据：`instance-app-design.md`、`instance-app-api.md`、`../contracts/instance-runtime-v1/v2/v3.md`、
> `document-readers.md`，以及文末官方一手来源。

## 0. 核心立场

**永远把 TeX 编译器当作不信任进程执行。** 任何“配置安全选项即可放心跑不可信输入”的假设都是错的：

- `env` 清理、`--no-shell-escape`、`openin_any=paranoid`、`openout_any=paranoid` 等
  都只是**纵深防御（defense in depth）**，**不是沙箱**，不能单独承载安全边界。
- TeX 源在编译期可读文件路径、可写输出；`\input`/`\openin` 可触达编译器可访问的任意 host 路径，
  因此**输入隔离（isolation）比过滤标志更重要**。
- 由此推出本文的总体架构（§1）；过滤、标志、白名单是补充，不替代隔离。

## 1. 架构与所有权

```text
用户/前端 --> 应用(Host) --受限 RPC 任务--> 受信编译执行器(独立基础设施)
                 |                               |
                 |                               +-- 每次构建全新 sandbox（无网络）
                 +-- 只给 inputs / 收 artifacts    +-- 预置工具链镜像（digest 固定）
```

规则（与 `docs/architecture.md` 边界一致）：

- **应用任务服务持有模型密钥**（`TUTORLM_MODEL_API_KEY` 等只从 env 读，见 `instance-app-design.md` §7）；
  编译执行器**不持有**任何模型密钥或用户凭据。
- **禁止在同一环境里直接编译**：应用容器**不**内置/调用 TeX 编译器；它只发起受限任务。
- **应用只提交受限任务格式**：`inputs`（源与本地资源）+ 期望产物类型；
  **不允许任意 shell 命令**、不允许指定可执行文件或命令行。任务格式是**声明式**的。
- **应用不能挂 socket**：容器无 Docker socket（`instance-app-design.md` §9.3）；执行器边界不由应用创建。
- **Control Plane 不做业务编译调度**：它负责身份/租约/卷（见 `architecture.md`）；TeX 编译是应用侧业务，
  执行器是**应用与部署基础设施之间**的独立组件，**不由 Control Plane 代管**。
- **执行器由部署基础设施预置、独立管理**：受信、专用、最小权限（rootless），
  **不向应用暴露 host root 或 Docker API**。

## 2. 沙箱创建（由受信执行器负责）

沙箱为**每次构建全新创建**，构建结束即销毁；配置由执行器（受信）控制，输入由应用（不信任）提供。

### 2.1 隔离基线

- **无网络**（network namespace `none`/无出口）；禁止任何出网请求。
- **无宿主可写敏感面**：不挂 host home、不挂 `/proc` 敏感项、不挂可用凭据；
  原始项目快照**只读**挂入。
- **无密钥**：环境无 secret；即便编译器被利用也拿不到密钥（见 §5）。
- **fresh scratch**：每次构建独立可写 scratch（tmpfs/临时目录），构建后随沙箱销毁，
  不跨构建复用。
- **用户命名空间隔离**：rootless（如 `bubblewrap --unshare-all`，其中含 `--unshare-net`/`--unshare-pid` 等，
  见来源）；不获得 host root。

### 2.2 资源与内核限制

- 限制 **CPU、内存、pids、打开文件数、输出磁盘配额、挂钟时间、文件描述符上限**。
- 采用 seccomp 过滤系统调用、只读根文件系统、丢弃 capabilities、`no-new-privileges`、
  独立 PID/IPC/UTS/mount 命名空间（见来源）。
- 所有限制由**沙箱创建方**（受信执行器）设定；任务输入不得修改这些限制。

### 2.3 为什么过滤标志不够

官方文档说明：

- `-no-shell-escape` / `-shell-restricted` 只是启用/禁用/受限 `\write18`（见 Web2c 手册）；
  受限模式依赖 `shell_escape_commands` 白名单，**白名单仍可能被滥用**，官方明确建议
  “在 CGI/wiki 等不可信输入环境里应**完全禁用** shell escape”（见来源）。
- `openin_any`/`openout_any` 的 `paranoid` 只约束**输出文件名**是否落在允许目录，
  且 Kpathsea **不解析 `..` 或符号链接**（按字符串处理），因此它**不是路径沙箱**（见来源）。
- 编译器可读任意可访问 host 路径 => 关键在**把 host 路径暴露面降到最低**（只读、最小快照）。

结论：**输入隔离优先**；标志与过滤为**纵深防御**，不是主防线。

## 3. 输入处理

- **输入隔离优先于过滤**：只把当前项目所需的**只读快照**与显式列举的本地资源放进沙箱。
- **禁止 zip/path/symlink bombs**：解包与目录遍历必须防 zip bomb、路径穿越、符号链接逃逸；
  拒绝超出配额的条目与异常嵌套。
- **文件名严格校验**：限制字符集/长度/层级；拒绝 `..`、绝对路径、保留名；
  **主输入（main input）显式校验**（存在、可读、在允许目录内、类型正确）。
- **`\include`/`\includegraphics` 只用快照内本地资源**：资源在构建前从快照解析并复制到 scratch，
  编译期不再触达快照外路径。
- **禁止远程拉包与运行时安装**：不允许 `\input{http...}` 之类的取数路径，
  不允许编译期调用包管理器/联网安装。
- **预装包与字体可复现**：工具链镜像内的 packages/fonts **预置且以 digest 固定**，
  构建输入与工具链摘要共同构成 `build_id`（见 §6）。不承诺完全 TeX 兼容。

## 4. 编译适配器与编译器中立

- 定义 **`CompilerAdapter`** 接口，运行**受控镜像**；首版**不锁定具体编译器**，
  供后续实现选型，避免无依据的承诺。

```ts
interface CompilerAdapter {
  // 由受信执行器实现；应用只提供声明式任务
  submit(task: CompileTask): Promise<BuildHandle>;
  cancel(handle: BuildHandle): Promise<void>;
  // 产物经校验后从受控出口取回
  fetchArtifact(handle: BuildHandle): Promise<VerifiedArtifact>;
}

CompileTask = {
  document_id: string,
  revision_id: string,     // 源版本（见 document-readers.md §2.1）
  inputs: InputRef[],      // 只读快照内的源与本地资源
  output: 'pdf',           // 期望产物类型；不指定编译器/命令
}
```

- 后续若选择某个具体编译器（如 Tectonic），**必须仍运行在安全沙箱内**；
  编译器自身的 “untrusted” 模式**不是安全解释器**，只是关闭已知危险特性（见 §4.1）。
- 若部署**尚未提供安全执行器**：接口返回 **`503 tex_compile_unavailable`**，
  **绝不**回退到“同环境直接编译”或“关闭沙箱”。

### 4.1 具体编译器的定位（若要选）

- **Tectonic**：官方 `--untrusted` 会禁用已知不安全特性（如 `-Z shell-escape`），
  并可用 `TECTONIC_UNTRUSTED_MODE` 强制；但官方也明确“不可信脚本可清除该环境变量”从而绕过，
  说明它**不是沙箱**（见来源）。因此 Tectonic 只能作为**沙箱内的编译器**，不是安全边界。
- **`pdflatex` / `xelatex` / `lualatex`（TeX Live/Web2c）**：
  - **推荐先限制在 `pdflatex` 或 `xelatex`**，使用**受限引擎 + 允许名单**，而非一上来开放全部引擎。
  - **`lualatex`/LuaTeX 扩大风险面**：Lua 是通用脚本语言，官方说明 LuaTeX 是特例；
    较新版本默认禁用 socket 库，但 `--socket`（以及 `--shell-escape`）会重新打开（见来源）。
    因此默认**不启用 `lualatex`**，如需启用需单独评估。
  - 无论哪个引擎：查官方 `-no-shell-escape`/`-shell-restricted` 的语义并用之，
    但仅当作**纵深防御**；同时设置 `openin_any`/`openout_any` 等为**额外**约束（非主防线）。

## 5. 输出、日志与密钥

**产物与日志同样不可信**（编译器可能被利用而伪造输出）：

- **大小与类型校验**：对 PDF 做 size 上限与 magic/结构校验；只接受期望的 PDF。
- **日志处理**：脱敏（去 secret/路径），去除控制字符（防终端注入），
  严禁 dump 环境变量；即使编译器被利用也**不得**泄出任何 secret（沙箱内本就无 secret）。
- **不落敏感**：应用侧日志不打印模型密钥、token、恢复凭据。

## 6. 版本、取消与发布

- **文件版本 = hash(build)**：产物以内容摘要标识；`revision_id`（源）与
  `artifact_id`/`build_id`（产物）严格区分，**不得互换**（对齐 `document-readers.md` §2）。
- **stale publish 保护**：若在构建期间源已推进（出现更新 `revision_id`），旧构建的产物
  **不覆盖**已发布的更新产物；**保留最后一次成功**。
- **hard cancel**：取消必须**杀进程组/子树**、**清理 sandbox**，并**拒绝迟到的发布**
  （late publish）；取消后不写入产物、不留半成品。
- **安全执行器未部署**：返回 `503 tex_compile_unavailable`，**不做 unsafe fallback**。

## 7. 验收用例（设计判据，未执行）

| 用例 | 期望 |
| --- | --- |
| 数据外泄（exfiltration） | 无网络/无 secret，编译无法把内容送出 |
| `/proc` 与环境访问 | 敏感项不可读；环境无 secret |
| 网络（network） | namespace 无出口，任何出网尝试失败 |
| zip/path/symlink bomb | 拒绝；不逃逸到快照外或宿主 |
| 超时（timeouts） | 超挂钟/CPU 限额即终止并清理 |
| 产物 symlink | 产物出口拒绝符号链接/越界路径 |
| 日志敏感（logs sensitive） | 日志脱敏、去控制字符、无 env dump |
| hard cancel | 杀进程组、清 sandbox、拒绝 late publish |
| stale publish | 保留最后一次成功，旧产物不覆盖新源 |
| 未部署执行器 | 返回 `503 tex_compile_unavailable`，无 unsafe fallback |
| 浏览器引用稳定性 | 产物 PDF 在阅读器内锚点可复核（见 `document-readers.md` §6） |

## 8. 设计 vs 已完成

- **设计（本文）**：`CompilerAdapter` 接口、受限任务格式、沙箱基线、输入/输出处理、取消与发布语义。
- **已完成**：无。本文不含任何已实现能力；控制面与应用侧目前**均无**安全编译执行器。
- 未部署安全执行器时的行为是**设计约定**：返回 `503 tex_compile_unavailable`。

## 9. 事实边界与官方来源

- 本文为**设计**，未实测任何编译器/沙箱；未改动其他文件。
- 相对链接：`document-readers.md`、`instance-app-design.md`、`instance-app-api.md`、
  `../contracts/instance-runtime-v3.md`。

官方一手来源（纯链接）：

- Web2c 手册（`-shell-escape`/`-no-shell-escape`/`-shell-restricted`、`\write18`、`shell_escape_commands`，
  以及“不可信输入应完全禁用 shell escape”）：https://tug.org/texinfohtml/web2c.html
- Kpathsea 手册（`openin_any`/`openout_any`/`paranoid`、不解析 `..` 与符号链接、LuaTeX 特例与 socket 库）：
  https://tug.org/texinfohtml/kpathsea.html
- TeX Live 指南（安全注意事项、不可信输入用新子目录/chroot、LuaTeX socket 默认禁用与 `--socket`）：
  https://tug.org/texlive/doc/texlive-en/texlive-en.html
- Tectonic `-X compile`（Security 段：`--untrusted`、`TECTONIC_UNTRUSTED_MODE` 及其可被清除的局限）：
  https://tectonic-typesetting.github.io/book/latest/v2cli/compile.html
- bubblewrap（`--unshare-all`/`--unshare-net`、`--die-with-parent`、`--ro-bind`、`--tmpfs`、`--clearenv`、
  `--cap-drop`、`--new-session`、`--as-pid-1`）：https://github.com/containers/bubblewrap/blob/main/bwrap.xml
- Docker Engine 安全（rootless）：https://docs.docker.com/engine/security/rootless/
- Docker seccomp 安全 profile：https://docs.docker.com/engine/security/seccomp/
- Linux namespaces：https://man7.org/linux/man-pages/man7/namespaces.7.html
- Linux seccomp：https://man7.org/linux/man-pages/man2/seccomp.2.html
