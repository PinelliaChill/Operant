# B2-3 J1 原生验收记录（进行中）

记录身份：Codex；适用对象：所有Agent；2026-09-13。尚未完成J1，不是交付通过声明。

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

## 未完成/阻断

- 永久delete两条路径（已安装卸载delete、保留dataset删除）、历史/专属资源清理回读：等待本次CUA永久删除确认，只有合成临时dataset，不能操作真实用户库。
- Project重命名/归档/解除关联完整桌面，Skill启停/卸载与基础任务装载，Artifact宽限期/保护矩阵仍需按范围补证。
- 宽窄屏/键盘焦点/对比度最终验收：原1200×768截图发现分区标签与scope排版，COM-20260912-001/UI-MEM-02已交Antigravity，尚未收到实名接受。
- 正式模型Run失败ProviderError；诊断HTTP422 invalid_model_error，目录刷新ConnectError，见model-smoke/provider-validation；不能以discovery旧成功或Mock代替。
- 最终完整门禁和独立Reviewer增量结论未齐。

2026-09-13补充：迁移查询已修且合成v14演练通过(migration-acceptance.json)。stdio空闲/真实deadline和notebook inactive索引已修；真实isolated notebook重装、新保存、停用后查询0完成。standard重装重新绑定正式查询成功。GUI101测试通过，Root又修预算键脱敏与实际项目名；原一条Host binding静态断言已改为禁止冒充项目名。完整集成门禁已记录exit：Python876pass/1skip，其他通过；GUI旧断言失败随后纠正重跑0，见gates/integrated。此后新增memory_version来源回调用于notebook精确键过滤，正在定向验证，不能冒用之前全套为新代码结论。
项目主名改为J1 记忆验收项目；两独立项目分别归档/解除关联，keep.txt已读回保留，见project-source-retention.json。Artifact pinned schedule在UI明确拒绝，保持连接。Skill启用已验，停用/卸载尚未提交。
CUA最近报用户改变窗口，实际从Skill跳回聊天；已询问是否可继续操作验收窗口，尚无明确回复，暂不点击。另已请求永久删除两个合成dataset确认，等待明确回复，不将自动“继续”当作该删除问题答案。模型ID/配置问题也仍未得到答案。
原Reviewer增量额度失败，不再重复启动相同失败服务；独立初审报告已复制review-initial.md。
