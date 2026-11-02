"""网页收集流水线。参数root为服务根目录；用隔离浏览器渲染公开页面，再由Gemma接口提炼正文。"""

from pathlib import Path
from microServices.CrawlerAdapter import CrawlerAdapter


class WebCollection:
    """浏览器所有HTTP请求经固定IP校验抓取，禁止私网、跳转、WebSocket和站点后台Worker。"""

    def __init__(self, root):
        """root为服务代码根目录；模型地址使用独立服务配置。"""
        self.root = Path(root)

    def collect(self, url, summarize=True):
        """url为公开页面，summarize决定是否调用模型；不保存或使用个人浏览器登录态。"""
        title, text, _ = self.render_page(url)
        if not text.strip():
            raise RuntimeError("页面没有可提取正文")
        if summarize:
            from services.ServiceRegistry import ServiceRegistry
            from services.ServiceClient import ServiceClient
            reply = ServiceClient(ServiceRegistry().get_service("llm")).chat({"messages": [
                {"role": "system", "content": (self.root / "prompts" / "WebCollectionSummary.md").read_text(encoding="utf-8")},
                {"role": "user", "content": "页面地址：" + url + "\n标题：" + title + "\n正文资料：\n" + text}],
                "max_tokens": 1500})
            text = str(reply["choices"][0]["message"]["content"] or "").strip()
            if not text:
                raise RuntimeError("模型未返回网页整理结果")
        return "来源：" + url + "\n标题：" + title + "\n" + text

    def render_page(self, url):
        """url为公开地址；返回标题、正文及最多200个链接，供网页收集和新闻源共同使用。"""
        from playwright.sync_api import sync_playwright
        fetcher = CrawlerAdapter(self.root)
        fetcher.validate_url(url)
        count, size = [0], [0]
        with sync_playwright() as driver:
            browser = driver.chromium.launch(headless=True, args=[
                "--disable-quic", "--force-webrtc-ip-handling-policy=disable_non_proxied_udp"])
            try:
                page = browser.new_page(service_workers="block", accept_downloads=False)
                page.route_web_socket("**/*", lambda ws: ws.close())

                def route_request(route):
                    """route为页面网络请求；所有外部数据只经受限抓取器，最多80个资源。"""
                    count[0] += 1
                    req = route.request
                    if count[0] > 80 or req.method != "GET" or req.resource_type not in (
                            "document", "script", "stylesheet", "xhr", "fetch"):
                        return route.abort()
                    try:
                        text, mime = fetcher.fetch_http(req.url, with_headers=True)
                        size[0] += len(text.encode("utf-8"))
                        if size[0] > 8 * 1024 * 1024:
                            return route.abort()
                        route.fulfill(status=200, content_type=mime + "; charset=utf-8", body=text)
                    except Exception:
                        route.abort()

                page.route("**/*", route_request)
                page.goto(url, timeout=60000, wait_until="domcontentloaded")
                page.wait_for_timeout(1500)
                title = page.title()
                text = page.locator("body").inner_text(timeout=10000)[:24000]
                links = page.locator("a[href]").evaluate_all(
                    "rows=>rows.slice(0,200).map(a=>({title:a.innerText.trim(),url:a.href,time:''}))")
            finally:
                browser.close()
        return title, text, links
