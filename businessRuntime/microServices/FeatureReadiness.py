"""后台功能依赖探活。参数root/name/registry为部署根、功能及服务表；只探协议，不跑推理或发送消息。"""

import importlib.util
import json
import shutil
import time
import threading
from urllib.parse import urlsplit, urlunsplit
from pathlib import Path
from microServices.ServiceTransport import ServiceTransport


class FeatureReadiness:
    """API存活与功能可用分开检查；结果缓存15秒，避免每次心跳重复启动浏览器。"""

    def __init__(self, root, name, registry):
        """root为Backend运行根，name为固定服务，registry提供本机模型和RSSHub地址。"""
        self.root, self.name, self.registry = Path(root), name, registry
        self.at, self.result = 0, None
        self.lock = threading.Lock()

    def check_endpoint(self, name, path, identity=None):
        """name/path为固定依赖端点；identity非空时要求统一服务身份。"""
        cfg = {**self.registry.get_service(name), "timeoutSeconds": 3}
        if path == "/health":
            url = urlsplit(cfg["baseURL"])
            cfg["baseURL"] = urlunsplit((url.scheme, url.netloc, "", "", ""))
        if name == "rsshub":
            cfg["baseURL"] = cfg["baseURL"].rstrip("/")
        response = json.loads(ServiceTransport(cfg).request("GET", path))
        if identity:
            if response.get("ok") is not True or response.get("service") != identity:
                raise RuntimeError("依赖健康协议不匹配")
        elif name == "llm" and not response.get("data"):
            raise RuntimeError("模型未注册")

    def check_browser(self):
        """无参数；实际建立无页面网络的浏览器会话，并执行小型协议请求。"""
        from playwright.sync_api import sync_playwright
        with sync_playwright() as driver:
            browser = driver.chromium.launch(headless=True)
            try:
                if browser.new_page().evaluate("1+1") != 2:
                    raise RuntimeError("浏览器协议失败")
            finally:
                browser.close()

    def check_ready(self):
        """无参数；返回就绪布尔值；错误不包含配置地址、密钥或私人路径。"""
        with self.lock:
            return self.check_dependencies()

    def check_dependencies(self):
        """无参数；在探活互斥锁内刷新依赖状态，繁忙不是不可用。"""
        if self.result is not None and time.monotonic() - self.at < 15:
            return self.result
        try:
            if self.name in ("digest", "crawler", "video"):
                self.check_endpoint("llm", "/models")
            if self.name == "digest":
                cfg = self.registry.get_service("rsshub")
                ServiceTransport({**cfg, "timeoutSeconds": 3}).request("GET", "/healthz", limit=65536)
                config = self.root / "newsDigest" / "config" / "newsDigest.json"
                if not config.is_file():
                    raise RuntimeError("爬虫中心未配置")
            if self.name == "crawler":
                self.check_browser()
            if self.name == "video":
                if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
                    raise RuntimeError("视频工具缺失")
                for module in ("PIL", "yt_dlp"):
                    if importlib.util.find_spec(module) is None:
                        raise RuntimeError("视频依赖缺失")
                self.check_endpoint("asr", "/health", "asr")
            self.result = True
        except Exception as error:
            import sys
            print(f"{self.name} readiness fail: {type(error).__name__}: {error}",
                  file=sys.stderr, flush=True)
            self.result = False
        self.at = time.monotonic()
        return self.result
