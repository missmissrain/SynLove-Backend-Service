"""视频下载步骤。参数config/out_dir/login_config指定上限和服务器Cookie；不创建额外下载守护进程。"""

import os
from pathlib import Path
from urllib.parse import urlsplit
from steps.LoginRequired import LoginRequired

loginRequired = LoginRequired


class videoDownloader:
    """下载在受超时和进程树管理的业务Worker内执行，不依赖个人主机或8088端口。"""

    def __init__(self, config, out_dir, login_config=None):
        """config含maxSeconds，out_dir为服务专属目录；Cookie只从服务器固定目录读取。"""
        self.config, self.out = config, Path(out_dir)
        self.root = Path(__file__).resolve().parents[2]
        self.login = login_config or {}

    def fetch(self, target):
        """target为已上传的本机文件或公开视频URL；返回实际存在的视频文件。"""
        path = Path(target)
        if path.is_file():
            return str(path.resolve())
        from microServices.CrawlerAdapter import CrawlerAdapter
        CrawlerAdapter(self.root).validate_url(target)
        import yt_dlp
        self.out.mkdir(parents=True, exist_ok=True)
        cookie = None
        host = urlsplit(target).hostname or ""
        for domain in self.login.get("sites", []):
            if host == domain or host.endswith("." + domain):
                candidate = self.root / "runtime" / "cookies" / (domain + ".txt")
                if candidate.is_file() and candidate.stat().st_size:
                    cookie = str(candidate)
                # 部分公开视频无需Cookie，缺失时允许yt-dlp先尝试；需要鉴权时给出明确提示。
                break
        maximum = int(self.config.get("maxSeconds", 600))

        def filter_duration(info, *, incomplete=False):
            """info为下载元数据；超过配置时长即拒绝，限制长视频资源消耗。"""
            duration = info.get("duration")
            return "视频超过时长上限" if duration and duration > maximum else None

        options = {"outtmpl": str(self.out / "%(id)s.%(ext)s"), "noplaylist": True,
                   "max_filesize": 256 * 1024 * 1024, "socket_timeout": 30,
                   "retries": 1, "cookiefile": cookie, "match_filter": filter_duration,
                   "quiet": True, "no_warnings": True}
        try:
            with yt_dlp.YoutubeDL(options) as loader:
                info = loader.extract_info(target, download=True)
                if not info:
                    raise RuntimeError("下载被限制或未获得视频")
                filename = loader.prepare_filename(info)
        except yt_dlp.utils.DownloadError as error:
            if any(word in str(error).lower() for word in ("login", "sign in", "cookies", "登录")):
                raise LoginRequired("站点要求服务器登录凭据，请配置服务器Cookie后重试") from None
            raise RuntimeError("视频下载失败，检查服务端网络、站点限制和下载器日志") from None
        if not Path(filename).is_file():
            raise RuntimeError("下载未生成可验证文件")
        return filename
