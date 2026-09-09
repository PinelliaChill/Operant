# B2-1 原生 Tauri 验证记录（部分完成）

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

## 未覆盖项与恢复点

- 原生 `#/remote`、`#/session` 的旧别名跳转尚未取得有效结果。
  尝试输入时发生剪贴板读取超时，随后 Console 显示拼接命令
  `location.hash='/remote'location.hash='/remote'` 的 SyntaxError；此尝试不计为通过。
- 原生画布、Agent、Run、Workflow 深链尚未逐项走查；既有浏览器/单测结果保留各自范围。
- 目标续接时，当前工具清单已经没有 `cua_repl`，普通 node_repl 中 `cua` 与 `b2App` 均不存在，
  因此不能继续当前原生 UI 操作。未改用 AppleScript、System Events 或其他 UI 技术绕过。
- 恢复桌面控制工具后，复用已获准产物，核对并恢复 3000 的本工作树前端服务；取得新的 AX 树，
  不复用旧元素编号。Console 输入先选中并清空，避免再次拼接，然后逐项检查真实跳转、阻断和刷新。
- 代码、隔离测试及独立审查已完成；B2-1 整批仍未宣布桌面验收完成，不进入 MP-1。
