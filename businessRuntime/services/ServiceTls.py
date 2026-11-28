"""服务 TLS 信任配置：为显式配置的私有 CA 建立验证上下文。

注册参数 config 为服务配置，tlsCaFile 默认空，使用系统信任；相对路径以生产根目录解析。
保持证书链和主机名检查，不接受跳过校验开关。
"""

import ssl
from pathlib import Path


class ServiceTls:
    """为 Python 客户端和子进程统一解析服务 CA。"""

    @staticmethod
    def resolve_ca(config, root=None):
        """config 为服务配置，root 为可选项目根；返回验证存在的 CA 路径或空字符串。"""
        value = config.get("tlsCaFile")
        if not value:
            return ""
        base = Path(root) if root else Path(__file__).resolve().parent.parent
        path = Path(value)
        path = path if path.is_absolute() else base / path
        if not path.is_file():
            raise ValueError("服务 TLS CA 文件不存在")
        return str(path.resolve())

    @staticmethod
    def create_context(config):
        """config 为服务配置；保留系统根证书，再加入指定的私有 CA。"""
        ctx = ssl.create_default_context()
        ca = ServiceTls.resolve_ca(config)
        if ca:
            ctx.load_verify_locations(cafile=ca)
        return ctx
