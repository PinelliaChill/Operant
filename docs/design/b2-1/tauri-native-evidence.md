# B2-1 原生 Tauri 验证记录（GUI-L0 范围完成）

记录身份：Codex；适用对象：Antigravity、Reviewer、User。2026-09-09。
本记录区分真实窗口观察与尚未覆盖项，不据编译、进程驻留或浏览器测试推断整个桌面验收完成。

## 产物与运行来源

- 实施 worktree：`/private/tmp/operant-b2-1-a`；核对 HEAD `79d21e8d7b9e0bbb9dcfae7173c81820a65fa855`，代码无改动。
- User 已当次明确允许启动本批二进制，后续同一产物验证无需重复请求许可。
- 二进制：`clients/desktop/src-tauri/target/debug/operant-desktop`，30,209,056 bytes。
- SHA-256：`9cfa47df5c9bebe4d72da6fdf089d5dbd04cf3a51690ca9b8133be6ce103497c`。
- CUA 不识别裸二进制路径，因此建立临时 `/private/tmp/Operant B2-1 Verification.app`。
  `Contents/MacOS/operant-desktop` 是指向上述已获准文件的符号链接，二进制内容未修改。
  包装的 bundle id 为 `dev.operant.b21verification`，未替换 `/Applications/Operant.app`。
- 实际通过 CUA 打开并操作原生窗口，App 名为 `Operant B2-1 Verification`，窗口标题为 `Operant`。
- 该 debug 产物在真实 WebView 中加载 `127.0.0.1:3000/#/chat`，对应本工作树的 Vite 服务。
  本轮没有证明 debug 产物嵌入 `dist`；不能沿用 Antigravity 的这一描述。
- 续接后只读进程核对仍有本批二进制 PID 33200；3000 端口已无 listener。恢复验证时先重新核对现场。

## 已取得的原生窗口证据

以下为 CUA 实际可访问性树与交互结果的摘要，未冒充原始截图文件。

| 操作 | 实际观察 | 结论范围 |
| --- | --- | --- |
| 打开原生窗口 | WebView URL 为 `127.0.0.1:3000/#/chat`；显示“演示模式 使用内置演示数据”，开关 on | 当前为明确标注的 Mock，不是 Live 假成功 |
| 点击任务入口 | URL 为 `#/tasks`，显示 6 项演示任务及“演示数据” | 确认切换前确有演示任务 |
| 返回主页并关闭演示模式开关 | 演示会话列表消失；开关变为“切换到演示模式”/off；Project、Thread、Session 显示未选择/未绑定；创建操作禁用 | 原生 Mock→Live 视图与可见选择隔离已验证；不声称直接读取 React 内部变量 |
| Live 连接结果 | 显示 `core_disconnected`、“Core 连接失败”“Projection 待校正”，明确说明不会回退演示 | 验证连接失败时的显式失败与禁用行为；不推断 Core 服务根因，也不是成功模型链路验收 |
| Live 再次点击任务入口 | URL 保持 `#/tasks`，显示“任务：Live 本阶段未接入”，原 6 项 Demo 任务不再挂载 | 原生任务页阻断已验证 |
| 在原生 Web Inspector 的可见 Console 执行 `location.reload()` | WebView 在刷新后仍为 `#/tasks`，重新出现“任务：Live 本阶段未接入”，仍显示 Core 连接失败 | 原生任务深链刷新及 Live 模式持久化已验证 |

原生刷新是通过 Web Inspector 的可见输入框执行，没有使用外部浏览器冒充 WebView。

## 2026-09-10 恢复后的原生补证

记录身份：Codex；源码 HEAD `6534c2c0ee06e235f297a2c3dd605bd97a3944fe`，运行代码仍为 `1e0cfc7`。
CUA 重新可用后，重新绑定同一临时验证 App；旧 Vite 依赖文件缺失，按本批 package-lock 在
`/private/tmp/operant-b2-1-verify-deps` 执行 `npm ci --offline --ignore-scripts`，恢复独立依赖。
未改 package/lock；本工作树 node_modules 链接改指独立目录，未修改旧工作树共享依赖。
仅恢复本工作树 127.0.0.1:3000 Vite，退出空白旧验证窗口并重开同一已授权产物。

以下均通过原生 Web Inspector 可见 Console 输入导航，并读取实际 WebView 可访问性树：

| 操作 | 实际观察 |
| --- | --- |
| 原生重新打开 | `#/chat`；演示模式开关 off；Core 连接失败，未回退 Demo |
| `location.hash='/remote'` | 跳转 `#/settings?cat=system`，显示“系统与设备”；Load failed 明确可见，启用 Host 禁用 |
| 设置页 `location.reload()` | Console cleared，WebView 仍为 `#/settings?cat=system`；随后显示系统与设备及失败状态 |
| `location.hash='/session'` | 跳转 `#/chat`，演示模式开关仍 off |
| `location.hash='/collab/wf-b2-review/canvas'` | 同一路径显示“工作流画布预览：Live 本阶段未接入” |
| 画布页 `location.reload()` | Console cleared，仍为该画布路径及未接入屏障 |
| `location.hash='/agents'` | `#/agents` 显示“Agent：Live 本阶段未接入” |
| `location.hash='/runs/b2-review'` | 同一路径显示“运行详情：Live 本阶段未接入” |
| `location.hash='/workflow/b2-review/s/session-review'` | 同一嵌套路径显示“工作流深链：Live 本阶段未接入” |

剪贴板超时后先回读输入，再只提交已输入命令；未把超时、函数引用或 SyntaxError 算作刷新成功。
本记录是实际 CUA 交互及 AX 观察摘要，不冒充原始截图文件。

## 结论范围与限制

GUI-L0 所需原生模式隔离、旧别名、嵌套深链、刷新与显式错误观察已补齐。
Inspector 显示 Core 协议请求预检 HTTP 400 / access control checks 失败；未修改既有 Core 配置。
这证明失败时不回退 Demo，不证明成功的 Core/模型调用；模型调用为 0。
使用真实 Tauri debug WebView + Vite，未验证打包嵌入 dist、签名、公证或发布产物。
MP-1、Host 运行和真实模型链路仍按后续批次验收。

补证结束后已退出临时验证 App、停止本轮 Vite；只读进程/端口检查确认没有 operant-desktop 进程及 3000 listener。未停止或重配既有 Core。
