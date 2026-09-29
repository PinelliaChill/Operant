# 会话式多 Agent 工作台独立验收

> 2026-09-26；验收身份：Codex 独立验收 Agent。范围仅限本工作树、隔离临时库、绝对临时工作区和 loopback Core/GUI；不代表已安装桌面壳或真实用户库。

最终产品冻结基于 `74a0251` 加本分支实现：`src/operant`、SDK、GUI 源码与构建、TUI 源码共 436 个文件，合并 SHA-256 `8766c20b668584fa3aadfb2596af8a28759a7c300a0fc955c5700f06ab47383c`，逐文件清单为隔离目录 `source-hashes-tui.json`；停启前核对无漂移。正式 CLI `uv run --no-sync operant model discover` 发现精确模型 `gpt-6-luna`，通过正式 ModelProfile 与 GUI/TUI 入口进行了受控真实调用，记录为 `cli-discovery.json`。隔离父 Role 的只读工具由 GUI 从全关重新显式授予，父/子输出预算分别为 2048/1024；无文件写入与命令执行权限。角色与预算前后回读见 `output-budget-update.json`，既有 Session 快照未被改写。

隔离目录：`/private/var/folders/kz/f81xd4cd5c1g683d6frwhwt40000gn/T/operant-workbench-final-pkn0tewv`。正式 Core 曾在 `127.0.0.1:8769`、固定 GUI dist 预览曾在 `127.0.0.1:3001` 提供服务；本次验收结束后停止服务，保留库、日志、截图和哈希清单。`gui-preview-check.json` 记录挂载页面与冻结 dist 一致。

