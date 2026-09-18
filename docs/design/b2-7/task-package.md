---
task_id: B2-7
owner: Codex
status: complete_candidate
scope: B2-7 / MP-6 only
base_head: 5b7833aa0a9c3d7c1cddc44689654d2c1509297c
code_head: 684bea1ca994d59ea63df7c34ba2f75cf076c7fc
delivery_ref: origin/codex/b2-7-acceptance-candidate
governance:
  root: /Users/bigo/agentworkspace/codexworkspace/operant
  version: workflow-20260915.1
  files:
    - path: AGENTS.md
      sha256: 7ffeca81acbd409b37d9504c905194fc361c5afc76b3338296d229bd150cddae
    - path: MEMORY.md
      sha256: f08729775f0b34094cc5a6391aa440cbf00ab43af915165f5d9015be6bea15f0
    - path: memory/communication/README.md
      sha256: 545facda04ff5501c8e8a0fbdb1ed977500392faa1bbc52a8fbda8acd8b6d179
    - path: memory/communication/items/README.md
      sha256: 3e6554b03f45d6adeb37b09be6e1fcbfa6223e19cb25a2b6184f433367f73b02
    - path: docs/design/Operant-Beta-2.0更新计划.md
      sha256: af1d494c17c95ae03abe976fa16c9bba6676e37c95ffa6bc7685d62030ebf719
    - path: docs/design/记忆系统设计草案.md
      sha256: 8d1eab12a162cc542dc1c40636d7ef760a1ddd4199794713599984a6146ee960
implementation:
  worktree: /Users/bigo/agentworkspace/codexworkspace/operant/.worktrees/b2-7
  branch: codex/b2-7-acceptance-candidate
  dirty_ref: null
coordination:
  antigravity_task: null
  initial_activation: not_needed_until_visual_issue
  delivery_channel: null
acceptance_environment:
  holder: none (acceptance stopped)
  revision: b932e30 product; 684bea1 evaluation; release Tauri inputs unchanged
  resources: [B2-7 release Tauri app, 127.0.0.1:8000, /private/tmp/operant-b27-native-final, /private/tmp/operant-b27-migration-final]
  paused_writers: []
  release_condition: 本段真实验收结束或发现需修复产品缺陷
evidence_index: []
review_ref: review.md
next_action: 本批候选验收完成；交付以工作分支及PR回读为准，随后停止，合并和发布等待User授权
---

# B2-7 / MP-6 候选验收

记录身份：Codex；适用对象：本批执行者与 Reviewer；治理版本 workflow-20260915.1。

本批从已合并 B2-6 PR #22 的 `5b7833a` 建立持久隔离树，主工作区既有改动保留。产品冻结
`b932e30`，评测脚本冻结 `684bea1`。只授权 B2-7 / MP-6，普通工作分支推送与可审阅 PR；
不合并、部署、正式发布或迁移真实用户库。B2-4 Host 性能限制保留，未启动性能专项。

## 结果与证据

| ID | 对应要求 | 结果与入口 |
| --- | --- | --- |
| AC-01 | MP-6.1 / 计划6：联合故障 | 完成。新增7项、相关137项，并纳入最终1070项通过；冷启动、并发、崩溃、撤销/关闭、keep/delete、缺包与安全重启边界见 [故障矩阵](fault-migration.md)。明确区分真实stdio、shim和OS沙箱。 |
| AC-02 | MP-6.2 / 计划8.1：真实对照与形成后复用 | 完成范围内测量。[评测说明](evaluation.md)、[汇总](evaluation-acceptance.json)：43×3确定性；固定4×3实际模型完成；答案达标0/4、2/4、3/4。学习经实际read_file、模型提取、正式propose/review；固定问题有超时/拒答，明确要求转述推断证据的补充问题成功。负结果不改写，不推广为普遍收益。 |
| AC-03 | MP-6.3 / 计划8.2：隔离升级回退 | 完成。[migration-final.json](migration-final.json) 合成v14→18原行保留，空新增表受限回退与非空拒绝，真实sandbox-exec；[native-upgrade.json](native-upgrade.json) 真Core启动升级、旧记录明确legacy_unverified。没有原子跨包升级或通用降级承诺。 |
| AC-04 | MP-6.4：旧旁路收敛 | 完成。[旧路径说明](legacy-cleanup.md)：旧写明确升级、Workflow旧自动读写/关键词晋升移除；兼容读逐版本检查角色/Agent/权限/来源。初审P1/P2已修复并独立复核。 |
| AC-05 | GUI-LR / 计划8.3：真实桌面、GUI/TUI、单多Agent | 完成。[新库原生](native-fresh.json)、[升级原生](native-upgrade.json)、[最终大投影原生](native-projection-final.json)；真实模型+read_file与实际Pack，Graph两Agent同链/私有隔离见 [graph-live.json](graph-live.json)。[最终TUI](tui-projection-final.json) 为headless Textual+真实HTTP四步/断线，不冒称原生桌面。GUI121通过/类型/构建，TUI12通过，协议确定生成。 |
| AC-06 | 计划8.3：候选包与用户说明 | 完成。[候选验证](candidate-verification.json)、[中文使用说明](usage.md)。本地dist/b2-7-candidate含macOS arm64壳、Core与TUI wheel/sdist、SBOM/清单/hash；正式独立安装两插件与10协议通过。 |
| AC-07 | AGENTS：完整门禁与独立审查 | 完整门禁步骤exit0：1070通过、1 Docker条件skip；[门禁闭合](gates-closure.json)说明评测脚本改动导致raw source_unchanged=false，产品/测试未变，当前脚本另验。最终 [Luna/max Reviewer](review.md) 已签审，无开放P1/P2。 |

