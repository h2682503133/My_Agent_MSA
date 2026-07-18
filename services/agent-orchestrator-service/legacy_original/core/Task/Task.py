"""
原 core/Task/Task.py 的关键职责已经迁移到 app/task_runtime.py。

保留点：
- agent_context
- push_context()
- pop_context()
- temp_dialog_input / temp_dialog_output
- main_memory
- tool_log
- main_log

变化：
- TaskRuntime 只存在于 agent-orchestrator-service 内部
- 不再跨服务传输
- scheduler 不再持有该对象
"""
