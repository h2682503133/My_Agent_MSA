这里放置原仓库 core/Agent 相关文件的迁移说明。

本次拆分使用/参考的原文件：
- core/Agent/Agent.py
- core/Agent/response_parser.py
- core/Agent/syntax_parser.py
- core/Agent/Tool_manager.py
- core/Agent/Skill_manager.py

由于当前执行环境无法直接 git clone GitHub 仓库，完整原文请以原仓库为准。
本服务运行态中已经把可直接复用的解析逻辑拆入：
- app/response_parser.py
- app/syntax_parser.py
- app/task_runtime.py
- app/agent_runtime.py
