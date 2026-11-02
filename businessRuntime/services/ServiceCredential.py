"""公共服务鉴权解析：环境变量优先，显式 apiKeyRef=backendService 时读取本机密钥。

注册参数 config 为服务配置，env 默认进程环境；不在服务地址或公开配置保存密钥正文。
缺省密钥路径为生产根目录 config/secrets.json；请求时读取，修改后客户端无需重建。
"""

import json
import os
from pathlib import Path


class ServiceCredential:
    """为 HTTP 业务客户端和启动环境提供同一凭据来源。"""

    @staticmethod
    def resolve_key(config, env=None):
        """config 为服务配置、env 为可选环境；返回内存中的密钥，不输出到日志。"""
        source = os.environ if env is None else env
        key = source.get(config.get("apiKeyEnv", ""), "")
        if key or config.get("apiKeyRef") != "backendService":
            return key
        file = Path(__file__).resolve().parent.parent / "config" / "secrets.json"
        if not file.exists():
            return ""
        data = json.loads(file.read_text(encoding="utf-8-sig"))
        return data.get("backendService", {}).get("apiKey", "")
