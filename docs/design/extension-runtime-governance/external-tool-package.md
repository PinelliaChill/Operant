# 隔离第三方 Tool 与本机扩展包格式

> 更新：2026-10-04；记录身份：Codex；适用对象：本实施分支的本地安装者与集成者。

此入口接收显式安装的本地扩展包。原有 `operant-tool-extension.v1` 继续只支持 Tool；`operant-local-extension.v1` 开放 Tool、Command、Event、Provider、Runtime 与能力驱动六类声明。插件代码不会被 Core 导入；启用前必须通过 macOS `sandbox-exec` 的实际文件与网络拒绝探测。没有可用隔离器的平台会拒绝启用。插件不能读取工作区或 Core 数据库，不能联网，也不会继承 Core 的 Key/Token 环境变量。需要这些能力的工作应走现有受控 Tool、MCP 或能力 Target，不把权限加给此包格式。

## 包内容

来源目录须由当前用户或 root 所有，且组和其他用户不可写；拒绝 `..` 路径段及来源目录符号链接。目录中放两个普通文件，不允许符号链接或 FIFO。检查和安装在规范化并验证目录边界后，固定目录句柄读取这两个文件。安装后的托管目录只允许清单、脚本和 Host 生成的 runner；添加其他文件会使调用失败：

```text
my-tool/
  manifest.json
  plugin.py
```

旧 Tool 包的 `manifest.json` 示例：

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

新版清单仍是上述两个文件，只把 `host_api_version` 改为 `operant-local-extension.v1`，并按需加入 `tools`、`commands`、`events`、`providers`、`runtimes`、`capability_drivers` 数组。六类不能以空声明代替实现；每个操作沿用上述 `name`、`description`、平铺 `parameters`。所有操作名在包内唯一，并以 `ext_<plugin_id>_` 开头。调用请求增加 `category`，其值为六类的单数名；`tool` 字段仍是该操作的原始名称。示例：

```json
{
  "plugin_id": "sample_ext",
  "version": "1.0.0",
  "host_api_version": "operant-local-extension.v1",
  "commands": [{
    "name": "ext_sample_ext_status",
    "description": "Return a short status.",
    "parameters": {
      "type": "object",
      "properties": {"label": {"type": "string"}},
      "required": ["label"],
      "additionalProperties": false
    }
  }],
  "events": [{
    "name": "ext_sample_ext_event",
    "description": "Observe a committed event type.",
    "parameters": {
      "type": "object",
      "properties": {"payload_json": {"type": "string"}},
      "required": ["payload_json"],
      "additionalProperties": false
    }
  }]
}
```

各类消费边界如下。**Tool** 必须另外写入 Role 的允许工具清单，正式 Agent Tool Policy、Action Gateway 与隔离执行共同生效。**Command** 可从 `/v1/workbench/extensions/commands` 发现，以安装身份绑定的名称在 `/v1/workbench/threads/{thread_id}/extension-commands` 显式调用；参数按清单校验，结果写入会话 Event 与 Thread 历史，重复请求用同一个幂等键回放，结果不明要求人工核对。GUI/TUI 输入 `/<授权名称> {"label":"value"}`。**Event** 只接收 Core 已持久化的 `model.completed`、`tool.completed`、`tool.failed`、`agent.completed`、`agent.failed`、`agent.cancelled` 类型及脱敏字段名，入参固定为 `payload_json`；回执与错误类型写回正式 Session Event，不接受插件改写原事件。

**Provider** 的 `payload_json` 只含冻结 Profile 的 `model_id`、`base_url`。返回值必须恰为这两个字段；URL 只能在同一协议、主机、端口内改路径，模型 ID 必须由同源正式 Discovery 精确返回。Core 保管 `secret_ref`、提示词、工具、预算和传输，插件不接触凭据。ModelProfile 的 `provider` 指向带安装 ID 的授权操作名，正式 `run_session` 才会消费它。**Runtime** 在每次正式 Provider 请求前接收 `model_id` 和当前 `max_output_tokens`，可返回 `{}` 或更小的正整数上限；Core 仍裁决取消、预算、权限、恢复及终态，插件不能放宽上限。Event、Provider、Runtime 声明的入参字段只能是 `payload_json`。**能力驱动** 可从 `/v1/extensions/drivers` 发现；本机控制会话的 `drive` 调用隔离包，只接受精确的 `{ "operation": str, "arguments": object }` 建议，再由现有 Target 观察、租约、Action Gateway 与 `act` 执行。建议不等于副作用成功；禁用、接管、撤销及结果不明依旧由 Core 判断。

## 安装与授权

```bash
operant capability-plugin inspect-external-tool --source /absolute/path/my-tool
operant capability-plugin install-external-tool --source /absolute/path/my-tool --expected-sha256 DIGEST_FROM_INSPECT
operant capability-plugin list-external-tools
operant capability-plugin enable-external-tool sample_tool
operant role update ROLE_ID --enable-extension-tool GRANTED_TOOL_NAME_FROM_LIST
```

先检查插件 ID、工具名和包摘要，确认来源后在安装命令中固定该摘要；若源文件在两步之间变化，安装会拒绝。安装会复制声明文件到私有 Host 目录并默认禁用。`list-external-tools` 返回带本次安装 ID 的授权名，例如 `ext_sample_tool_count__a1b2c3d4e5f6`。重装会生成新授权名，旧 Role 不会自动获得新代码，旧会话缓存的工具也会因安装 ID 不同而拒绝执行。托管的 data/state/logs/tmp 目录也按安装 ID 分开；旧数据保留供人工检查，新安装的沙箱不能读取。启用会验证实际隔离；每次调用重查源码摘要、启用状态、参数与资源限制，再通过正式 Agent Tool Policy、Action Gateway 和隔离进程执行。禁用后不接收新调用；卸载前必须禁用，卸载仅删除托管代码，旧数据目录保留。

```bash
operant capability-plugin disable-external-tool sample_tool
operant capability-plugin uninstall-external-tool sample_tool
```

进程启动后的超时、异常退出或无效回执均按结果不明处理；Agent 的副作用回执不能据此标为已安全失败并自动重放。

新版 CLI 管理命令如下，GUI 的「本机扩展」页调用同一正式 API。安装后默认禁用；启用时必须逐类授权，列表会显示包摘要、安装 ID、隔离证据和授权类别。停用立即阻止新调用，卸载前必须停用；重装产生新安装 ID，旧授权名不沿用。

```bash
operant extension inspect --source /absolute/path/sample-ext
operant extension install --source /absolute/path/sample-ext --expected-sha256 DIGEST_FROM_INSPECT
operant extension list
operant extension enable sample_ext --grant command --grant event
operant extension disable sample_ext
operant extension uninstall sample_ext
```

HTTP 管理写入走本机鉴权、Action Gateway 与幂等收据；审批待处理时同键批准后可继续，已完成同键回放，执行结果不明不会盲目再调插件。原 `capability-plugin *-external-tool` 命令与 `operant-tool-extension.v1` 清单保持兼容；旧包不会因此自动获得新增类别。
