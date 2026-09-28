# 隔离第三方 Tool 包格式

> 2026-09-28；记录身份：Codex；适用对象：本实施分支的本地安装者与集成者。

此入口只接收显式安装的本地 Tool 包。插件代码不会被 Core 导入；启用前必须通过 macOS `sandbox-exec` 的实际文件与网络拒绝探测。没有可用隔离器的平台会拒绝启用。插件不能读取工作区或 Core 数据库，不能联网，也不会继承 Core 的 Key/Token 环境变量。需要这些能力的工作应走现有受控 Tool、MCP 或能力 Target，不把权限加给此包格式。

## 包内容

目录中放两个普通文件，不允许符号链接。安装后的托管目录只允许清单、脚本和 Host 生成的 runner；添加其他文件会使调用失败：

```text
my-tool/
  manifest.json
  plugin.py
```

`manifest.json` 示例：

```json
{
  "plugin_id": "sample_tool",
  "version": "1.0.0",
  "host_api_version": "operant-tool-extension.v1",
  "tools": [
    {
      "name": "ext_sample_tool_count",
      "description": "Count characters in the provided text.",
      "parameters": {
        "type": "object",
        "properties": {"text": {"type": "string"}},
        "required": ["text"],
        "additionalProperties": false
      }
    }
  ]
}
```

`plugin.py` 示例：

```python
import json
import sys

request = json.loads(sys.stdin.buffer.read())
if request["host_api_version"] != "operant-tool-extension.v1":
    raise SystemExit(2)
if request["tool"] != "ext_sample_tool_count":
    raise SystemExit(2)
text = request["arguments"]["text"]
print(json.dumps({"result": {"characters": len(text)}}))
```

每次调用只在标准输入传一个 JSON 对象，标准输出只接受 `{"result": {...}}`。参数 Schema 目前只接受最多 20 个平铺的字符串、整数、数字或布尔字段，拒绝额外字段；输出上限 64 KiB。工具名必须位于包名命名空间 `ext_<plugin_id>_...`，内置 Browser/Computer 名称保留。插件描述会标为第三方不可信内容，结果带 `trust: untrusted` 来源标记。

## 安装与授权

```bash
operant capability-plugin inspect-external-tool --source /absolute/path/my-tool
operant capability-plugin install-external-tool --source /absolute/path/my-tool --expected-sha256 DIGEST_FROM_INSPECT
operant capability-plugin list-external-tools
operant capability-plugin enable-external-tool sample_tool
operant role update ROLE_ID --enable-extension-tool GRANTED_TOOL_NAME_FROM_LIST
```

先检查插件 ID、工具名和包摘要，确认来源后在安装命令中固定该摘要；若源文件在两步之间变化，安装会拒绝。安装会复制声明文件到私有 Host 目录并默认禁用。`list-external-tools` 返回带本次安装 ID 的授权名，例如 `ext_sample_tool_count__a1b2c3d4e5f6`。重装会生成新授权名，旧 Role 不会自动获得新代码，旧会话缓存的工具也会因安装 ID 不同而拒绝执行。启用会验证实际隔离；每次调用重查源码摘要、启用状态、参数与资源限制，再通过正式 Agent Tool Policy、Action Gateway 和隔离进程执行。禁用后不接收新调用；卸载前必须禁用，卸载仅删除托管代码，数据目录保留供人工检查。

```bash
operant capability-plugin disable-external-tool sample_tool
operant capability-plugin uninstall-external-tool sample_tool
```

进程启动后的超时、异常退出或无效回执均按结果不明处理；Agent 的副作用回执不能据此标为已安全失败并自动重放。当前 Host API 只开放无网络的第三方 Tool。Command、Event、Provider/Runtime 适配及第三方 Browser/Computer 驱动仍需各自明确权限与恢复契约，不能用本格式冒充已交付。
