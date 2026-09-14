# B2-1 Reviewer 返工：浏览器路由证据

记录身份：Codex；适用对象：Antigravity、Reviewer。2026-09-09。
实现：03cd4b0 + 本次返工；源码位于 /private/tmp/operant-b2-1-a。

使用独立 agent-browser 会话 operant-b2-1-review、临时 Vite 127.0.0.1:3001、当前真实 React Router。
应用实际使用 HashRouter，因此有效入口为 #/session 和 #/remote。测试会话 localStorage 模式设为 live。
不使用 Mock 响应替换 Core，没有提交任务、调用模型或写用户库；只检查 URL 和 DOM 屏障。

| 输入 | 实际终点 | .live-unavailable-state |
| --- | --- | --- |
| /#/session | /#/chat | false |
| /#/remote | /#/settings?cat=system | false |
| 重新打开 /#/remote | /#/settings?cat=system | false |
| /#/collab/wf-b2-review/canvas | 同一画布深链 | true |

mode 的实际回读均为 live。该检查通过真实浏览器挂载 RailLayout/Outlet/Navigate 的组合，
不只测试 resolver 函数，也不把重新打开页面说成 Tauri 深链。

独立浏览器已 close；本任务 Vite 已停止（exit 130）。未改现有 8000 Core。
首次误用无 hash URL 的尝试不计入以上证据；未命中的 URL wait 也不算验收。
这是浏览器集成验证；真实 Tauri 窗口、模式切换及桌面入口仍由 B 补证，不能以本表替代。