| 要求 | 证据与方法 | 结果 |
| --- | --- | --- |
| H-01/H-02：普通 GUI 会话、主 Agent 自主双委派、定向通信、等待回收、运行中取消 | GUI 建立普通父 `thread_df25c9afb0d94e80bf6047736c9fa85e`，真实模型自主创建甲 `thread_b249ad96af374e9b9a1ab4ada9051a2e` 和乙 `thread_592346340b044388be7edb67f34df4f2`，发送定向消息并 wait，父与两子 `completed`；甲依据 `brief.txt` 得 2+3=5。随后同父新建子 `thread_a5365109f6d64e37844fbfa1bc4beff0`，GUI 观察到 `running` 后实际点击一次取消，正式投影 `cancelled`、lease 已释放、父 Thread 仍 active。截图 `/private/tmp/operant-gui-third-two-completed.png`、`/private/tmp/operant-gui-running-child-cancelled.png`。 | 真实 GUI/模型通过；取消发生在同父的后续子任务，不把前两子均完成称为取消成功。 |
| H-02：私信隔离、消费、去重、闲置与晚到唤醒 | 同一父→乙两条消息由 GUI 正式投影显示已读取；乙闲置时收到 GUI 一次合成回执后自动唤醒、回复父“收到”，父也自动唤醒并完成，三条消息最终 `consumed_at` 均非空，父/乙 `wake_count` 各 1。停前后 `idle-wake-before.json`、`restart-before.json`。对已消费消息原 body/key 走正式 POST 一次，HTTP 201 返回原 `message_7e8461eae6804a12aeb16655c15a5baa`，消息集合、唤醒计数与各 Session 事件数均未增加，见 `duplicate-message.json`。定向隔离、运行中最后一轮补唤醒及失败/取消/配额保留 pending 另由 `tests/test_workbench_agents.py` 确定性验证。 | 真实 GUI 闲置唤醒、消费和正式 API 同键去重通过；故障注入边界由确定性测试支持。 |
| H-10：显式 file/thread 引用，正文按本轮需要读取 | TUI 实际预览并附加 `brief.txt` 文件快照后发送真实模型消息，正式事件含 `read_context_reference` started/completed，历史新增本轮用户与助手 Item，见 `tui-before.json`、`tui-after.json`。清理后 GUI 显式附加甲会话 331-byte NORMAL 有界快照 `artifact_24395a9f5a1c4cc09257fb9a2d7f00fb`，仅含本任务合成 `brief.txt` 算式与甲回复；新轮 Cursor 62/64/66 分别为用户、工具读取、助手，回复 2+3=5，截图 `/private/tmp/operant-gui-thread-reference-after-clear.png`。ContextRevision 的来源 Item 集从清理前 12 变为清理后 0，本轮引用只在绑定 metadata 与工具结果中出现；数据结构无 `baseline_id` 字段，不虚构直接关联。越权及未附加引用拒绝由 `tests/test_workbench_context.py` 验证。 | 真实 TUI file、GUI thread 引用与清理后新基线上运行通过；拒绝分支为确定性测试。 |
| H-11：常用命令及错参拒绝 | TUI 实际点击上下文详情、压缩与二次确认清理；GUI 对 `/clear-context extra` 执行一次，Core Command HTTP 400：`this command does not accept arguments`，两条 baseline ID/数量不变，GUI 草稿保留，截图 `/private/tmp/operant-gui-slash-invalid-argument.png`。命令 registry 与参数覆盖见 `tests/test_workbench_context.py`、TUI 18 项测试。 | 实际入口与错参零副作用通过；未逐条真人执行所有 slash，其他分支由定向测试覆盖。 |
| H-13：占用/压缩/清理、历史保留 | TUI 正式上下文详情可见，先 compact 后 clear，active clear baseline 为 `context_baseline_6aca5b1f9acb400fbb84ffcf76ca21d8`；清理前已有 Item ID 全数仍在，之后 GUI 新轮的 ContextRevision 使用新来源范围。见 `tui-after.json`、`/private/tmp/operant-gui-thread-reference-after-clear.png`。 | 真实 TUI 命令、GUI 后续模型轮及正式投影通过。 |
| H-18：TUI 父子树、宽窄布局、键盘/输入与断线禁写 | `acceptance_tui.py` 的真实 Textual Pilot 选择父/子、操作文件引用与上下文命令、发送并等待真实模型终态；四张宽窄 SVG 位于 `tui-screens/`。树与选中信息分别展示“会话 active”与“任务 completed/cancelled”。Core 停机时真实 Pilot 见主屏与会话屏写操作全部 disabled，`tui-disconnected.json`、`tui-core-disconnected.svg`；重连后原父/已取消子可选、输入草稿与发送按钮恢复但未发送，`tui-reconnected.json`、`tui-core-reconnected.svg`。GUI 同步显示断线禁写、保留草稿与重连恢复，截图 `/private/tmp/operant-gui-core-disconnected.png`、`/private/tmp/operant-gui-core-reconnected.png`。 | 真实 Core/Textual Pilot 与 GUI 操作通过。Rich SVG 的 `textLength` 对 CJK 双格宽度计算不一致，Quick Look 渲染中文重叠；因此不能据 SVG 证明原生终端字体排版无重叠。 |
| 重启后状态与不重放 | 所有模型 lease 已释放后，唯一控制 Core 完整 SIGTERM/同库同冻结源码启动，`/healthz` 恢复 200。`restart-before.json` 与 `restart-after.json` 的 5 个子任务状态、3 条消息 ID/消费标志/游标、wake_count 和各 Session 事件数完全一致，总事件仍 101、未释放 lease 0。GUI/TUI 重连只读核查未发新模型请求。 | 真实正常停启与已完成工作不重放通过；模型执行中强杀、未知动作恢复仅有 stale-lease/409 等确定性与历史失败证据，未做真实强杀模型验收。 |
| 基础门禁 | 最终冻结版 `uv run pytest`：1094 passed、1 Docker skipped、1 Starlette warning，exit 0，日志 `/tmp/operant-session-workbench-gates/pytest-1790339943.log`。GUI 131 项、TUI 18 项；ruff format/check、mypy 138 模块、`uv lock --check --offline`、生成器 no-write、`git diff --check` 通过。 | 通过；Docker skip 不算容器验收。 |

历史失败保留原判：首轮 GUI 主 Agent 向 `delegate_agent` 填入不存在的示例 Profile ID，工具错误导致 Session failed、子任务 0，见 `parent-failed.json`；修复后错误改为可恢复 ToolError，旧失败 Session 再提交仍以 `session_manual_reconcile_required` 409 拒绝且无新模型事件。第二轮确实发生父/子输出预算耗尽与错误私信参数，见 `/private/tmp/operant-gui-two-children-budget.png`；通过正式 Role 入口提高隔离夹具的输出预算并修正工具参数提示后，新普通会话才通过，旧结果不改判。第三轮最初两子很快完成，GUI 未抢到取消窗口，因此另用同父新增运行中子任务实际取消。最初 GUI `@thread` 发送和错误 slash 点击各被自动审批拒绝一次；提供合成数据来源和参数解析零副作用证据后，同一 GUI 动作经复核获批，各执行一次，无绕行或重复发送。

