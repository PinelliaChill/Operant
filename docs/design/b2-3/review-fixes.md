# 增量审查处理
记录身份：Codex；适用对象：原Reviewer；2026-09-13。原审查独立报告见review-delta.md；以下为负责人修复/反例，尚未获Reviewer最终确认。

| 项目 | 当前处理 | 定向证据 |
|---|---|---|
| P1-01 inactive旧ref | 未复现原结论。authorize_ref已有head.state==published与精确published_version条件；只有当前extract/confirm的Core staged版本能使用_pending_refs。普通recall没有该例外。增加恶意插件返回inactive ref的实际Host测试，拒绝成功。未改原授权条件。 | test_inactive_candidate_from_malicious_plugin_is_rejected |
| P2-01 owner/source | Manager要求proposal.owner==installation.owner==version.owner，source_refs==version.sources；实际插件伪造principal/删source均被拒绝。Ledger已对构造和supplied Proposal校验同一版本owner/source。 | test_plugin_cannot_forge_proposal_provenance，两参数通过 |
| P2-02 new head CAS | 原Ledger Agent已补新head revision0检查，含已有version缺head路径。 | test_memory_ledger.py，10项通过 |
| P2-03 detach | detach只清除installation_id，保留archived原值；只有archive改生命周期。可重新绑定，源码保留。 | test_detach_preserves_active_project_and_source |
| P2-04 effective_at | 管理设置按scope/key+实际value/source独立持久时间；无关项目改名不变。移除的设置丢弃旧时间，重新绑定重新生效。旧状态无法还原历史时显示unknown。Role用不可变版本created_at。 | test_setting_times_follow_values_and_role_versions |

Root管理定向16passed，日志gates/reviewer-management-fixes.log。此前完整集成881passed/1skip，GUI101及构建通过，tests格式修正复核通过；这份完整证据在上述Reviewer改动前，不冒用为最新源码完成证据。需要Ledger交还后做集成门禁及原Reviewer增量确认。

原生早期detach确实归档的证据已失效，需要GUI恢复操作后按修复语义重验；settings新时间与当前安装包检索增量也尚待原生读回。真实模型仍未通过。
