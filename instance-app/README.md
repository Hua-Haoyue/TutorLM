# Instance Application

本目录属于容器内团队。`backend/` 已提供**可本地启动的 HTTP Host**（`python -m backend`，见 `backend/README.md`）；`frontend/` 提供租约页面生命周期、邀请码入口与阅读器模块。**完整学习工作台仍未提供**。

预留归属：

- `backend/`：应用 Runtime Host、业务分层、插件和任务管理。设计与分层见 `backend/README.md`。
- `frontend/`：学习工作台，以及浏览器端租约和应用会话接入逻辑。
- `deploy/`：应用 Dockerfile、镜像构建与交付脚本，不放网关或控制面。见 `deploy/README.md`。

## 本地启动（真实后端）

`backend/` 是真实应用后端，不是 fake 控制面。先按 `backend/README.md` 装好依赖，再在仓库根执行：

```powershell
.\scripts\dev-instance-app.ps1          # 默认 127.0.0.1:8091，数据/会话在 var/instance-app/（git-ignored）
.\scripts\dev-instance-app.ps1 -Help    # 参数与安全默认值
```

脚本 loopback-only、强制离线 `fake` provider、不挂载真实持久卷、不启动付费模型；详见仓库根 [`README.md`](../README.md) 与 [`../docs/development.md`](../docs/development.md)。
应用**不提供** `/docs`：启动后访问 `/health`、`/health/ready`、`/api/runtime`。

`frontend/` 提供可直接挂载的同源模块：邀请码入口表单（申请计算租约）、关闭工作台释放计算的生命周期模块与文档阅读器，详见 `frontend/README.md`。
完整工作台仍未提供，页面不会自动获得这些能力，需显式挂载或安装。

文档支持目标为 **md/txt/pdf/tex（含 tex 多文件工程）**。md/txt 抽取与 PDF **结构化门禁**已实现；
PDF 文本抽取使用 `backend/requirements.txt` 的 `pypdf` 后端；浏览器 PDF.js 引擎桥接已实现，
使用锁定的本地依赖和 `frontend/assets/pdfjs-engine.js`，不从 CDN 加载脚本。
TeX 编译在本仓库**无安全执行器**：默认 **503 `tex_compile_unavailable`**（见
[`../docs/tex-compilation-security.md`](../docs/tex-compilation-security.md)）。
Reader（MD/PDF 统一 open/select/search/navigate/highlight，tex 源码编辑器与编译产物共用同一 Reader）
已提供适配器和单元测试，完整页面渲染与交互接线尚未完成。**同一 workspace 仍是单 project**：tex 工程是**一个 document revision 的多 resources**，
不新增控制面 project 概念。

内层团队可以独立提供镜像和静态前端制品供外层联调；不需要依赖外层源码，
也不需要共享数据库或安装控制面的 Python 包。

持久项目接入前先阅读 `../contracts/instance-runtime-v3.md`，匿名临时模式见 v1。
外层已提供项目 UUID 和独立持久卷；内层的严格配置解析与就绪判定（`/health/ready`）已实现，但 **managed 形态的卷初始化 / 克隆 / 重绑（clone/rebind）与布局标记由外层在容器外接通**，完整 bootstrap 流程尚未 outer 接通，不能作为已可用能力依赖。
`/internal/status`、可信 drain 与自动租约到应用会话交换尚未实现，不应作为现有能力依赖。

## 设计与实现边界

- 容器内架构设计：[`../docs/instance-app-design.md`](../docs/instance-app-design.md)
- API 契约草案（非已实现）：[`../docs/instance-app-api.md`](../docs/instance-app-api.md)
- 文档阅读器（Reader 框架与统一引用）：[`../docs/document-readers.md`](../docs/document-readers.md)
- TeX 编译执行安全：[`../docs/tex-compilation-security.md`](../docs/tex-compilation-security.md)
- 持久项目运行契约：[`../contracts/instance-runtime-v3.md`](../contracts/instance-runtime-v3.md)
- 后端分层与目录：[`backend/README.md`](backend/README.md)
- 镜像与交付：[`deploy/README.md`](deploy/README.md)

设计文档包含已实现接口和后续规划；以代码、测试及上述能力边界区分，不将缺失的安全编译器、完整工作台和外层集成当作已完成。
