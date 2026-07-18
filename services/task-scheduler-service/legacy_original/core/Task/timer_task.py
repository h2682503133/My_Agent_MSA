# 原 core/Task/timer_task.py 文件较长；迁移版本见 app/timer_task.py。
# 原始逻辑要点：
# - 以 JSON 文件保存定时任务
# - timer_scan_loop 后台扫描 trigger_time
# - task_type=submit_task 时 POST main /submit_task
# - task_type=send_message 时 POST callback_port /send
# 微服务化后，HTTP main 调用被替换为 submit_task(runtime_task)，直接进入 scheduler 队列。
