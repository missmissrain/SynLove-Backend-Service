"""爬虫中心浏览器新闻源。参数root和browser_config；复用隔离网页采集，不依赖个人实例或本机Edge。"""

from microServices.WebCollection import WebCollection


class browserSource:
    """新闻列表与网页收集共用受限浏览器，提取标题和链接后交给爬虫中心筛选去重。"""

    def __init__(self, root, browser_config=None):
        """root为后台运行根，browser_config保留旧配置调用兼容。"""
        self.collector = WebCollection(root)

    def fetch(self, url, limit):
        """url为公开新闻列表，limit为最多条目数，拒绝空标题和非HTTP链接。"""
        _, _, links = self.collector.render_page(url)
        return [row for row in links if row["title"] and
                row["url"].startswith(("https://", "http://"))][:max(1, min(int(limit), 200))]
