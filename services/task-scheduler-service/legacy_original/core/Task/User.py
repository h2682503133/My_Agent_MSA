from typing import overload

from typing_extensions import override


class User:
    """
    用户实体类
    职责：仅封装用户标识 + 消息推送，不处理任何任务/队列逻辑
    """
    def __init__(self, user_id: str, session_id: str, output):
        # 用户唯一ID
        self.id = user_id
        # 会话ID（同一对话窗口不变）
        self.session_id = session_id
        # 底层推送接口（WebOutput/QQOutput，实现send(text)方法）
        self.output = output

    def send(self, task) -> None:

        images = task.send_images or []
        text = task.send_text or ""

        if len(task.send_images)==0 and  task.send_text=="":
            text = task.consume_temp_dialog_input()

            # 处理文本
            if not isinstance(text, str):
                if task is not None:
                    text = f"{text[2]}:{text[3]}"
                else:
                    text = "空回复"

        # 读取图片（从 task 专用变量）
        """
        统一消息发送入口
        文本 + 图片 分离，全渠道兼容
        """
        # 传给 output：结构通用
        self.output.send({
            "user_id": self.id,
            "text": text,
            "images": images
        })

        # 重要：发完清空中间变量（避免重复发）
        task.send_images = []
        task.send_text = ""
