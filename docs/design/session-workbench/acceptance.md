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
| BW-01 / H-07、H-18 | 新会话、隔离工作区内 `hello.py` 有 Git 未提交改动 | GUI 和 TUI 分别创建/打开任务，选文件；显示有界正文、代码可读预览和相应 Diff，路径/版本与内容一致 | **通过**：GUI `gui-file-diff-visible.png` 可见 28 B 正文及 `return 42→43` Diff；TUI `tui-live.json` 的会话、预览和 Diff 用例为 true，Core `api-smoke.json` 回读一致。 |
| BW-02 / H-07 | `../`、绝对路径、指向工作区外的 symlink、二进制文件、受保护路径 | 逐一请求正文/Diff；拒绝越界或不可展示内容，不能泄露外部字节 | **通过**：`api-smoke.json` 实测 `../` 与绝对路径 400、symlink 和 `.env` 403、二进制 415；TUI 越界仍保持连接；`history-protection-smoke.json` 对旧 `.bash_history` 的正文、Diff、引用均为 403。`test_h07_preview_rejects_file_replaced_between_check_and_open` 实际替换最终文件后返回 409，替换后的正文未泄露。 |
| BW-03 / H-07 | 超出预览上限的 UTF-8 长文件与大量 Diff | 两端显示截断和实际大小；响应有界，不因为末尾多字节切断而产生错误正文 | **通过**：GUI `gui-long-truncated.png` 显示 630000 B 已截断；TUI `tui-live.json` 覆盖 70 KB 正文与 630000 B 中文引用；`large-diff-smoke.json` 的 5000 行 Diff 限于 4096 B 且标截断；UTF-8 边界修复见 `b7c15ae`。 |
| BW-04 / H-07 | 有终端权限的会话，固定隔离工作区 | GUI 与 TUI 各打开 PTY，输入 `pwd` 与安全输出命令，读取实际输出，resize 后正常交互，关闭后进程组退出 | **通过**：GUI `gui-terminal-narrow.png` 与审计显示 ASK 后唯一 PTY、`pwd`/`GUI_PTY_OK`、resize 97→33、终止；TUI `tui-terminal.json` 与 `tui-live.json` 显示 `pwd`、92×27 resize、Ctrl+Q 关闭、后台 PID 消失。 |
| BW-05 / H-07 | 无 `run_command` 权限会话、远端连接、需要人工 ASK 的会话 | 无权/远端创建拒绝；ASK 未批准前无进程；取消、断线、长输出及已启动的后台作业在关闭后及时回收，且无自动重放 | **通过**：`api-remote-smoke.json` 远端 403；GUI ASK 审计证明批准前无进程、同键只启动一次；TUI `tui-terminal.json` 的 2000 行输出约 34 KB 后仍可关闭且后台 PID 消失。`bw05-core-65d401f.log` 的 5 项正式 FastAPI 路由加真实本机 PTY 用例验证 Policy ASK 前未启动、Role 禁用 403、WebSocket Token/断线清理、Thread 取消回收、Shell 自然退出且无历史文件；最终完整门禁通过。 |
| BW-06 / H-10 | 同工作区文件、另一会话及已授权普通 Artifact，`gpt-6-luna` 正式 ModelProfile | GUI/TUI 显示来源、摘要、哈希/大小/截断；显式附加后真实模型按需读取正文，未附加的不进入本轮 Context | **通过**：GUI `gui-three-reference-chips.png` 显示 file/thread/普通 Artifact 摘要和哈希，TUI `tui-live.json` 附加普通 Artifact 并拒绝跨 Thread。GUI 正式 `gpt-6-luna` 首轮两次 `read_context_reference` 成功，回复使用 `hello.py` 的 `43` 与普通 Artifact 首句；`gui-model-first-{items,events,revisions}.json` 证明仅两份显式 artifact 绑定，第三个 thread 绑定是当前会话自身。独立 TUI 正式模型轮见 `tui-model-audit.json`：未附加轮没有引用读取；缺失文件显式 `http_400`、保留草稿与历史；只附加 `hello.py` 后工具一次成功返回精确 28 B，模型回答 43，绑定仅含该快照和当前会话。 |
| BW-07 / H-10 | 引用目标删除、跨工作区会话/Artifact、敏感或未附加 Artifact、旧快照文件已变更 | 失效/越权请求显式失败，草稿和用户选择保留；旧快照来源/版本可辨，不暗读新内容 | **通过**：GUI `gui-stale-reference-error.png` 在文件移走后重附加返回错误，同页已有 chip 与草稿保留；旧/新快照哈希各与原文一致。`tests/test_workbench_context.py::test_file_snapshot_is_metadata_first_scoped_and_immutable` 通过正式 `read_context_reference` 路径证实原文件变更后旧快照仍返回旧正文，未附加或跨 Thread 读取被拒；跨 Thread Artifact 403 与敏感/内部快照拒绝另有 Core/TUI 测试。GUI 错误提示已在 `853260b` 修正并实际挂载检查。此旧快照分支使用确定性工具调用，未再发起一次模型请求。 |
| BW-08 / H-08、H-18 | 全局、工作区和 Role 有不同设置；已有活动 Session | TUI 查有效值与逐字段来源，修改/恢复覆盖并处理 CAS 冲突；新会话使用新快照，旧会话保持冻结 | **通过**：`tui-live.json` 的来源、Role 覆盖/恢复与冻结/CAS 通过；独立 `tui-bw08-09/pilot.json` 的 8 项中，TUI 写入 global=low、workspace=medium、role=high 及各层 Prompt，回读 role 优先、逐层 reset 来源回落、旧 Session 冻结、新 Session 生效和 stale CAS 409。 |
| BW-09 / H-12、H-18 | 一个会话与 Goal 完成条件 | TUI 创建/更新 Goal、Plan、清单并回读状态、预算、证据；无效修订拒绝，取消操作不产生意外写入 | **通过**：`tui-live.json` 的 Goal/Plan/清单创建与更新、预算/证据回读、stale revision 拒绝为 true；独立 `tui-bw08-09/pilot.json` 在 TUI 编辑 Goal/Plan 表单后直接离开，重开回读字段、revision=1 和清单全部不变。 |
| BW-10 / H-11、H-18 | Core 命令 Registry，普通会话 | GUI/TUI 可发现命令与参数；执行有效命令可见反馈，错误参数拒绝且不改变基线或会话历史 | **通过固定命令入口**：GUI `gui-command-invalid-visible.png` 显示错参及草稿保留，Context/Items 前后 JSON 相同；TUI `tui-live.json` 命令发现、错参、二次确认与 clear 新基线为 true。动态 Skill/插件命令仍属任务 4 原范围。 |
| BW-11 / H-13、H-18 | 有历史消息和 Context Revision 的会话 | 两端查占用、来源与压缩状态；手动压缩/清理要确认，历史 Item 保留，新轮用正确基线 | **通过**：GUI 首轮模型产生 8 个历史 Item；`gui-model-context-before-clear.json` 与 `gui-model-context-after-clear-cancel.json` 完全相同，确认后 active baseline 从 `context_baseline_8a09…` 变为 `context_baseline_fcc1…`，旧 8 个 Item ID/顺序全保留。GUI 第二轮 Cursor 9–14 的正式 `gpt-6-luna` 读取 `hello.py` 成功；`gui-model-after-clear-revisions.json` 的新 Revision `source_item_ids=[]`、来源游标为空，仍绑定当前会话与本轮显式快照，见 `gui-model-after-clear-{items,events,revisions,context}.json` 及前后截图。TUI `tui-live.json` 覆盖占用和命令入口。 |
| BW-12 / H-18 | 窄终端、键盘操作、Core 断线与恢复 | TUI 关键操作可到达；断线禁写但保留草稿，恢复后以 Core 投影校正且不重放未知写入 | **通过**：`tui-live.json` 72 列焦点可达；`tui-disconnect.json` 3/3 true，停机禁写/草稿保留，重启同库恢复且 Item 0→0 无重放；GUI `gui-core-disconnected/recovered.png` 显式断线与恢复。 |
| BW-13 / H-07/H-08/H-10～H-13/H-18 | 固定源码、隔离 Core/GUI/TUI 与正式模型 | 从创建任务、文件预览/引用、模型按需读取、Goal/Plan、配置、终端和上下文操作走通最小真实链路 | **通过**：GUI 文件/普通 Artifact 正式模型读取及有历史的新基线轮、TUI 第一段 14/14、断线 3/3 与独立模型轮 4/4 通过。隔离原生 WebView 连接真实 Core，显示 `hello.py` 正文/Diff、历史；本次 Approval `approval_6c83fbd697894823a73a39b7ff8e65d4` 经页面确认写入 `approved / human_approved`。同一请求重试后原生 PTY 回显隔离工作区 `pwd` 和 `NATIVE_PTY_OK`，自动 resize 后 Core 回读 97×7；关闭后 `terminal_6f8eadadf0074dc49624b7279a130500` 为 `terminated`、无流 token。`native-pty-audit.json` 记录 `approval.consumed → terminal.started → terminal.terminated`。文件替换竞态加固后的四项受影响 API 用例通过；最终基础门禁以 PR 同一 HEAD 的 CI 为准。 |

