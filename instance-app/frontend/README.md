# Frontend Lifecycle

当前目录提供两个同源模块，尚未实现完整学习工作台，也没有登录页或静态站点：

- `assets/lease-lifecycle.js`：页面生命周期与关闭通知；
- `assets/invitation-entry.js`：邀请码入口表单（申请计算租约）。

完整工作台仍未提供，页面不会自动获得这些能力；二者都需要某个页面显式挂载或安装。

## 邀请码入口

`mountInvitationEntry(container, opts)` 直接在给定容器里生成一个真实 DOM 表单：
邀请码输入框与提交按钮始终可见，不做落地页或重定向。它只负责“取得计算租约”，
不安装租约生命周期模块（避免重复登记页面），接入方式由 `onLease` 决定。

```javascript
import { mountInvitationEntry } from './assets/invitation-entry.js';
import { attachLeaseLifecycle } from './assets/lease-lifecycle.js';

const entry = mountInvitationEntry(document.querySelector('#entry'), {
  workspaceId: 'ws-existing',   // 打开已有项目；它不是邀请授权，邀请模式下仍需邀请码
  async onLease(response) {
    // response 是 POST /control/leases 的响应：{ lease, instance_url, ... }
    const presence = attachLeaseLifecycle(response.lease, { onLeaseLost() { /* 回到入口 */ } });
    await presence.ready;
  },
  onImported(workspace) { /* 分享副本已建立，可先记下它 */ },
  onReady(state) { /* state.ok === false 时表单保持不可用 */ },
  onError(error, context) { console.error(context, error); },
});
await entry.ready;             // 首次访问策略加载结束
await entry.refreshPolicy();   // 需要时手动重新校验策略
entry.dispose();               // 中止在途请求、移除表单
```

### 访问策略

组件用 `GET /control/access-policy` 读取 `{ invitation_only: bool, ... }` 决定是否为必填：

- `invitation_only: false`：可以留空直接试用；填了邀请码会原样传给服务端，优先级由服务端判定。
- `invitation_only: true`：在**尚未取得 trial grant** 时输入框 `required`，服务端在签发租约时会再次检查。
  `opts.workspaceId` 只表示“打开这个已有项目”，不是邀请授权证据，因此邀请模式下未取得 grant 时仍需邀请码。
- 请求失败或响应缺少布尔值时**不放行**：表单保持不可用并显示错误，避免失败即放开。
  此时会显示一个轻量的“重试”按钮，也可以调用 `refreshPolicy()` 重新校验。

每次提交前都会重新校验一次策略，因此策略在挂载后收紧/放松也会在下次提交时生效；
成功或失败的策略加载结果都会通过 `onReady` 回调。

免码的唯一依据是本组件已经取得的 trial grant：一次成功的分享导入会为对应工作区登记该 grant，
之后即使清空邀请码，重试也能无码打开该副本。将来若服务端返回显式的 grant 字段，
可由后端确认后据此放宽；在此之前客户端不假设历史系统码等同于 grant。

### 提交与分享码

提交时按 `POST /control/leases` JSON `{ workspace_id?, invitation_code? }` 申请租约，凭据走同源 Cookie。
传入 `opts.workspaceId` 时会带上该 `workspace_id` 打开已有项目（这是普通的项目打开，不代表已获邀请授权）；
分享码导入则会把新副本的 `workspace_id` 一并带上。

以 `TLM-SHARE-` 开头的码视为分享码：先 `POST /control/workspace-imports` `{ invitation_code, title? }`
得到独立 workspace，再用新的 `workspace_id` 申请租约；workspace id 只能来自导入响应，来源项目不能直接使用。
其他码（如 `TLM-SYS-`）由服务端判定类型，客户端不伪造优先级；前缀只用于选择走导入还是直接申请。

导入请求带 `Idempotency-Key`，取自已注入环境（默认 `globalThis`）的 Web Crypto
（`crypto.randomUUID`，缺失时退回 `crypto.getRandomValues`）。若环境没有 Web Crypto，
组件会明确报错而**不会**退回 `Math.random`，避免不可靠的去重键导致重复克隆。
同一个导入 attempt 复用同一个 key 直到成功，因此网络重试不会重复克隆。
导入成功的副本按 `code + title` 只在本次挂载的内存中缓存：
若随后的租约申请失败，重试会复用该副本而不会再次调用导入接口。缓存不落盘、不跨挂载共享。
副本一旦建立就立即触发 `onImported`（即使租约随后失败），`onLease` 只在租约成功时触发。

