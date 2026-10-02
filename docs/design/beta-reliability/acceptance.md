# Beta 任务 2：可靠性、性能与资源治理

> 2026-10-02；记录身份：Codex 主线程；基线 main@600d97c，实施分支 codex/beta-reliability-task2。仅执行六次计划中的任务 2。

范围来自 [Harness 目标第 3 节](../Operant-Harness待实现目标.md)：H-01/H-02 取消、去重与恢复；H-13 长任务压缩；H-14 资源盘点、TTL、Pin、保留锁和清理；既有记忆形成、召回与纠正；日常负载基准。实现状态与验收状态分别记录，必需项缺证据时不宣布整组完成。

证据根为治理工作区 `.operant/beta-task2/evidence/`，所有业务数据均为合成数据，使用隔离 SQLite、绝对临时工作区和 loopback 服务。凭据仅进程注入，未复制到工作树或证据目录。

| 用例 / 需求 | 输入与操作 | 预期 | 实际证据 / 状态 |
| --- | --- | --- | --- |
| BR-01 / H-01、H-02 | 创建父子任务，跨 Core 取消活动 owner；排程后、模型调用前取消；重复创建/发送/取消 | 取消传播、最终持久状态一致，同键回读且不重复模型/工具；活跃租约不被误判重启 | **通过**：`workbench-recovery/pytest.log` 25 项覆盖预占租约、跨 Core/启动前取消、同键取消后回读、并发唤醒与完成/取消两种顺序；详见 [可靠性说明](workbench-recovery.md)。 |
| BR-02 / H-01、H-02 | 在模型流、真实工具执行、审批等待时强杀 Core，重启同一隔离库 | 已提交事实保留；审批不自动续跑未知动作；未知副作用不重复；完成后消息消费/唤醒去重 | **通过**：正式 HTTP Core 子进程在模型等待、工具副作用已提交但回执未完成、Git 审批等待三处强杀后同库重启，无重复副作用。`reliability-real-controlled/result.json` 的真实 `gpt-6-luna` 首帧后受控暂停再取消：重启唯一 child=cancelled，事件 3→3，无模型重放。受控门闩只固定时间点，未制造模型响应。 |
| BR-03 / H-13 | 合成长历史含早期目标、普通表达约束、决定、未完成项及来源；自动与手动压缩，下一轮正式模型回答 | 关键事实/来源和工具调用配对保留；历史不删；不可满足预算显式失败 | **通过**：`context-real-final/result.json` 两次正式 `gpt-6-luna` 回答都保留蓝杉报告、两页限制、模板 B、未完成合计核对及 docs/facts.md；自动/手动均有 Compaction，50 个 Canonical Item 不变。`automatic/manual_summary.json` 与 Revision 保留完整证据；密集超预算、重复手动压缩、损坏摘要和工具配对由定向测试覆盖。 |
| BR-04 / 既有 Memory | 正式保存并确认项目偏好，召回；Proposal/CAS 纠正，新 Session 再召回及模型回答 | 旧快照边界清楚，新 Session 使用已确认新版本，无静默跨 Scope 注入 | **通过**：`memory-real/result.json` 正式保存/确认后回答英语；Proposal/CAS 纠正并确认，新 Session 的输入仅中文版本，回答中文。旧 Session 冻结记忆边界保留，不外推普遍收益。 |
| BR-05 / H-14 | 真实引用快照及当前其他资源分类盘点，调整 TTL、确认完成、Pin，预览再手动清理 | 显示大小/归属/保留原因；Pin 与聊天、记忆、审计、审批、可恢复 Run 保留；只删除安全临时资源 | **通过**：真实 GUI Pin、无效 TTL 拒绝、保存 2/72 小时、完成确认和清理回读；`clients-final/gui-after-cleanup.json` 仅 unused 快照 deleted，其余 Pin、引用、ContextRevision 与历史保留；源工作区文件保持原文。普通 SESSION Artifact Pin、跨 scope/敏感边界由 Core 回归覆盖。 |
| BR-06 / H-14 | TTL 到期前后、未回复计时、Pin/活动 Run 在预览后变化、重启扫描与清理阶段失败 | 到期自动回收，恢复保留锁再检查，不清理用户工作区，不因重启丢策略或重复删除 | **通过**：`tests/test_workbench_resources.py` 覆盖到期前后、旧时钟的新资源、Pin/活动 Run、缺失 blob 未知结果、并发更新、UTF-8 字节、线程轮转、Trash 后续清理和默认物理删除关闭时的启动回收。TTL 用受控时钟验证，worker 用真实 App 生命周期重启；另有 v20→v21 历史/账本保留、空治理回退和非空拒绝回退。 |
| BR-07 / 日常负载 | 12 个会话、每会话 100 轮 / 200 Item；列表、分页历史、详情、压缩与资源盘点各 10 次 | 记录 P50/P95、内存、库大小与源码；只修实测影响使用的瓶颈 | **通过**：同一 12 会话 / 2400 Item / 20 快照负载各 10 次；`load-after.json` P95 列表 3.928 ms、历史页 6.551 ms、详情 18.98 ms、压缩 85.095 ms、资源盘点 390.727 ms，进程峰值 RSS 约 86 MiB。均低于本场景 500 ms 交互参考，未启动无边界优化；不包括 Provider 等待或无限规模。 |
| BR-08 / 正式客户端 | GUI 挂载资源面板，宽窄屏、键盘、错误/断线、Pin/预览/确认清理回读；适用 TUI 与生成客户端 | 通过同一 Core 契约操作，权限/未知状态显式失败，不回退 Mock | **通过**：固定生产构建 GUI 实际 Pin/预览/确认清理与 Core 回读，390px 无横向溢出且 Tab 焦点可达；明确断线禁写并同库恢复。`clients-final/tui-result.json` 的 72 列真实键盘入口、Pin 清理禁用、取消预览与完成撤回均通过。GUI 138/138，TUI 30/30；类型和构建通过，生成 digest e38adddc…b69b8a 连续两次一致。`gui-result.json`、断线/恢复截图和 before/after-restart.json 证明重启不重放；次要文字实际对比度 7.63:1。 |