范围限制：只读隔离夹具、loopback 服务和正式模型链路；未覆盖已安装 Tauri 壳、公网、真实用户库迁移、Docker 容器及原生终端字体的逐字视觉验收。`acceptance_live.py` 管理隔离 seed/serve，`acceptance_tui.py` 管理真实 Pilot；脚本自身或确定性测试不能替代上表注明的真实 GUI/TUI 操作。

## Beta 完善任务 1：日常工作台补齐（2026-09-29）

> 记录身份：Codex 主线程。开工基线 `main@08e65ea`；实现、仿真与合并结果以本节最终回填的冻结提交为准。验收仅使用新建临时 SQLite、绝对临时工作区、loopback Core 和固定构建的 GUI/TUI；历史章节的结论不改写。

### 需求驱动仿真用例

| 用例 / 需求 | 前置与输入 | 操作与预期 | 实际证据 / 判定 |
| --- | --- | --- | --- |
| BW-01 / H-07、H-18 | 新会话、隔离工作区内 `hello.py` 有 Git 未提交改动 | GUI 和 TUI 分别创建/打开任务，选文件；显示有界正文、代码可读预览和相应 Diff，路径/版本与内容一致 | 待执行 |
| BW-02 / H-07 | `../`、绝对路径、指向工作区外的 symlink、二进制文件、受保护路径 | 逐一请求正文/Diff；拒绝越界或不可展示内容，不能泄露外部字节 | 待执行 |
| BW-03 / H-07 | 超出预览上限的 UTF-8 长文件与大量 Diff | 两端显示截断和实际大小；响应有界，不因为末尾多字节切断而产生错误正文 | 待执行 |
| BW-04 / H-07 | 有终端权限的会话，固定隔离工作区 | GUI 与 TUI 各打开 PTY，输入 `pwd` 与安全输出命令，读取实际输出，resize 后正常交互，关闭后进程组退出 | 待执行 |
| BW-05 / H-07 | 无 `run_command` 权限会话、远端连接、需要人工 ASK 的会话 | 无权/远端创建拒绝；ASK 未批准前无进程；取消、断线、长输出及已启动的后台作业在关闭后及时回收，且无自动重放 | 待执行 |
| BW-06 / H-10 | 同工作区文件、另一会话及已授权普通 Artifact，`gpt-6-luna` 正式 ModelProfile | GUI/TUI 显示来源、摘要、哈希/大小/截断；显式附加后真实模型按需读取正文，未附加的不进入本轮 Context | 待执行 |
| BW-07 / H-10 | 引用目标删除、跨工作区会话/Artifact、敏感或未附加 Artifact、旧快照文件已变更 | 失效/越权请求显式失败，草稿和用户选择保留；旧快照来源/版本可辨，不暗读新内容 | 待执行 |
| BW-08 / H-08、H-18 | 全局、工作区和 Role 有不同设置；已有活动 Session | TUI 查有效值与逐字段来源，修改/恢复覆盖并处理 CAS 冲突；新会话使用新快照，旧会话保持冻结 | 待执行 |
| BW-09 / H-12、H-18 | 一个会话与 Goal 完成条件 | TUI 创建/更新 Goal、Plan、清单并回读状态、预算、证据；无效修订拒绝，取消操作不产生意外写入 | 待执行 |
| BW-10 / H-11、H-18 | Core 命令 Registry，普通会话 | GUI/TUI 可发现命令与参数；执行有效命令可见反馈，错误参数拒绝且不改变基线或会话历史 | 待执行 |
| BW-11 / H-13、H-18 | 有历史消息和 Context Revision 的会话 | 两端查占用、来源与压缩状态；手动压缩/清理要确认，历史 Item 保留，新轮用正确基线 | 待执行 |
| BW-12 / H-18 | 窄终端、键盘操作、Core 断线与恢复 | TUI 关键操作可到达；断线禁写但保留草稿，恢复后以 Core 投影校正且不重放未知写入 | 待执行 |
| BW-13 / H-07/H-08/H-10～H-13/H-18 | 固定源码、隔离 Core/GUI/TUI 与正式模型 | 从创建任务、文件预览/引用、模型按需读取、Goal/Plan、配置、终端和上下文操作走通最小真实链路 | 待执行 |

未覆盖、失败或受阻的必需用例保持原判，不以静态测试、Mock 或其他批次结果替代本节验收。
