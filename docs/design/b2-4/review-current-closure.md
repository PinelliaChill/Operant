本片结论：原 Graph 目录 P1/P2 均关闭；未发现新增 P1/P2。

- P1 workspace 泄漏已关闭：无 `workspace` 只返回定义目录；指定 workspace 才查询 Graph，且 cursor 绑定 workspace。[api_b2_4.py:143](/Users/bigo/agentworkspace/codexworkspace/operant/.recovery/b2-4-20260914/delivery/src/operant/api_b2_4.py:143)
- P2 截断/无法找回旧运行已关闭：服务端每页最多 100 条，使用 `updated_at + id` 稳定 keyset cursor；GUI 分页合并去重，刷新和切换 workspace 清空旧投影。[api_b2_4.py:182](/Users/bigo/agentworkspace/codexworkspace/operant/.recovery/b2-4-20260914/delivery/src/operant/api_b2_4.py:182)、[b24-client.ts:196](/Users/bigo/agentworkspace/codexworkspace/operant/.recovery/b2-4-20260914/delivery/clients/gui/src/features/collab/b24-client.ts:196)、[LiveGraphTeamView.tsx:223](/Users/bigo/agentworkspace/codexworkspace/operant/.recovery/b2-4-20260914/delivery/clients/gui/src/features/collab/LiveGraphTeamView.tsx:223)
- 契约与 Python/TS SDK 已包含 `workspace`、`cursor` 和分页字段。[b2_4.py:51](/Users/bigo/agentworkspace/codexworkspace/operant/.recovery/b2-4-20260914/delivery/src/operant/contracts/b2_4.py:51)、[b2_4.generated.ts:560](/Users/bigo/agentworkspace/codexworkspace/operant/.recovery/b2-4-20260914/delivery/sdk/typescript-client/b2_4.generated.ts:560)
- 定向测试证据覆盖双 workspace、101 条、跨页、anchor 更新、foreign/malformed cursor；native06 证明第二页找回 Graph/Team，Enter 未产生重复 POST。未执行测试，仅读已有日志。

限制：workspace 只是发现范围过滤，不是新的多租户 ACL。`directory-pagination-validation.json` 中三份 GUI SHA 是 109 项目录切片的旧指纹；75de guard 增量由 native06 freeze 与 110 项 GUI 日志另行覆盖。Host 性能门仍失败，不能据此宣布整批完成。

实际工作树 HEAD 为 `4d07c5a4...`，目标文件相对 `75de71c` 无差异。当前 SHA：

```text
api_b2_4.py                 47de5bb514b6101d7059112029e7e23ca4da4755bfc86d25f6d5b334cba50230
contracts/b2_4.py           a88dcf603f2715a0a3c897eacce6074a9f7ca4e24094ce6e02eb6f2c914a51a5
test_b24_graph_api.py       ba522ba5e06ae569a6953c97fd83b7a6c404192a823e4c42370b5b3c5d7d4a8b
b2_4_generated.py           e4de63f475b50bed371efeb9795ef444b3da79c59a454afbf3e0cdbc92c7a6a7
b2_4.generated.ts           d5f4ace12e7bab1563dfb3dbf1c1e5a56f973d9d90214b445b694206c26b98b3
LiveGraphTeamView.tsx       f010dbac771268551520e11a2d0cd178746b2ba0f72754d8f503b42547ce42f3
b24-client.ts               63db462e0119532bcfcf556659ef298d5e046672f5424cd79f84fabb389a5a0e
b24-client.test.ts          d51a4d6d3c166afd6d3f75f679933ec8e5973b418e17d0eb617433fbf4355c5f
```

J2 补证支持其声明范围：`gpt-5.6-luna`、low、3 次实际调用、42 output tokens；nextsend 的撤销后 0 Provider 与强依赖 409 也有记录。原生分页最新验收范围仍以 native06 为限。