复制是独立 workspace 导入，会先停止源项目计算：源内容无损，但源端进行中的任务可能被打断。
组件在提示文案里说明了这一点，不展开成完整功能说明。

### 行为与安全

- 同源请求一律带 `credentials: 'same-origin'`；用户身份沿用现有 Cookie。
- 不读取、不记录、不持久化任何令牌或邀请码文本。
- 成功后清空输入；失败保留输入以便重试，并显示错误；提交期间输入框和按钮禁用。
- `dispose()` 通过 `AbortController` 中止在途请求并移除表单，卸载后不再触发回调。
- 服务器返回的字符串只通过 `textContent` 写入，不使用 `innerHTML`。
- 输入框与按钮带 `label`、`aria-required`、`aria-describedby`；状态用 `role=status`，
  错误用 `role=alert`，提示文案同时作为 `title`。每次挂载生成唯一 DOM id，多挂载不串线。
- 邀请码输入 `maxlength=256`，与后端 `invitation_code` 字段上限对齐。
- 消费方回调（`onReady`/`onImported`/`onLease`）抛错不会破坏表单状态，也不会被误报成 API 失败；
  错误交给 `opts.onError`，未提供时写 `console.error`。

### API 摘要

| 名称 | 说明 |
| --- | --- |
| `mountInvitationEntry(container, opts)` | 挂载表单，返回 `{ ready, refreshPolicy, dispose }` |
| `opts.workspaceId` | 已有项目 UUID，仅用于打开该项目；不是邀请授权，邀请模式下仍需邀请码 |
| `opts.environment` | 依赖注入点（`fetch`/`document`/`crypto`/`AbortController`），测试用 |
| `opts.importTitle` | 分享导入时的可选项目标题 |
| `opts.onReady(state)` | 每次策略加载/刷新结束回调，`state.ok === false` 表示表单不可用 |
| `opts.onImported(workspace)` | 分享副本建立后立即回调（即使随后租约失败），每个新副本一次 |
| `opts.onLease(response)` | 租约申请成功后回调，由调用方接入生命周期 |
| `opts.onError(error, context)` | 消费方回调抛错时回调；缺省写 `console.error` |
| `ready` | `Promise`，首次策略加载结束 |
| `refreshPolicy()` | `Promise`，重新校验策略，可用于失败后的手动重试 |
| `dispose()` | 中止在途请求、移除表单、停止回调 |

## 页面关闭释放计算

工作台取得有效租约后，接入同源生命周期模块：

```javascript
import { attachLeaseLifecycle } from './assets/lease-lifecycle.js';

const presence = attachLeaseLifecycle(lease, {
  onLeaseLost(error) {
    // Disable application actions and return to the project list.
    console.error(error);
  },
});
await presence.ready;
```

`lease` 是租约响应中的 `lease` 对象，必须含 `lease_id` 和 Unix 秒时间 `expires_at`。
每个 document 只创建一个控制器。在 SPA 中离开工作台但不离开 document 时调用
`presence.dispose()`；显式“释放计算”按钮仍可调用 `DELETE /control/leases/current`。
终止错误要求停止应用操作；短暂网络错误会重试，不得因此自动延长绝对计算期限。

模块为每个 document 生成新的 UUID，`pagehide` 发出关闭通知，普通刷新后的新 document
以及后退缓存恢复后的新 presence 会重新登记。它不会以 `visibilitychange` 将切换标签等同关闭。
同租约多个标签页各自登记，只有全部离开或失去心跳后才自动回收。

关闭通知通过 `sendBeacon`，排队失败时使用 `fetch` 的 `keepalive`；服务端心跳超时兜底。
仅凭浏览器关闭回调不能保证通知送达。模块不提供项目数据续期或登录逻辑。
接入细节见 `../../docs/workspaces.md`。

测试无需安装第三方包：`node --test instance-app/frontend/tests/*.test.js`（仓库根执行）。
