# B2-3 管理接口

记录身份：Codex；适用对象：实现与审查；2026-09-13。本页为入口摘要，字段唯一权威为 `src/operant/contracts/b2_3.py` 与生成OpenAPI，不复制完整DTO。

- 协商 `GET /v1/protocol/b2-3`；读取 `GET /v1/b2-3/management`；命令 `POST /v1/b2-3/commands`。协议b2-3.v1，生成器 `sdk/protocol/generate_b2_3.py`，生成Python/TypeScript B23Client。
- 项目：create/update/archive/detach；绑定注册Workspace，归档/解除不删除源码或历史。
- 插件：install/enable/disable/configure/uninstall，安装可显式接回同插件的retained dataset；卸载必须选择keep或delete。
- 记忆：save/propose/confirm/deactivate/search/migrate；propose始终候选，confirm/deactivate要求expected_revision。全局memory_switch优先于项目覆盖。
- 数据集：export/delete/cleanup_resume；保留态无插件也可管理，物理清理要求持久计划及Host屏障。独立导出不随dataset清除。
- Skill：discover/install/enable/disable/uninstall，package_ref来自可信目录发现，启停按项目，不增加Tool Policy权限。
- Artifact：pin/archive/schedule/trash/restore/audit；复用既有保留/宽限期/保护对象规则，不提供物理purge。

所有ID由服务端返回；每动作必填值由Core检查。结果包含status/message/最新ManagementState，以及可空export_data/records。业务拒绝保留可用状态；网络、协议、未知写结果显式阻断，不自动重放。命令经ActionGateway及完整幂等journal，删除后缓存数据结果失效。

插件共用冻结MP-0 Host SDK。memory_version来源读取使用既有SourceRef/HostReadRequest，仅当前dataset同scope/epoch、当前published版本和精确digest可读。笔记插件据此对Host粗筛候选做精确key过滤，Core不硬编码插件匹配策略。认证进程内可缓存私有索引，隔离模式同样经授权读取过滤；两者不能使用候选、停用或旧版本。

实际证据见 `sdk-generation.json`、`client-smoke.json`、`isolated-exact-acceptance.json`。J1未齐与真实模型失败仍在 `desktop-acceptance.md` 标明，不因协议/生成通过而标交付完成。
