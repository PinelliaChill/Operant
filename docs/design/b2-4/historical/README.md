# B2-4 历史验收证据恢复

记录身份：Codex；适用对象：User、Reviewer；2026-09-14。

本目录四份 JSON 与丢失前 Reviewer 在 2026-09-14T07:46:19.262Z 读回的原 SHA256 完全一致。来源、call_id 与哈希见 recovery-source-manifest.json。文件中的状态与路径描述当时的验收环境，不代表当前服务仍在运行，也不是恢复后的新执行。

- native-single.json：真实 Tauri 单 Agent、Memory Pack、只读工具与检查器；保留原文件的非因果消融限制。
- native-multi.json：真实 Tauri 发起的两 Agent Graph，记忆、工具、答案与 SSE 终态。
- native-freeze.json：上述段落的固定源码、GUI 产物、服务与临时库记录。
- gui-artifact-publication.json：接收者可见性缺陷发现与 revision2 修正；原文件的其他成员读回仍标为 pending，不能擅自改成通过。

后续 registry 和同请求 QueryPlan 优化须按任务包补验；这些历史记录不能代替最终代码的联合验收。没有迁移真实用户库。
