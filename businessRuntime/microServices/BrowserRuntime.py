"""浏览器启动适配：优先使用部署指定或Playwright浏览器，缓存缺失时使用已安装的Edge。

默认不下载、不创建个人配置；SYNLOVE_BROWSER_EXECUTABLE可指定已有程序，启动期限10秒。
"""
import os
from pathlib import Path


class BrowserRuntime:
    """统一网页采集与依赖探活的无头浏览器启动，兼容Windows服务账号。"""

    @staticmethod
    def launch_browser(driver, **options):
        """driver为Playwright驱动，options为宿主固定启动参数；仅在缓存缺失时回退已有Edge。"""
        args = {"headless": True, "timeout": 10000, **options}
        explicit = os.environ.get("SYNLOVE_BROWSER_EXECUTABLE", "")
        if explicit:
            return driver.chromium.launch(executable_path=explicit, **args)
        try:
            return driver.chromium.launch(**args)
        except Exception as error:
            # 缺少浏览器缓存可以兼容，崩溃、权限、超时等错误必须保留。
            if "Executable doesn't exist" not in str(error):
                raise
            roots = [os.environ.get("ProgramFiles(x86)", "C:/Program Files (x86)"),
                     os.environ.get("ProgramFiles", "C:/Program Files")]
            edge = next((Path(root) / "Microsoft/Edge/Application/msedge.exe"
                         for root in roots
                         if (Path(root) / "Microsoft/Edge/Application/msedge.exe").is_file()), None)
            if edge is None:
                raise
            return driver.chromium.launch(executable_path=str(edge), **args)
