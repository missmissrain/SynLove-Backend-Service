"""Gemma云端兜底：GPU资格或加载失败后，将原始聊天请求交给已配置云端模型。

注册参数root为后台仓库；gpuResidency.json的cloudFallback配置默认关闭，
provider/model指定模型，modelConfigPath/secretsPath仅引用私有文件，不复制密钥。
timeoutSeconds默认180秒；不支持的接口和多模态模型不匹配时明确失败。
"""
import json
import os
import urllib.request
from pathlib import Path


class CloudFallback:
    """复用现有模型与凭据配置，不将本地Bearer令牌转发到云端。"""

    def __init__(self, root):
        """root为后台根目录；只读取配置，不在初始化时调用云端。"""
        path = Path(root) / "config/gpuResidency.json"
        config = json.loads(path.read_text("utf-8-sig")) if path.exists() else {}
        self.config = config.get("cloudFallback", {})

    def open_response(self, body, route):
        """body为原始JSON字节、route为路径；返回云端HTTP响应，由调用者关闭。"""
        cfg = self.config
        if not cfg.get("enabled") or route != "/v1/chat/completions":
            raise RuntimeError("当前接口未配置云端兜底")
        base = Path(__file__).resolve().parents[2]

        def _resolve(value):
            """value 为配置路径；相对路径按后台仓库根解析。"""
            path = Path(value)
            return path if path.is_absolute() else (base / path)

        models = json.loads(_resolve(cfg["modelConfigPath"]).read_text("utf-8-sig"))
        secrets = json.loads(_resolve(cfg["secretsPath"]).read_text("utf-8-sig"))
        provider = cfg["provider"]
        model = cfg["model"]
        definition = models["providers"][provider]
        if definition.get("api") != "openai-completions":
            raise RuntimeError("兜底模型需支持Chat Completions")
        entry = next((m for m in definition["models"] if m["id"] == model), None)
        if entry is None:
            raise RuntimeError("兜底模型未注册")
        payload = json.loads(body)
        has_image = any(
            isinstance(part, dict) and part.get("type") == "image_url"
            for msg in payload.get("messages", [])
            for part in (msg.get("content", []) if isinstance(msg.get("content"), list) else []))
        if has_image and "image" not in entry.get("input", []):
            raise RuntimeError("兜底模型不支持图片，拒绝丢弃图像")
        key = os.environ.get(definition.get("apiKeyEnv", ""), "")
        if not key:
            key = secrets.get("providers", {}).get(provider, {}).get("apiKey", "")
        if not key:
            raise RuntimeError("云端兜底凭据未配置")
        base = os.environ.get(definition.get("baseURLEnv", ""), "") or definition["baseURL"]
        if not base.startswith("https://"):
            raise RuntimeError("云端兜底必须使用HTTPS")
        payload["model"] = model
        # 本地llama扩展不透传；正文、工具、图像及流式标志保留。
        for name in ("cache_prompt", "n_predict", "mirostat", "grammar"):
            payload.pop(name, None)
        payload.setdefault("enable_thinking", False)
        request = urllib.request.Request(
            base.rstrip("/") + "/chat/completions",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"})
        return urllib.request.urlopen(request, timeout=min(600, max(5, float(cfg.get("timeoutSeconds", 180)))))
