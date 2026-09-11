# B2-2 Host 审查修复增量

记录身份：Codex；适用对象：原Reviewer、User。代码518bb3f；不是独立通过报告。

- 8baf09f：旧lease终态释放幂等，最高fencing身份保护新run标记；受限回调逐scope/permission epoch/availability/dataset及Core权威授权检查，模型来自绑定配置。
- 34542eb：同一授权应用于Host.invoke四数据operations的入参/返回值；来源、版本引用、Head、Proposal需对应Core授权器，缺失拒绝，结果检查request/event/watermark。生命周期握手保留原协议。
- Manifest映射：search需recall；read_source需extract/recall/maintain；提取/重排模型分别需extract/recall及对应Profile。实际生产入口测试拒绝越权，非旁路fixture通过替代。
- Cleanup按目录句柄递归删除，不跟随symlink；解析异常纳入blocked Receipt，恢复后可续做。停止、卸载、Host关闭先阻止新运行，再有界收束在途task与钩子；超时保留task/engine/资源并返回restart_required。enable/start拒绝同Host绕过fence；同步close/cleanup移至线程但不宣称可以强杀线程。
- 定向：payload20 + Host invoke集成8 + callback12 = 40通过；生命周期9 + sync close2 + callback12 = 23通过。最后局部日志在临时目录lifecycle-final.log。
- 最终代码真实macOS隔离启动/两次授权RPC/Host关闭通过，见host-isolation-smoke.json。34542eb门禁为826pass/1已修fixture失败/1Docker skip；最终518bb3f完整门禁847pass/1Docker skip、GUI91及全部静态/锁检查通过，见verification.json。
- caa2ba8客户端/Session源文件未改变，复用原Reviewer已签署切片及client-review-increment.json；实际原生流程无需因Host独立修改再跑。

- b34de8c：资源登记先计调用/请求预算再创建，返回计响应预算；超限前不新增资源。旧测试手工构造的无task占位显式清理，不弱化生产关闭检查。
- 988fb24：当前extract仅可使用extraction_profile，recall仅rerank_profile；两方向合法/越阶段请求经实际invoke验证。
- 518bb3f：stop/uninstall/resume_cleanup按installation串行，close自身互斥；关闭期间排队请求得到明确restart_required回执。九种两两并发生命周期组合通过。maintenance_enabled=False拒绝维护，recall/嵌套search预算不得高于配置；六项允许/拒绝回归通过。空召回夹具明确token_budget=0，不虚构上下文分配；非零预算独立验证。
- 原Luna/max Reviewer最后turn 01a0906f-d947-7d51-8682-6b04d3ce191c再次workspace credits失败。它已关闭先前P1并核到988fb24；最后并发P1和配置约束修复仍须恢复后独立复核，不能视为整批通过。