未覆盖、失败或受阻的必需用例保持原判，不以静态测试、Mock 或其他批次结果替代本节验收。

### 当前范围与证据边界

本轮固定源码分支为 `codex/beta-workbench-task1`，隔离验收数据位于 `/Users/bigo/agentworkspace/codexworkspace/operant/.operant/beta-task1-tmp/acceptance/`（Git 忽略）。`seed.json` 记录唯一临时工作区与正式 `gpt-6-luna` ModelProfile；Core 只在 `127.0.0.1:18769` 运行，GUI 开发代理为 `127.0.0.1:13000` 或原生 dev 壳的 `127.0.0.1:3000`。早先 `/private/tmp` 目录被系统清理，其截图与库不作为本表证据；本表只引用当前持久隔离目录的回读。任务专用原生 App `dev.operant.desktop.beta-task1-smoke` 已由 CUA 实际打开 WebView，关闭演示模式后连接隔离 Core，显示文件正文与 Diff、历史、新基线、审批中心及真实 PTY 输出。首次发现原生 `window.confirm` 未弹出，修复为页面内聚焦确认（`c315b9a`）；最终原生批准、同键重试、输入、输出、关闭及 Core 回读均通过。它不是已安装的正式 App，也不代表安装包验收。

| 原需求 | 本任务范围实现状态 | 本任务范围验收状态 | 后续原范围边界 |
| --- | --- | --- | --- |
| H-07 | 已实现正文、代码预览、必要 Diff、本机交互 PTY 与历史保护 | BW-01～05 及最终完整门禁通过 | 完整 IDE 编辑器不列为 H-07 前置 |
| H-08 | TUI 已接有效配置来源、覆盖、恢复与 CAS | BW-08 及独立三层配置 Pilot 均通过 | 任务 6 仍需跨端日常组合验证 |
| H-10 | GUI/TUI 已接 file、同工作区 thread、授权普通 Artifact 的有界引用 | BW-06 GUI/TUI 正式模型读取与 BW-07 旧快照工具读取、失效及越权分支通过 | 任意对象引用不从本轮结果外推 |
| H-11 | GUI/TUI 已接固定 Slash Registry、参数校验与反馈 | BW-10 固定命令入口通过 | Skill/插件动态注册命令留在任务 4 的原范围 |
| H-12 | TUI 已接 Goal、Plan、清单管理 | BW-09 及独立编辑后离开零写 Pilot 均通过 | BTW 与六项默认能力包的真实产物留在任务 6 |
| H-13 | GUI/TUI 已接占用详情、压缩/清理与确认 | BW-11 有历史的取消/确认、Item 保留及新轮 ContextRevision 通过 | 长任务保真度与性能留在任务 2 |
| H-18 | TUI 已接日常会话、文件/引用、配置、Goal/Plan、命令、上下文与 PTY | 第一段 `tui-live.json` 14/14、断线 `tui-disconnect.json` 3/3、TUI 正式模型 `tui-model-audit.json` 4/4 通过 | 任务 6 仍需真实日常任务组合 |

