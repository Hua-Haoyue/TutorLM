# TutorLM

面向长文档学习的可追溯工作台：在原文旁阅读、提问、拆解问题，并把理解过的内容组织成一张可以回到出处的知识图。

[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Instance App CI](https://github.com/namenotfound64/TutorLM/actions/workflows/instance-app.yml/badge.svg)](https://github.com/namenotfound64/TutorLM/actions/workflows/instance-app.yml)
[![Runtime Host CI](https://github.com/namenotfound64/TutorLM/actions/workflows/runtime-host.yml/badge.svg)](https://github.com/namenotfound64/TutorLM/actions/workflows/runtime-host.yml)

在阅读区选中原文，创建可追溯的解释节点；在节点之间建立关系，按根到叶整理知识图；用离线 Provider 本地开发，也可以接入 Gemini 或 OpenAI 兼容模型。

[在线工作台](http://stellaron.link:47823/) · [快速开始](#快速开始) · [功能](#你可以做什么) · [文档格式](#多格式阅读) · [模型接入](#接入真实模型) · [开发测试](#测试与质量检查) · [文档入口](#文档入口)

TutorLM 将浏览器工作台、容器内应用和容器外控制面分开。应用负责文档、节点、解释、任务、模型和存储；控制面负责用户、持久项目、邀请码、计算租约和实例生命周期。两侧通过版本化 HTTP 契约集成，不共享私有模块或数据库。

https://github.com/user-attachments/assets/6813d775-f34b-4f83-adbd-158cc1ecb264

## 你可以做什么

### 原文旁提问

导入 Markdown、TXT 或 PDF，选中一段文字生成知识节点。节点保存原文引用、文档版本和上下文，可以从右侧卡片返回原文对应位置。

### 把一个问题拆成分支

在解释中继续选词或选句，创建子节点。父子关系保留追问路径，后续解释可以沿着这棵树继续展开，而不是把问题和答案散落在聊天记录里。

### 选择解释方式

设置中可以选择默认、通俗、严谨、多举例、类比和引导式解释。严谨模式会要求模型说明上下文关系、前提条件以及原文省略的数学推导；通俗和类比模式使用分层的简化策略。

偏好设置提供模型思考强度和联网搜索开关，按当前 Provider 的能力启用。联网默认关闭；开启后可以为解释补充带来源的检索资料。显式提高思考强度会增加输出 token 预算，可能增加延迟和费用，不会显示模型的私有思维链。

### 整理知识图

工作台支持缩放、平移、手动拖动和自动布局。自动布局按根到叶、从左到右排列节点，并通过动画展示位置变化。AI 关系梳理会先提出新增、修改或删除连线的建议，用户确认后才会写入。

自动布局保留跨层连线的绕行路径并调整同层节点顺序以减少交叉；复杂关系图不保证零交叉。解释卡片放大和缩小都有过渡动画，连线随卡片尺寸同步更新，并遵循减少动态效果的系统偏好。

节点支持手动布局、撤销和恢复默认位置；在启用 `card-clear` 插件时，还可以框选或清空当前文档的卡片，并在有效期内撤销删除。Feynman 检查作为独立的服务端学习流程运行，不会把检查结果伪装成解释。

### 编辑和清理学习卡片

Markdown 视图支持按块原地编辑。编辑会形成新的文档版本，并同步移动可验证的文本来源；已有解释锚定的内容不能直接改写，避免节点悄悄指向错误原文。卡片支持放大、恢复默认位置、拖动撤销，以及框选删除、清空当前文档和撤销删除。

### 优化解释和检查推理

优化解释会读取父节点和已解释的子节点，只把本次新增理解合并到相关语句，保留原解释和历史版本。Feynman 检查以独立任务检查前提、推理方向和结论，不把检查过程当作普通解释写入知识卡片。

### 多格式阅读

| 格式 | 支持内容 | 边界 |
| --- | --- | --- |
| Markdown / TXT | 上传、版本、搜索、选区、来源定位、表格、列表、代码块、引用和 KaTeX 数学公式 | 原始 HTML 不执行，远程图片不加载 |
| PDF | 原件保存、文本抽取、PDF.js 原页渲染、文本层、搜索、选区和高亮 | 扫描 PDF 没有 OCR |
| TeX | 单文件和 ZIP 工程校验、源码 Reader、源码引用 | 安全编译执行器尚未交付，编译返回 503 |
| 代码 | Code 高亮视图、编辑保存、Tab 标识符补全与缩进、ZIP 多文件导入、版本化静态索引和项目内搜索 | 仅 UTF-8 文本，不执行代码；已有来源节点的文件不能整体替换 |

### 代码工程与工作区 Chat

上传代码会自动建立文件与符号索引；针对代码节点生成解释时，会检索项目内相关文件并保留路径、行号和版本信息。PDF 和普通文档默认不检索代码，偏好设置可以显式开启或关闭。索引是静态文本与符号检索，不运行构建、语言服务器或项目代码。

工作区选择器右侧的 Chat 按钮打开可拖动、调整大小的浮动对话卡片。助手可以读取当前项目的代码、文档和解释节点，按联网开关搜索公开资料，并提出文件或节点修改。写入必须经用户确认，应用前重新检查版本；禁止运行代码、Shell、SQL、安装依赖或访问任意主机文件。会话历史按应用 session 隔离。

原生联网支持 Gemini 和明确支持搜索的 OpenAI 兼容模型；可配置思考强度支持 Gemini 2.5/3 和兼容接口的 o1/o3/o4/GPT-5 系列，实际能力还取决于服务端模型及网关。详细接口、导入限制及安全边界见 [工作区工具](docs/workspace-tools.md)。

### 持久项目与分享

接入控制面后，可以创建多个持久项目。分享使用一次性的字符串，接收者得到独立副本，不会共享源项目的卷或业务数据。分享码默认有效期为 **7 天**，可由控制面配置覆盖；撤销或过期后不能继续导入。计算租约结束会回收实例，但项目数据按控制面保留策略管理。

### 插件与工作流

当前应用内置 `documents`、`explanations`、`knowledge-narration` 和 `card-clear` 四个插件。主要工作流包括 `explanation.v1`、`graph_relations.v1`、`narration.v1` 和 `retelling.v1`；Feynman 检查和卡片清理由应用内服务及插件提供。插件是随镜像构建的可信代码，不是用于执行任意用户代码的沙箱。

## 快速开始

### 1. 准备环境

要求 Python 3.11+、Node.js 22+。在仓库根目录执行：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r instance-app/backend/requirements-dev.txt
.\.venv\Scripts\python.exe -m pip install -r runtime-host/control-plane/requirements-dev.txt
Push-Location instance-app/frontend
npm ci
Pop-Location
```

### 2. 启动独立工作台

独立模式不需要 Docker、模型密钥或远程服务，应用使用离线 `fake` provider：

```powershell
.\scripts\dev-instance-app.ps1
```

它会在 `127.0.0.1:8091` 启动应用后端。另开一个终端启动工作台：

```powershell
.\.venv\Scripts\python.exe instance-app/workbench/scripts/dev-server.py --port 8092 --api http://127.0.0.1:8091
```

打开 `http://127.0.0.1:8092/`，输入本地体验码 `TLM-DEV-LOCAL`。这个模式适合验证阅读、节点、解释和前端交互；离线回答不代表真实模型质量。

### 3. 启动控制面开发模式

如果只需要调度、租约和槽位逻辑：

```powershell
.\scripts\dev-runtime-host.ps1
```

控制面默认监听 `127.0.0.1:8090`，使用 fake runtime adapter，不启动真实应用容器。

## 接入真实模型

应用支持以下 Provider：

| Provider | 说明 |
| --- | --- |
| `fake` | 离线、确定性结果，用于本地开发和回归 |
| `openai_compatible` | OpenAI Chat Completions 兼容接口 |
| `gemini` | Gemini 原生 `generateContent` 接口 |

真实模型配置只应通过服务端环境变量提供：

```text
TUTORLM_PROVIDER=gemini
TUTORLM_MODEL_API_BASE=<provider endpoint>
TUTORLM_MODEL_API_KEY=<server-side secret>
TUTORLM_MODEL_NAME=<provider model name>
TUTORLM_MODEL_TIMEOUT_SECONDS=60
TUTORLM_MODEL_MAX_TOKENS=1024
```

`dev-instance-app.ps1` 会强制使用 `fake` 并清理继承的模型地址和密钥。真实模型请求可能产生费用，且模型输出仍需进行内容和数学正确性检查。

## 项目结构

```text
runtime-host/
  control-plane/       用户、项目、邀请码、租约、容量和 Docker 生命周期
  gateway/              Nginx 同源入口和受控转发
  deploy/               控制面与网关部署模板
instance-app/
  backend/              应用 API、Host、插件、任务、格式适配和存储
  frontend/              PDF.js、Reader、租约生命周期和通用浏览器模块
  workbench/             TutorLM 工作台页面、画布、阅读区和本地开发服务器
  deploy/                应用镜像构建入口
contracts/               外层与应用之间的版本化协议
docs/                    架构、配置、API、Reader 和开发说明
scripts/                 本地启动和边界检查
```

`runtime-host/` 和 `instance-app/` 是两个开发边界。`instance-app/backend/host` 属于应用容器内部，不是外层控制面。

## 测试与质量检查

控制面和应用后端应在不同 pytest 进程中运行：

```powershell
.\.venv\Scripts\python.exe -m pytest runtime-host/control-plane/tests -q
.\.venv\Scripts\python.exe -m pytest instance-app/backend/tests -q
node --test instance-app/frontend/tests/*.test.js
node --test instance-app/workbench/scripts/test-access.cjs instance-app/workbench/scripts/test-workspace-view.cjs instance-app/workbench/scripts/test-markdown.cjs instance-app/workbench/scripts/test-explanation-preferences.cjs instance-app/workbench/scripts/test-graph-layout.cjs
.\.venv\Scripts\python.exe scripts/check-boundaries.py
```

浏览器测试需要使用隔离的本地后端，不要对已有用户数据或生产服务运行测试。真实 Docker 集成测试必须显式开启，并使用隔离的本地资源。

## 当前限制

- 扫描 PDF OCR 尚未提供。
- TeX 安全编译执行器尚未提供，编译请求不会回退到不受限 subprocess。
- 当前任务接口使用轮询，不提供 HTTP SSE 或逐 token 流式回答。
- 公共工作台的 TLS、Cookie 安全属性、容量规划和长期运行需要在实际部署环境中单独验收。
- 代码中的本地测试通过不等于某个线上环境已经正确配置。
- 真实模型调用可能产生费用；仓库测试默认不调用远程模型。
- 关系梳理和布局只能在当前文档范围内工作；复杂或含环的图无法保证完全没有交叉。
- Markdown 原地编辑目前以文本块为边界；复杂表格单元格、已绑定解释的内容和不可验证的跨版本来源不会被强行重写。

## 部署说明

控制面动态创建应用实例，Compose 不会静态列出所有用户容器。部署时需要同源提供工作台页面、Reader 模块、PDF.js 脚本、worker 和字体资源。应用镜像负责后端和通用静态资源，完整工作台页面由外层网关按部署方案提供。

配置优先级为：内置默认值 → TOML 文件 → `CONTROL_PLANE_*` 环境变量 → 显式代码覆盖。详细配置见 [docs/configuration.md](docs/configuration.md)。

不要把 `.env`、真实配置、凭据、数据库、卷、日志、镜像归档或 `reference/` 提交到 Git。

## 文档入口

| 主题 | 文档 |
| --- | --- |
| 已实现能力与当前限制 | [docs/feature-status.md](docs/feature-status.md) |
| 工作台启动与浏览器测试 | [instance-app/workbench/README.md](instance-app/workbench/README.md) |
| 架构与责任边界 | [docs/architecture.md](docs/architecture.md) |
| 应用设计与 API | [docs/instance-app-design.md](docs/instance-app-design.md)、[docs/instance-app-api.md](docs/instance-app-api.md) |
| 插件 SDK | [docs/plugin-sdk.md](docs/plugin-sdk.md) |
| 文档 Reader | [docs/document-readers.md](docs/document-readers.md) |
| 工作区、邀请与分享 | [docs/workspaces.md](docs/workspaces.md) |
| 控制面配置与 CLI | [docs/configuration.md](docs/configuration.md)、[docs/cli.md](docs/cli.md) |
| 多人协作流程 | [docs/development.md](docs/development.md) |
| 运行契约 | [contracts/README.md](contracts/README.md) |

## 许可证

项目采用 [MIT License](LICENSE)。随仓库提供的第三方前端资源保留各自许可证，例如 [KaTeX](instance-app/workbench/assets/katex/LICENSE) 和 [markdown-it](instance-app/workbench/assets/markdown-it/LICENSE)。
