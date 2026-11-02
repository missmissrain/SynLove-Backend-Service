"""配置加载器：读 newsDigest/config/newsDigest.json，补默认值并解析成绝对路径。

职责：
- 把 JSON 配置读成 dict，缺字段用默认值补齐（避免改配置时漏项就崩）；
- 浏览器使用服务器隔离环境，不读取个人主机路径和登录态；
- 校验关注画像；结果写持久通知，不需要QQ账号或平台密钥。

注册参数（全部可选，默认值见 DEFAULT）：
    interest：关注画像文字，默认"AI/科技、消费、民生、健康"
    maxItems：一条消息最多写几条新闻，默认 3
    maxChars：消息字数上限，默认 120
"""

import json
import os
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except AttributeError:  # 极老的解释器没有 reconfigure
    pass

DEFAULT = {
    "enabled": True,
    "sources": [],
    "interest": "关心 AI 与科技新品、消费与价格、健康和居家安全、本地民生政策；不关心体育赛事、娱乐八卦、明星绯闻。",
    "maxItems": 3,
    "maxChars": 120,
    "gemma": {
        "endpoint": "http://127.0.0.1:8080/v1/chat/completions",
        "model": "gemma-4-E4B-it-Q4_K_M",
        "timeoutSeconds": 240,
    },
    "push": {"enabled": True},
    "browser": {"isolated": True},
}


class configLoader:
    """读取并规范化采集/筛选/推送配置。"""

    def __init__(self, project_root, config_path=None):
        """project_root：项目根目录（用来解析相对路径）；config_path：配置文件，默认 config/newsDigest.json。"""
        self.root = Path(project_root)
        self.path = Path(config_path or os.environ.get("SYNLOVE_DIGEST_CONFIG") or
                         self.root / "newsDigest" / "config" / "newsDigest.json")
        self.data = self._load()

    def _load(self):
        """读配置并与默认值做浅合并-深合并，返回规范化后的 dict。"""
        if not self.path.exists():
            raise FileNotFoundError(f"配置文件不存在：{self.path}")
        raw = json.loads(self.path.read_text(encoding="utf-8-sig"))
        sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
        from services.ServiceRegistry import ServiceRegistry
        service = ServiceRegistry().get_service("llm")
        rsshub = ServiceRegistry().get_service("rsshub")
        merged = json.loads(json.dumps(DEFAULT))
        for key, value in raw.items():
            if isinstance(value, dict) and isinstance(merged.get(key), dict):
                merged[key].update(value)
            else:
                merged[key] = value
        browser = merged["browser"]
        merged["rsshubBase"] = rsshub["baseURL"]
        # 统一服务注册表拥有地址和模型；新闻配置保留业务生成参数。
        merged["gemma"].update({
            "endpoint": service["baseURL"] + "/chat/completions",
            "model": service["model"],
            "timeoutSeconds": service["timeoutSeconds"],
            "apiKeyEnv": service.get("apiKeyEnv", ""),
        })
        if not merged["interest"].strip():
            raise ValueError("interest 不能为空：没有关注画像就没法筛选")
        return merged

    def get(self, key, default=None):
        """按 key 取配置项，不存在时返回默认值。"""
        return self.data.get(key, default)
