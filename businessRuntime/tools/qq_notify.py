"""视频报告通知兼容入口。参数text为后台任务摘要；只存通知队列，不读取任何QQ账号。"""

from newsDigest.push.qqPush import qqPush


def send_qq_text_message(text, dry_run=False):
    """text为结果正文，dry_run只返回预览；正常调用保存远端通知。"""
    return {"ok": True, "mode": "dry-run"} if dry_run else qqPush().push(text)
