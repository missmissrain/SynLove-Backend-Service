"""热点采集：微博热搜 / B 站热搜（公开 API）+ RSSHub 热榜（知乎/36氪/酷安）。

职责：
- **不启动浏览器**，只调公开 JSON API 与本机 RSSHub；
- 统一成 [{source, title, url, heat}]，按来源限量、标题去重；
- 单个来源失败不影响其它来源（记录在 errors 里）。

注册参数（构造）：
    rsshub_base   本机 RSSHub 地址（默认 http://127.0.0.1:1200）
    max_weibo     微博热搜条数（默认 3）
    max_bilibili  B 站热搜条数（默认 2）
    max_rsshub    每个 RSSHub 热榜条数（默认 2）
"""

import json
import sys
import urllib.parse
import urllib.request

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/122 Safari/537.36"
RSSHUB_HOT = (
    ("知乎热榜", "/zhihu/pin/hotlist"),
    ("36氪热榜", "/36kr/hot-list"),
    ("酷安热榜", "/coolapk/hot"),
)


class hotSource:
    """各平台热搜/热榜聚合（无浏览器）。"""

    def __init__(self, rsshub_base="http://127.0.0.1:1200", max_weibo=3, max_bilibili=2, max_rsshub=2):
        """见模块说明。"""
        self.rsshub_base = str(rsshub_base).rstrip("/")
        self.max_weibo = int(max_weibo)
        self.max_bilibili = int(max_bilibili)
        self.max_rsshub = int(max_rsshub)
        self.errors = []

    def fetch(self):
        """抓取全部热点，返回去重后的列表。"""
        items = []
        items.extend(self._safe(self.weibo_hot, "微博"))
        items.extend(self._safe(self.bilibili_hot, "B站"))
        for label, path in RSSHUB_HOT:
            items.extend(self._safe(lambda p=path: self.rsshub_hot(p, label), label))
        seen = set()
        out = []
        for item in items:
            key = item["title"].strip()
            if not key or key in seen:
                continue
            seen.add(key)
            out.append(item)
        return out

    def _safe(self, func, label):
        """单个来源失败不影响整体。"""
        try:
            return func()
        except Exception as error:  # noqa: BLE001
            self.errors.append(f"{label}: {type(error).__name__}: {error}")
            return []

    def weibo_hot(self):
        """微博实时热搜（weibo.com/ajax/side/hotSearch）。"""
        data = self._get_json("https://weibo.com/ajax/side/hotSearch", referer="https://weibo.com/")
        realtime = ((data or {}).get("data") or {}).get("realtime") or []
        out = []
        for row in realtime[: self.max_weibo]:
            word = str(row.get("word") or "").strip()
            if not word:
                continue
            out.append({
                "source": "微博",
                "title": word,
                "url": "https://s.weibo.com/weibo?q=" + urllib.parse.quote(f"#{word}#"),
                "heat": row.get("num") or "",
            })
        return out

    def bilibili_hot(self):
        """B 站热搜（search/square，无需 wbi）。"""
        data = self._get_json(
            "https://api.bilibili.com/x/web-interface/search/square?limit=10&platform=web",
            referer="https://www.bilibili.com/",
        )
        trending = ((data or {}).get("data") or {}).get("trending") or {}
        out = []
        for row in (trending.get("list") or [])[: self.max_bilibili]:
            word = str(row.get("keyword") or "").strip()
            if not word:
                continue
            link = row.get("link") or row.get("goto") or ""
            if not str(link).startswith("http"):
                link = "https://search.bilibili.com/all?keyword=" + urllib.parse.quote(word)
            out.append({"source": "B站", "title": word, "url": link, "heat": ""})
        return out

    def rsshub_hot(self, path, label):
        """RSSHub 热榜：取前 N 条标题。"""
        import xml.etree.ElementTree as ET

        url = self.rsshub_base + path
        request = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(request, timeout=30) as response:
            root = ET.fromstring(response.read())
        out = []
        for item in root.iter("item"):
            title = (item.findtext("title") or "").strip()
            link = (item.findtext("link") or "").strip()
            if not title or not link:
                continue
            out.append({"source": label, "title": title, "url": link, "heat": ""})
            if len(out) >= self.max_rsshub:
                break
        return out

    @staticmethod
    def _get_json(url, referer=None):
        """GET JSON。"""
        headers = {"User-Agent": UA, "Accept": "application/json"}
        if referer:
            headers["Referer"] = referer
        request = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(request, timeout=25) as response:
            return json.loads(response.read().decode("utf-8", "replace"))
