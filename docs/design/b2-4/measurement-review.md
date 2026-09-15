# B2-4 观测采样方法复核

记录身份：Codex；适用对象：B2-4；2026-09-15。

独立 Reviewer：Codex 子 Agent `/root/measurement_reviewer`，gpt-5.6-luna/max，Fast 未启用。只读审查基于产品检查点 06351e9 的脚本与 MP-0/MP-3.5 要求；本结论不验收 C 原型、实现或性能。

结论：有条件支持把 benchmark 自加的 phase/RPC 观测移至独立 observation pass。这是消除测量干扰；不能改算法、比例、冻结阈值，不能从正式样本中扣减观测时长。

来源：旧 `scripts/benchmark_b24_memory.py` 的 855–871 行在正式调用内计时 Manager，893–925 行包装三个 Registry 方法，935–950 行为隔离模式统计重复编码。冻结要求见 `docs/design/b2-1/evaluation-baseline.md` 25–29 行和治理根记忆设计 634–640 行。

必要条件：

1. timing、allocation、observation 使用独立临时 SQLite、Registry、Host/进程；fixture、请求顺序和数量、权限、配置与启动健康检查语义一致。正式 timing 只测真实调用，allocation 只额外启用 tracemalloc。
2. 所有生产 lease、epoch、包完整性、认证、权限、预算和撤销检查继续执行。观测包装必须调用原方法，不能缓存验证结果或跳过检查。
3. 逐请求核对返回 ID、质量、保留集和 forbidden hits；2×42 样本的 trusted 观察调用数为 84、各 Registry 方法 588，isolated 为 84/336，排除启动。
4. 独立 `observation_pass` 承载 phase/RPC。无法直接捕获而由编码重建的字节标为 reconstructed。正式门禁仅读取 clean timing/allocation 原始值；保留原失败报告，不追溯改判。
5. 测试证明额外观测不会进入正式样本、生产检查仍执行、三个 pass 输入/结果一致、差异被拒绝，且计数/RPC 含义清楚。

下一步：实施测量脚本重构、定向验证，再按固定阈值重新测量；本审查尚不等于实现复核或性能通过。
