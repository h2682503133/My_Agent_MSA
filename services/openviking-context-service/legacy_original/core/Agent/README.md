迁移参考：

- 原 Agent.py 中 OpenViking 初始化、session、get_context_sync、add_message 逻辑迁移到 app/viking_store.py
- 原 Skill_manager.py 中 SyncOpenViking、add_skill、ls、read 逻辑迁移到 app/viking_store.py
