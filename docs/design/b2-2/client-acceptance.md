# B2-2 实际客户端验收记录（进行中）

记录身份：Codex；适用对象：B2-2 Reviewer。只记录实际观察，不以此代替未完成项目。

- 环境：本机 Tauri debug WebView，复用未变更的 B2-1 桌面二进制，本树 Vite 3000 → 独立正式 desktop Core 18000；用户8000服务未改动。临时库与绝对workspace位于 `/private/tmp/operant-b2-2-runtime/`。
- 正式 Provider Discovery 67个精确ID，选择 `gpt-5.6-luna`；正式ModelProfile、RolePreset v3，effort low，4轮/90秒/600输出token/2工具调用。允许read_file/run_command，workspace_write=false；命令经过Action Gateway审批。
- 原生从零Thread的已登记Workspace点击新建会话，得到 `thread_b284f3efb9974b969b01a46e3ed0cba9`；命名Role选择后创建 `session_8854d653b5524fc0af07ae473a2f681c`。重复创建Session按钮禁用。
- 实际只读模型：Agent `agent_d505ceb5b7394b9a833a0f6e93d49efd`，read_file读取proof.txt，模型结果42，约7.9秒；正式事件已持久化。发生于历史修复前，不把此轮算作canonical历史通过。
- 真实审批闭环：新一轮Agent `agent_aacd341fe093488fae7abfbb12b927ef`，只读cat proof.txt；GUI批准 `approval_aa492e0aba2d41049fac42c92d736179` 一次，待审批归零，工具完成、模型回复“算术结果是 **42**。”和agent.completed自动显示于canonical Items（cursor6～13）。
- 真实取消：Agent `agent_ccf48ec0286849c1a2c6357a47221646` 在等待只读命令审批时由GUI取消，canonical cursor18为agent.cancelled，Task列表显示已取消且取消动作no_active_run。残留审批通过GUI明确拒绝，不重新执行。
- 中断边界：前一轮Agent `agent_1904d3a0598e4aa89c122167a3e3e780` 因历史Query500/流超时中断，显示明确错误；旧审批 `approval_bf7322a9d7794a858fcabd486e67cad7` 在无continuation时明确拒绝，未重放。已修复整页脱敏截断及只读历史错误误锁manual_reconcile；仍需独立Reviewer复核此边界。
- 回读：Core安全重启及原生页面重载后，查询能读取上述canonical历史；会话来源保持同一ID。不能把此前“活跃”Thread状态当作Session运行终态。
- 宽窄屏：实际原生约1200宽与768宽；任务卡在768宽首次挤成竖列，限定CSS将堆叠断点调至1000并提高scope优先级后，已实测正常横向文字、纵向卡片分组、动作可见。Tab焦点环已观察。审批空态窄屏和底部导航可用。
- Agent实例：原生页面从正式分页API读取真实created/running/completed/cancelled状态；发现旧取消事件与running行不一致，新增终态事件优先投影及cleanup修复，新版本原生刷新已确认旧实例显示已取消。

- 实际分页：独立测试Thread `thread_df0fa350ac7c42b7b18b5dc5aefca096`、Session `session_86fab275c1ac4eb2a4e171443b8842b6`，通过正式API写105条明确标注的非模型测试记录。原生首屏001～100，点击加载更多后滚动看到101～105，末页按钮消失。注意AX单次列表会截100项，滚动后确认末页，不能误判数据丢失。
- 窄屏会话：768宽发现历史/检查器因flex压缩重叠，新增仅Live对话CSS维持正常文档流，实际滚动确认105末尾、输入框、下方检查器独立可达。
- 断线重连：停止仅本次无运行Core，原生显式“Core连接失败，实时数据未加载”，新建/发送/取消均禁用；恢复相同临时库后自动重连，保持同Thread，首屏历史与加载更多恢复。没有静默切换Mock。

- 窄屏配置：B2表单采用顶层portal和限定modal样式，原生768宽可见完整表单、滚动区域及保存/关闭按钮，Tab焦点进入ModelProfile。GUI修改Role名称为B2-2 verified reader并保存v4；原Session仍用v3快照。预算与工具策略通过正式API回读核对。

## 尚未完成

最终候选完整门禁、恢复边界独立审查；指定Luna/max Reviewer因workspace额度失败，暂无独立通过结论。未覆盖打包dist/签名/公证、Docker隔离或后续MP-2。
