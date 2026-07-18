from core.Task.Task import Task
from core.Agent.Agent import Agent


def process_user_task(task: Task):
    user=task.user
    try:
        Agent.process_task(task)
    except:
        task.set_temp_dialog_output("服务繁忙")
        user.send(task)
