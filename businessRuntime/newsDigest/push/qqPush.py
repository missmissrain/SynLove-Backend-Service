"""旧推送接口的远端适配器。参数保留调用兼容；不读取账号密钥，不连接QQ。"""

import hashlib
import os
import time
from pathlib import Path
from microServices.NotificationStore import NotificationStore


class qqPush:
    """后台结果先持久保存，由个人实例接收后选择消息通道发送。"""

    def __init__(self, secrets_path=None, openid=None, allow_wakeup=True):
        """旧参数仅用于兼容；通知库由服务端环境指定。"""
        root = Path(__file__).resolve().parents[2]
        self.store = NotificationStore(os.environ.get("SYNLOVE_NOTIFICATION_DB") or
                                       root / "runtime" / "notifications.sqlite")

    def push(self, text, event_key=None):
        """text为正文；event_key使用提醒到期ID，新闻默认使用当日正文摘要。"""
        operation = os.environ.get("SYNLOVE_OPERATION_ID")
        key = event_key or ("result:" + operation if operation else
                            "news:" + time.strftime("%Y-%m-%d") + ":" +
                            hashlib.sha256(text.encode()).hexdigest())
        ident = self.store.enqueue(text, key)
        return {"ok": True, "id": ident, "detail": "结果已入队，等待个人实例接收"}