2026-09-30 记录身份：Codex 主线程。安全加固前的完整基础门禁从本分支源码显式 `PYTHONPATH` 运行，在允许本机回环测试的环境中得到 `1194 passed, 9 skipped`，Ruff format/check、mypy、离线锁和提交差异检查通过，退出码 0；日志为 `base-gate-final.log`，退出码为 `base-gate-final.exit`。前两次 history 用例高负载失败促成 `65d401f` 的 PTY 持续读取测试修复；一次未允许绑定本机回环的失败和一次导入旧 editable 工作树的运行均不作最终门禁证据。起初 User 仅授权域名时，自动审批在 GUI 点击发送前拒绝外发，`gui-model-autoreview-block.json` 证明没有模型请求或绕道发送；User 随后明确授权 `hello.py` 正文与普通 Artifact 文本发往 `axon.ystone.top`，GUI 和 TUI 正式模型读取已完成。PR #34 的 CodeQL 对新文件打开路径报告高危警报后，最终文件名增加精确匹配及打开前后设备号/inode 核对；四项受影响的文件正文/Diff API 用例（含新增替换竞态用例）、Ruff 和 mypy 已通过。修复后的完整基础门禁以 PR 同一 HEAD 的 CI 结果为准。本组 BW-01～13 必需仿真通过；未把隔离开发壳验收外推为已安装 App 或生产远程验收。
