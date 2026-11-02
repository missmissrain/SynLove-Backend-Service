"""业务CLI兼容入口。ServiceCli(name,root) 把旧命令行参数转换为API调用，不直接访问业务库。"""

import argparse

from microServices.ServiceMcpBridge import ServiceMcpBridge


class ServiceCli:
    """保留原有提醒/新闻/视频命令，执行权统一交给服务端。"""

    def __init__(self, name, root):
        """name为reminders/digest/video，root为调用机代码目录。"""
        self.name, self.root = name, root

    def run(self, argv):
        """argv为原命令参数列表；成功返回0，工具或传输失败返回1。"""
        parser = argparse.ArgumentParser()
        if self.name == "reminders":
            group = parser.add_mutually_exclusive_group(required=True)
            group.add_argument("--add")
            group.add_argument("--list", action="store_true")
            group.add_argument("--delete", type=int)
            group.add_argument("--loop", action="store_true")
            parser.add_argument("--at")
            parser.add_argument("--daily")
            parser.add_argument("--in", dest="seconds", type=int)
            args = parser.parse_args(argv)
            if args.loop:
                bridge = ServiceMcpBridge(self.name, self.root)
                bridge.manager.ensure_service(self.name)
                print("提醒由独立API服务调度，此命令不创建第二个轮询器。")
                return 0
            name, values = "remind_list", {}
            if args.add is not None:
                name, values = "remind_add", {"text": args.add}
                values.update({key: val for key, val in
                               (("at", args.at), ("dailyAt", args.daily), ("inSeconds", args.seconds))
                               if val is not None})
            elif args.delete is not None:
                name, values = "remind_delete", {"id": args.delete}
        elif self.name == "digest":
            parser.add_argument("--dry-run", action="store_true")
            parser.add_argument("--limit", type=int)
            args = parser.parse_args(argv)
            name, values = "digest_run", {"dry_run": args.dry_run}
            if args.limit is not None:
                values["limit"] = args.limit
        else:
            group = parser.add_mutually_exclusive_group(required=True)
            group.add_argument("--url")
            group.add_argument("--file")
            parser.add_argument("--frames", type=int)
            parser.add_argument("--language")
            args = parser.parse_args(argv)
            name = "video_digest"
            values = {"pathOrUrl": args.url or args.file}
            values.update({key: val for key, val in (("frames", args.frames), ("language", args.language))
                           if val is not None})
        result = ServiceMcpBridge(self.name, self.root).handle({
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": name, "arguments": values}})
        body = result.get("result", {})
        for block in body.get("content", []):
            if block.get("type") == "text":
                print(block.get("text", ""))
        return 1 if "error" in result or body.get("isError") else 0