**本任务范围已实现；BR-01～BR-08 验收通过。** 原 H-ID 的其他批次范围及整个 Beta 完成度不由此改判。

源码冻结后完整基础门禁通过：Ruff format/check、mypy（165 文件）、Python `1219 passed, 9 skipped, 1 warning`、离线锁和 diff 均退出 0。最终证据为 `gates-complete/status.json`、`pytest.xml` 和 `0.log`；源文件摘要与交付源码一致。Schema/客户端生成一致性、GUI/TUI 测试、GUI 类型/构建已通过。模型发现通过正式 `operant model discover`，上述真实调用使用其精确 `gpt-6-luna` ID 和正式 ModelProfile。

本次未更新已安装 App、迁移真实用户库、合并、部署或启动任务 3～6。

## 保留的失败、中断与证据边界

- 初次 H-13 `context-real/` 因引用裁剪与正式 Token 计数不一致而失败；修复后 `context-real-final/` 两路通过，旧库不改判。
- 初次 GUI fixture `clients/` 暴露连续引用快照策略时间戳冲突；修复并增加回归后，新 `clients-final/` 完成实际操作。
- `reliability-real/` 首帧后约 68ms 自然完成，取消产生投影竞争；修复完成/取消持久排序后复验。`reliability-real-final/` 仅 agent.started，执行被额度中断，没有取消或结果，不计通过；最终受控真实证据为 `reliability-real-controlled/`。
- `gates/` 保留格式/类型及版本断言失败；`gates-final/` 静态已过，Python 运行被额度中断，未记完整通过。当前新运行保留源文件摘要和 PID，避免把日志进度当完成。
- 清理只针对安全临时资源；权威历史、输入/压缩、长期记忆、审批审计、恢复及未知结果证据持续保留。资源大小为逻辑 payload 字节，不等于 SQLite 文件占用或 Provider 内部 Cache。
- 本轮只验收本机 loopback、隔离数据和合成任务；没有重验安装包、原生系统能力、真实跨设备或公网，也没有把既有有效证据外推为任务 3～6 完成。

9 个跳过项为 2 个 Docker、6 个浏览器/第三方沙箱、1 个 macOS 前台 App 的显式 opt-in 场景，本次未重验这些任务 3/4 的独立能力；没有用 skip 证明容器、浏览器接管或桌面操控通过。任务 2 的模型、工具持久副作用、审批、资源重启以及 GUI/TUI 必需场景均另有本文件记录的有效证据。

最终只做本地提交；未推送、合并、部署、更新 App 或迁移用户库。后续工作需另行启动，任务 3～6 本轮未执行。
