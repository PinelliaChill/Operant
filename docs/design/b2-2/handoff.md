# B2-2 交接（待独立审查）

记录身份：Codex；适用对象：User、Codex Reviewer。授权仅B2-2；没有推送、合并或部署。

## 版本与范围

- 治理根 `/Users/bigo/agentworkspace/codexworkspace/operant`，workflow-20260910.3及User后续Antigravity条件静默规则。
- 实施树 `/private/tmp/operant-b2-2`，分支 `codex/b2-2-tasks-host`，基线 `8851a237cc961afa8649cffe57d407472838887f`。
- 验收代码 `1b0fb4c6452374e9ba1267f672a420099d74f54d`；B2-1不重做；根工作区既有客户端/设计/未提交改动保留。
- 任务与验收来源见 [任务包](task-package.md)，实际客户端观察见 [客户端记录](client-acceptance.md)，环境见 [预检](environment.md)。

## 已实现

Core将Session/WorkflowRun投影为保留来源身份的Task，支持精确详情、历史分页、首个Thread创建、模型/角色配置、Agent实例分页和取消。B2单一Schema生成Python/TypeScript Client，旧协议冻结。

显式绑定Session的Thread保存用户/模型、工具与生命周期Item；事件和对应Item同事务。当前轮固定此前历史cursor，普通引用Thread不写入。Agent终态从已提交事件投影，防止流清理中断导致旧running行误导。

MP-1 Host提供受控安装目录、包/依赖/权限指纹、受控本机认证记录、绑定/配置、显式启停、运行lease/epoch fencing、有限stdio RPC、认证进程内适配、资源登记和keep/delete续做。受管索引按目录句柄读写删/登记，防止父目录替换、硬链接截断和无界读取，见[安全增量](host-security-increment.md)。普通Core默认不安装、不实例化插件。

GUI已接正式数据与命令，实际Tauri验证创建、命名Role选择、真实模型只读工具、审批一次、结果历史、取消、断线重连、重启回读、历史分页、宽窄屏与配置保存；事实和范围详见客户端记录。

## 验证与证据

| 范围 | 证据 | 当前结论 |
| --- | --- | --- |
| MP-1安装/绑定/资源 | test_plugin_host.py的install/private_index/registry_restart/cleanup测试 | 通过，最终完整门禁已覆盖 |
| 实际认证进程内 | test_trusted_mode_loads_verified_package_entrypoint：安装真实package并从entrypoint加载 | 定向通过，无plugin factory替代 |
| 实际未认证隔离 | 本机sandbox-exec启动探针拒绝受控Home读取/写入、目录外写入和网络；真实stdio经Host执行两次SourceBatch RPC | 已观察通过；见host-isolation-smoke.json |
| 预算与取消 | Host并发准入、deadline/log/late-result测试；独立子进程RSS/CPU/idle超限停止 | 定向通过；资源测试用transport shim，不冒充隔离证据 |
| 认证与Run安全 | 篡改/issuer撤回/撤销/过期/scope及伪造lease测试，重启旧lease过期 | 定向通过 |
| API/SDK | B2 API9 + SDK4；绑定/幂等/分页/错误/旧协议冻结/生成确定性 | 13项定向通过 |
| GUI | 91项测试、typecheck/build | 91项测试、typecheck/build通过 |
| 真实模型 | gpt-5.6-luna/low，正式ModelProfile与Tauri入口，read_file与审批cat，结果42 | 已通过范围受控调用；无插件 |
| 基础门禁 | [verification.json](verification.json) | 1b0fb4c完整门禁通过：772 passed/1 Docker skip，GUI91；日志已归档 |
| 独立Reviewer | 指定gpt-5.6-luna/max，原及替代任务均保留 | 恢复后再次额度失败，仅交回已修复的P1，见review-status.md |

## 边界与未完成

- 本机受控issuer认证记录是首个实现，不是外部CA/商店。认证进程内为信任边界、资源限制合作式；未认证只能实际沙箱，缺失即拒绝。stdio资源监测采样可能短时超调；首版只支持本机macOS隔离和stdlib/package代码。
- 不含MP-2记忆引擎/迁移、默认插件安装、插件HTTP管理/GUI生命周期（B2-3）、完整Graph/Team交互（B2-4）、打包dist/签名/公证。Docker环境不可用，skip不算容器验收。
- Task统一resume没有虚构接口；Session已验证明确新一轮继续，Workflow详情进入既有Graph安全恢复入口，未知写入仍需人工核对。不能把禁用按钮算作恢复验收。
- 1b0fb4c完整门禁已通过；指定Reviewer再次额度失败且尚无独立结论，故B2-2不能标完成。Reviewer恢复后只读本交接、任务包、必要源码与最新证据，按增量审查，不重跑B2-1。
- 子Agent中断/恢复已按原身份保留；host/gui工程归属已交还Codex。Antigravity交付a0a4a5d并释放CSS，Codex按原生缺陷增量修复；无新视觉项时条件静默。

## 恢复下一步

最终门禁已归档；Reviewer恢复后又报额度不足，下一步在额度可用时继续同一指定Reviewer任务 `01a08baa-4872-7473-abb1-7ca6fe91517d`。审查结论和必要修复完成后同步最终结果与治理进度，停止于B2-2。不得推送/合并/部署。

本次临时Core18000、Vite3000和独立验收.app已关闭，端口已释放；用户8000未触碰，临时库和证据保留，见cleanup.json。
