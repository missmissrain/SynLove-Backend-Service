"""RSSHub 采集源：从本机 RSSHub 拉取 RSS，返回统一新闻条目。

职责：
- 请求 `http://127.0.0.1:1200/<path>`，解析 RSS 的 title/link/pubDate；
- 不启动浏览器、不解析网页；
- 失败抛异常，由主编排统一记录。

注册参数（构造）：base（RSSHub 根地址）、timeout（秒）
"""

import sys
import urllib.request
import xml.etree.ElementTree as ET

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/122 Safari/537.36"


class rsshubSource:
    """RSSHub RSS 读取器。"""

    def __init__(self, base="http://127.0.0.1:1200", timeout=30):
        """base：RSSHub 地址；timeout：单次请求超时。"""
        self.base = str(base).rstrip("/")
        self.timeout = int(timeout)

    def fetch(self, path, limit=40):
        """拉取一个 RSS 路由，返回 [{title, url, time}]。"""
        url = self.base + ("/" + str(path).lstrip("/"))
        request = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/rss+xml, application/xml"})
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            raw = response.read()
        root = ET.fromstring(raw)
        items = []
        for item in root.iter("item"):
            title = (item.findtext("title") or "").strip()
            link = (item.findtext("link") or "").strip()
            pub = (item.findtext("pubDate") or "").strip()
            if not title or not link:
                continue
            items.append({"title": title, "url": link, "time": pub})
            if len(items) >= limit:
                break
        return items