## 证据复用与限制

- 新库原生在002f98c完成；随后仅兼容读权限与B25/B26回复包装产品变更。授权路径定向测试、最终全套和独立复核覆盖权限差异，原生最终大投影与TUI覆盖回复差异；未把早期原生结果冒称同一SHA。
- 升级/Graph在2bf45c4完成；至最终产品只改 api.py、api_b2_5.py、api_b2_6.py。Runtime、Graph、迁移、Host与客户端输入未变。最终Core大投影原生回归完成，无需重复模型任务及整条迁移。
- B2-6 J3仍按原证据范围成立；本批无Writer/Remote/Share/Skill逻辑改动，相关完整测试重跑。GUI/Tauri/TUI源码未改；原范围宽窄屏/焦点/颜色检查复用。本批未出现需改视觉的缺陷，未启动Antigravity或冒称其参与。
- B2-4 Host性能限制继续保留；不补跑专项性能对照。首次模型等待未单独埋点，价格/超时usage未知，成本不能填零。此类测量缺口不降低权限、数据正确性和真实链路门槛。
- 真机范围仅本机macOS arm64；候选只有ad-hoc链接签名，无Developer ID、公证、DMG/自动升级。桌面依赖单独Core；版本暂为0.1.0，Git SHA/hash识别候选。
- 生产公网/HTTPS Target及Docker容器未验，delete不是磁盘安全擦除，未知写结果不自动重放。未清零历史安全扫描事项。

## 分工、修复及失败记录

Codex root负责集成、模型/原生与最终交付；Luna/max子Agent负责旧路径、故障迁移、初版评测与候选校验，均按文件交权；原评测脚本由root接管修正。独立Reviewer为gpt-5.6-luna/max；工具无Fast开关，未宣称开启。

保留 `gates-002f98c.json`（1056通过、5旧benchmark适配失败、1skip）、`gates-42b2186-interrupted.json`（因发现真实大投影回执缺陷结束）及所有评测失败报告。大投影修复是本批真实验收发现的产品缺陷。权限P1/P2和回执缺陷均有复现、修复、定向回归；不把失败历史改成通过。

此前大评测将提取、审批和质量测量绑成单段，定位问题时会重复模型调用。已拆成固定对照、形成冻结和只读复用，按变更补验；以后继续使用这个划分，避免每次修复都重跑整套模型链路。

## 停止边界

原生新库、升级库、最终投影库均为独立合成数据；本轮Core和桌面已停止。完成独立签审、普通推送和PR回读后同步治理根进度并停止，不进入新阶段。

2026-09-18 Codex裁决：复用验收保留原固定问题失败，补充问题只要求注明推断证据与适用条件；模型、角色、预算、冻结记录和cutoff不变。最终独立Reviewer核对原MP-6.2后确认已满足形成后复用验证，无需把原问题反复采样至成功。这不降低权限、来源真实性或证据级别。

合并跟进（2026-09-18，Codex）：User已明确授权合并PR #23。首轮远端Python矩阵失败，按[CI跟进](ci-followup.md)仅修四处测试条件并补定向验证；产品与候选不变。最终合并状态以治理根进度和GitHub回读为准。
