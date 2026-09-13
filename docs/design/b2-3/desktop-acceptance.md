# B2-3 J1 原生验收记录

记录身份：Codex；适用对象：所有Agent；2026-09-13。本批J1步骤已完成；最终交付仍需冻结版本的独立复核。

## 环境

- 独立debug壳 `/private/tmp/Operant B2-3 Verification.app`，复用B2-2验证过的原生二进制，SHA256 `9cfa47df5c9bebe4d72da6fdf089d5dbd04cf3a51690ca9b8133be6ce103497c`；本批未改Rust，没有签名/正式打包验收。
- WebView实际加载127.0.0.1:3000，开发代理只连临时Core18000。数据库与workspace均在 `/private/tmp/operant-b2-3-j1`；用户8000不动。所有操作经CUA真实原生UI，未以浏览器截图代替。
- 当前运行源码指纹 `/private/tmp/operant-b2-3-j1/runtime-fingerprint.json`；最近一次重启加载生命周期/迁移修复。随后protocol.py只补datetime类型注解；需在最终冻结证据中区分。

## 已完成真实步骤

1. Live模式创建项目“J1 记忆验收”，项目/Workspace ID由Core返回，绝对workspace登记成功。
2. standard以isolated安装、明确项目绑定、显式保存中文记忆、来源展开、提议与CAS确认、查询、关闭、重启读回、keep卸载。数据集 `dataset_ec9f6f31485c4aa7b7cf89b95771d274`。
3. standard导出JSON确已落地Downloads，字节数/摘要/合成record ID读回见 `desktop-export.json`。没有仅凭Completed声称已下载。
4. standard重装接回同dataset，再选项目，最新一次正式查询“待验证”返回原已发布rev3/version3，记录1。
5. notebook先以trusted_in_process实际安装，保存“验收标记=MAPLE-42”；修改43提议pending期间正式记录与查询仍为42；确认后rev2/version2=43。
6. 原生发现停用后私有索引返回旧ref被Core拒绝，修复包后keep卸载、以isolated重装接回。新的“修复验收=INDEX-OK”保存与查询成功；停用rev2后正式查询返回0，保持Core已连接。
7. 隔离进程跨越原30秒空闲阈值后仍能查询。原空闲误报deadline问题已修，真实请求deadline仍受限；对应短时定向回归在test_plugin_host_stdio.py。
8. Skill从临时配置源发现j1-reference，实际安装副本并按项目启用；Artifact测试对象Pin与审计成功，未scheduled的Trash明确拒绝。
9. 配置页读到全局/项目开关、引擎/dataset、Role模型/prompt/预算/来源和新Session生效边界；预算数值不再误当凭据隐藏。
10. UI业务错误保留可用管理投影，传输/协议/未知写继续阻断；已卸载实例无可执行生命周期按钮。

## 修复发现与复用范围

- 早期standard v2曾被GUI错误confirmed=true直接发布，该旧操作不作为候选隔离证据；以notebook后续明确pending→旧查询→confirm证据替代。
- 空闲超时实际来自stdio资源watchdog对无请求空闲寿命的错误失败判定；并非readline在空闲时执行。修复后正常空闲不报请求超时，仍保留RSS/CPU和显式关闭。
- notebook Head停用状态为inactive，索引此前仅处理revoked/deleted；已补inactive并通过Manager回归。

## 2026-09-13 最新原生补验

