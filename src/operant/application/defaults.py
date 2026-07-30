from __future__ import annotations

from operant.domain.models import Budget, Effort, RolePreset, ToolPolicy


def default_role_presets(
    *,
    planner_model_profile_id: str,
    coder_model_profile_id: str,
    reviewer_model_profile_id: str,
) -> tuple[RolePreset, ...]:
    readonly_tools = ToolPolicy(
        allowed_tools=("read_file", "search_files", "git_diff"),
    )
    coder_tools = ToolPolicy(
        allowed_tools=(
            "read_file",
            "search_files",
            "apply_patch",
            "run_command",
            "git_diff",
        ),
        workspace_write=True,
        command_execution=True,
    )
    return (
        RolePreset(
            id="role_main",
            name="Main",
            system_prompt=(
                "你负责理解用户目标、选择合适的执行角色，并汇总最终结果。"
                "不要绕过角色权限，也不要直接执行未获授权的高风险操作。"
            ),
            model_profile_id=planner_model_profile_id,
            effort=Effort.HIGH,
            tool_policy=readonly_tools,
            budget=Budget(max_turns=12),
        ),
        RolePreset(
            id="role_planner",
            name="Planner",
            system_prompt=(
                "你负责分析任务、检查相关代码，并给出可执行计划和完成条件。只读，不修改 workspace。"
            ),
            model_profile_id=planner_model_profile_id,
            effort=Effort.HIGH,
            tool_policy=readonly_tools,
            budget=Budget(max_turns=8),
        ),
        RolePreset(
            id="role_explorer",
            name="Explorer",
            system_prompt=(
                "你负责检索和理解代码，返回文件、符号、调用链和证据。只读，不修改 workspace。"
            ),
            model_profile_id=planner_model_profile_id,
            effort=Effort.MEDIUM,
            tool_policy=readonly_tools,
            budget=Budget(max_turns=8),
        ),
        RolePreset(
            id="role_coder",
            name="Coder",
            system_prompt=(
                "你负责在指定 workspace 中实现任务。先读取相关代码，再做最小修改，"
                "运行测试并检查 diff。高风险操作必须等待审批。"
            ),
            model_profile_id=coder_model_profile_id,
            effort=Effort.HIGH,
            tool_policy=coder_tools,
            budget=Budget(max_turns=20, timeout_seconds=600),
        ),
        RolePreset(
            id="role_reviewer",
            name="Reviewer",
            system_prompt=(
                "你负责只读审查需求、计划、diff 和测试结果。指出具体缺陷、风险和遗漏，"
                "不要修改 workspace。"
            ),
            model_profile_id=reviewer_model_profile_id,
            effort=Effort.HIGH,
            tool_policy=readonly_tools,
            budget=Budget(max_turns=10),
        ),
    )
