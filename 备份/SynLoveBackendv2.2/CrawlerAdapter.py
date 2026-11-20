"""集成爬虫工具。CrawlerAdapter(root) 提供有界公开网页抓取与已有浏览器采集，不允许任意脚本。"""

import ipaddress
import http.client
import socket
from urllib.parse import urlsplit


class CrawlerAdapter:
    """只抓公开HTTP(S)地址；DNS校验后固定连接IP，不跟随重定向。"""

    def __init__(self, root):
        """root为服务器代码根目录，浏览器路径与登录态只由服务器维护。"""
        self.root = root

    def validate_url(self, value):
        """value为目标URL；拒绝凭据、私网/回环/链路本地地址和非标准端口。"""
        url = urlsplit(value)
        if url.scheme not in ("http", "https") or not url.hostname or url.username or url.password:
            raise ValueError("只允许公开HTTP(S)地址")
        if url.port not in (None, 80, 443):
            raise ValueError("只允许80和443端口")
        rows = socket.getaddrinfo(url.hostname, url.port or (443 if url.scheme == "https" else 80),
                                  type=socket.SOCK_STREAM)
        if not rows:
            raise ValueError("目标没有可用地址")
        for item in rows:
            if not ipaddress.ip_address(item[4][0]).is_global:
                raise ValueError("禁止爬取内网和本机地址")
        return url, rows[0][4][0]

    def fetch_http(self, value, with_headers=False):
        """value为公开URL；固定IP并校验TLS，最多2MiB；with_headers为浏览器返回正文及MIME。"""
        url, ip = self.validate_url(value)
        kind = http.client.HTTPSConnection if url.scheme == "https" else http.client.HTTPConnection
        port = url.port or (443 if url.scheme == "https" else 80)
        conn = kind(url.hostname, port, timeout=30)
        # HTTPSConnection.connect仍以原hostname作为SNI与证书校验名。
        conn._create_connection = lambda address, timeout, source_address=None: socket.create_connection(
            (ip, port), timeout, source_address)
        try:
            target = url.path or "/"
            if url.query:
                target += "?" + url.query
            conn.request("GET", target, headers={"User-Agent": "SynLoveCrawler/1.0"})
            response = conn.getresponse()
            if not 200 <= response.status < 300:
                raise ValueError(f"网页返回HTTP {response.status}；不自动跟随跳转")
            data = response.read(2 * 1024 * 1024 + 1)
            if len(data) > 2 * 1024 * 1024:
                raise ValueError("网页超过2MiB")
            from email.message import Message
            headers = Message()
            headers["Content-Type"] = response.getheader("Content-Type", "")
            text = data.decode(headers.get_content_charset() or "utf-8", errors="replace")
            return (text, headers.get_content_type()) if with_headers else text[:30000]
        finally:
            conn.close()

    def handle(self, message):
        """message为MCP请求；返回抓取内容或结构化新闻条目。"""
        key = message.get("id")
        if message.get("method") == "tools/list":
            return {"jsonrpc": "2.0", "id": key, "result": {"tools": [{
                "name": "crawler_fetch", "description": "默认用隔离浏览器收集公开网页并由Gemma整理；mode=http可仅抓原文。拒绝私网和跳转。",
                "inputSchema": {"type": "object", "properties": {
                    "url": {"type": "string"},
                    "mode": {"type": "string", "enum": ["http", "browser"]},
                    "summarize": {"type": "boolean", "default": True},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 50}},
                    "required": ["url"]}}]}}
        params = message.get("params", {})
        if params.get("name") != "crawler_fetch":
            raise ValueError("未知爬虫工具")
        args = params.get("arguments", {})
        mode = args.get("mode", "browser")
        if mode not in ("http", "browser"):
            raise ValueError("mode只允许http或browser")
        if mode == "browser":
            from microServices.WebCollection import WebCollection
            text = WebCollection(self.root).collect(str(args.get("url", "")), args.get("summarize", True))
        else:
            text = self.fetch_http(str(args.get("url", "")))
        return {"jsonrpc": "2.0", "id": key, "result": {"content": [{"type": "text", "text": text}]}}