- 当前 Core 已重启加载 c2856f79fd66bd795aca271ea690e4f1558c076b 的修复；实际关键源码指纹见 model-context-acceptance.json。新增的 GUI 记忆插件入口已从 MCP 页面实际点击进入，GUI 三脚本全部通过。
- 主项目改名完成；修复后的解除关联保持 archived=false、installation_id=null，再绑定最新 notebook 成功。早期“解除关联会归档”的独立项目步骤不作为修复通过证据。源码 proof.txt 仍保留。
- 设置页实际切换项目关闭、全局关闭、全局关闭时项目请求开启：有效值仍由 global=false 覆盖；插件启用明确返回 memory_disabled 并保持连接。实际变化时间分别为项目 2026-09-13T04:41:56.639189+00:00、全局 2026-09-13T04:42:08.908019+00:00，无关设置不共用新时间；旧不可追溯值显示 unknown。随后重新打开全局，原插件保持停用直到显式重装/启用。
- Skill j1-reference 已实际按项目停用并卸载托管副本；来源 SKILL.md 保留，见 desktop-skill-uninstall.json。正式 Session 的 Skill guidance/关闭后下一 Session 行为已有 test_b23_session_context.py 确定性测试，不冒充真实模型 Skill 调用。
- 重新运行正式 discovery（discovery-refresh.json）后，使用精确模型 gpt-oss-20b、正式 ModelProfile/Role/Session/Run，在绝对临时 workspace 读取 proof.txt 并回答42。实际 Task 显示已完成；详情首次受旧投影缓存影响显示“未找到该 Core Thread”，点击“刷新 Core Projection”后 canonical 历史显示 read_file、21 * 2 = 42 与最终答案。该手动刷新限制保留，未静默回退 Mock。
- 两次真实 Context 的 memory_refs=[]、唯一工具 read_file、无已存记忆正文；全局关闭后没有新插件来源行，见 model-context-acceptance.json。早期 Luna invalid_model_error 与 Gemini ReadTimeout 保留为失败历史；最新真实链路已通过，不再重复模型请求。
- 最新 notebook 通过 keep 卸载旧包、isolated 正常重装并接回同一两条记录的数据集，安装 Python 文件摘要与当前源码一致。新实例 plugin_installation_ee887ea3caa94e5797e6063735520ec9。
- 对原 inactive 记录提出“修复验收=INDEX-EXACT”：pending 时精确键查询0；确认后 published/version2/revision3，完整键“修复验收”命中1、部分键“修复”0、值“INDEX-EXACT”0。见 desktop-exact-reinstall.json；未新增第三条记录。
- 最后完整代码门禁888pass/1条件Docker skip、GUI101/类型/构建通过；原Reviewer两轮代码问题全部关闭。最新GUI入口、审计呈现及删除提示增量再运行102tests/typecheck/build全0。复用未变化Core门禁，不重复完整测试。

- User明确授权上述两个临时dataset删除后，standard执行已安装卸载delete，notebook先keep再保留dataset delete，两条路径均Completed并显示已删除0记录/导出禁用。专属versions/heads/proposals/idempotency/legacy/source行均0，保留2+3 tombstones；41登记资源均deleted、5安装目录均不存在，Run均completed。canonical Items19/Context4/Artifact1数量不变，proof.txt/Skill来源/独立Downloads导出摘要不变。见desktop-delete-acceptance.json；不是磁盘安全擦除。
- 删除后安装卡曾错误显示可以重装，已按服务端dataset状态修正并原生读回“对应数据集已删除，不能重新接回”。
- Artifact合成工件取消固定→归档→安排清理→宽限期未到明确拒绝Trash→恢复active取消安排→重新固定→审计。正式审计扫描引用1、内容1、findings0，保留状态与审计事件已读回。补齐GUI审计返回呈现后，原生显示最近一次审计结果与未发现异常。见desktop-artifact-retention.json；没有加速时钟或物理purge。

## 最终视觉与连接补验

Antigravity通过COM-20260912-001实名交付234955e、38e0bfe、ffa4950，仅b2-memory.css；已交回写权。原生宽屏/768×797与445×820窗口、标签可点/选中/Tab焦点、长scope/单列、浅深主题已检查。纵向flex-basis导致180px空白已返修复验；说明文字由低对比muted改secondary，实际文字色对比最低6.08:1，焦点超过3:1。范围为本批管理界面，不宣称全应用WCAG认证，见native-visual-acceptance.json。

真实停止临时Core后，原生显示未就绪/禁写；刷新返回明确invalid_error_envelope（Vite无上游），没有Mock回退。原临时create_app驱动未开启desktop CORS，后改用项目既有build_server_config(desktop=True)，只连本批临时库与18000。健康/GUI HTTP200，tauri://localhost预检200且回显Origin；原生手动刷新后管理投影恢复已连接。见desktop-reconnect.json。不涉及真实用户库或产品源码新增放宽规则。

最终GUI102/typecheck/build均通过，Core与SDK未变，复用888pass/1条件Docker skip及协议生成证据。最后等待原Reviewer确认冻结版本、增量与证据覆盖，再形成最终交付。
