# Host受管资源安全增量

记录身份：Codex；适用对象：原B2-2 Reviewer。

候选1b0fb4c。只读完本条及对应diff即可继续，不用重读历史。

确认缺陷：旧path_under先检查目录，后续os.open仅O_NOFOLLOW保护末级文件。受控临时目录探针在检查后替换中间目录为指向另一临时目录的链接，outside/index哨兵确实被修改。

修复：protocol._managed_parent逐级打开目录并保持dir_fd，拒绝所有符号链接；读、写、删、资源登记均使用该句柄。写入在fstat确认普通文件/单链接后才truncate，避免硬链接提前截断。非阻塞打开避免FIFO挂起；读大小在读取前及循环中受响应预算约束。没有修改公共Schema或插件运行模式。

定向结果：Host25项通过，含新增父目录替换后的read/write/delete/register四场景、硬链接及读取上限、目录已打开后被rename仍写入原目录的场景。实际macOS隔离stdio两次RPC复核通过；实际受信任package入口加载由同次Host定向测试覆盖。对应测试见test_plugin_host.py与test_plugin_host_stdio.py，外部所有哨兵仅位于隔离临时测试目录。

独立审查尚未完成：原Reviewer额度恢复后须复核此安全增量，不能只复核已修复的表单对比度。1b0fb4c完整基础门禁已通过（772 passed/1 Docker skip，GUI91），结果由verification.json记录。
