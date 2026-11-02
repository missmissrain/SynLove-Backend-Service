"""统一服务配置。ServiceRegistry 接收可选配置路径；默认读取 config/serviceConfig.json。

只负责地址、鉴权引用和生命周期归属，不启动进程、不加载模型。management 默认 external，
managed 仅允许回环地址；显式环境地址覆盖后默认 external，防止误启动或监控重启。
"""

import json
import os
from pathlib import Path
from urllib.parse import urlsplit
from services.ServiceCredential import ServiceCredential
from services.ServiceTls import ServiceTls


class ServiceRegistry:
    """统一管理推理与业务端点；密钥只从 apiKeyEnv 指定的环境变量读取。"""

    DEFAULTS = {
        "core": {"port": 8790, "timeoutSeconds": 240},
        "reminders": {"port": 8791, "timeoutSeconds": 60},
        "digest": {"port": 8792, "timeoutSeconds": 900},
        "video": {"port": 8793, "timeoutSeconds": 3600},
        "history": {"port": 8794, "timeoutSeconds": 120},
        "memory": {"port": 8797, "timeoutSeconds": 120},
        "state": {"port": 8798, "timeoutSeconds": 120},
        "qqstate": {"port": 8799, "timeoutSeconds": 120},
        "observation": {"port": 8800, "timeoutSeconds": 120},
        "session": {"port": 8801, "timeoutSeconds": 120},
        "coding": {"port": 8795, "timeoutSeconds": 1800},
        "crawler": {"port": 8796, "timeoutSeconds": 300},
        "rsshub": {"port": 1200, "timeoutSeconds": 30},
        "opencode": {"port": 4096, "timeoutSeconds": 60},
    }

    def __init__(self, path=None):
        """path 为独立服务配置文件；未传时允许 SYNLOVE_SERVICE_CONFIG 覆盖默认路径。"""
        self.path = Path(path or os.environ.get("SYNLOVE_SERVICE_CONFIG") or
                         Path(__file__).resolve().parent.parent / "config" / "serviceConfig.json")
        self.data = json.loads(self.path.read_text(encoding="utf-8-sig"))
        if self.data.get("version") != 1:
            raise ValueError("serviceConfig.version 必须是 1")

    def get_service(self, name):
        """name为已登记服务名；校验并返回副本，拒绝带内嵌凭据或非法协议的地址。"""
        defaults = self.DEFAULTS.get(name)
        cfg = ({"baseURL": f"http://127.0.0.1:{defaults['port']}/v1",
                "management": "managed", "timeoutSeconds": defaults["timeoutSeconds"],
                "apiKeyEnv": "SYNLOVE_" + name.upper() + "_API_KEY",
                "healthPath": "/health"} if defaults else {})
        if name in ("rsshub", "opencode"):
            cfg["baseURL"] = cfg["baseURL"].removesuffix("/v1")
        cfg.update(self.data["services"].get(name, {}))
        if not cfg:
            raise ValueError(f"未知服务 {name}")
        prefix = "SYNLOVE_" + name.upper()
        override = os.environ.get(prefix + "_BASE_URL")
        if override:
            cfg["baseURL"] = override
            cfg["management"] = "external"
        cfg["management"] = os.environ.get(prefix + "_MANAGEMENT", cfg.get("management", "external"))
        cfg["baseURL"] = str(cfg["baseURL"]).rstrip("/")
        url = urlsplit(cfg["baseURL"])
        if (url.scheme not in ("http", "https") or not url.hostname or
                url.username or url.password or url.query or url.fragment):
            raise ValueError(f"{name} 的 baseURL 必须是无内嵌凭据的 HTTP(S) API 根地址")
        cfg["port"] = url.port or (443 if url.scheme == "https" else 80)
        cfg["host"] = url.hostname
        if cfg["management"] not in ("managed", "external", "disabled"):
            raise ValueError(f"{name}.management 只允许 managed、external 或 disabled")
        if cfg["management"] == "managed" and url.hostname not in ("127.0.0.1", "localhost"):
            raise ValueError(f"{name} 托管服务必须使用回环地址；远程服务请设 external")
        expected_path = "" if name in ("rsshub", "opencode") else "/v1"
        if cfg["management"] == "managed" and (url.scheme != "http" or url.path.rstrip("/") != expected_path):
            raise ValueError(f"{name} 本机托管服务使用 http://回环地址:端口/v1；代理地址请设 external")
        cfg["timeoutSeconds"] = float(cfg.get("timeoutSeconds", 240))
        if not 0 < cfg["timeoutSeconds"] <= 3600:
            raise ValueError(f"{name}.timeoutSeconds 必须在 (0, 3600] 内")
        if not 1 <= float(cfg.get("cacheRetentionDays", 7)) <= 365:
            raise ValueError(f"{name}.cacheRetentionDays必须在1至365天之间")
        if name in ("tts", "asr", "llm") and (not isinstance(cfg.get("model"), str) or not cfg["model"].strip()):
            raise ValueError(f"{name}.model 不能为空")
        return cfg

    def apply_environment(self, env):
        """env 为子进程环境；同步模型客户端和服务归属，外部服务禁用本机显存门控。"""
        cfg = self.get_service("llm")
        env["SYNLOVE_SERVICE_CONFIG"] = str(self.path.resolve())
        env["GEMMA_LOCAL_BASE_URL"] = cfg["baseURL"]
        env["DSH_GEMMA_MODEL"] = str(cfg["model"])
        env["SYNLOVE_LLM_MANAGEMENT"] = cfg["management"]
        env["GEMMA_SERVER_PORT"] = str(cfg["port"])
        env["SYNLOVE_LLM_API_KEY_ENV"] = cfg.get("apiKeyEnv", "")
        env["SYNLOVE_LLM_TIMEOUT_MS"] = str(int(cfg["timeoutSeconds"] * 1000))
        if cfg["management"] == "external":
            env["DSH_GEMMA_MIN_FREE_VRAM_MB"] = "0"
        ca = ServiceTls.resolve_ca(cfg, self.path.resolve().parent.parent)
        if ca:
            env["NODE_EXTRA_CA_CERTS"] = ca
        for name in dict.fromkeys([*self.DEFAULTS, "tts", "asr", "llm"]):
            service = self.get_service(name)
            key = ServiceCredential.resolve_key(service, env)
            if key and service.get("apiKeyEnv"):
                env[service["apiKeyEnv"]] = key
            prefix = "SYNLOVE_" + name.upper()
            env[prefix + "_BASE_URL"] = service["baseURL"]
            env[prefix + "_MANAGEMENT"] = service["management"]
            if name == "session":
                env["SYNLOVE_SESSION_TIMEOUT_MS"] = str(int(service["timeoutSeconds"] * 1000))
            if name == "qqstate":
                mode = str(service.get("mode", "local")).strip().lower()
                if mode not in ("local", "remote"):
                    raise ValueError("qqstate.mode 只允许 local 或 remote")
                env["SYNLOVE_QQ_STATE_MODE"] = mode
            if name == "memory":
                mode = str(service.get("mode", "local")).strip().lower()
                if mode not in ("local", "remote"):
                    raise ValueError("memory.mode 只允许 local 或 remote")
                env["SYNLOVE_MEMORY_TIMELINE_MODE"] = mode
            if name == "state":
                mode = str(service.get("mode", "local")).strip().lower()
                if mode not in ("local", "remote"):
                    raise ValueError("state.mode 只允许 local 或 remote")
                env["SYNLOVE_STATE_MODE"] = mode
            if name == "observation":
                mode = str(service.get("mode", "local")).strip().lower()
                if mode not in ("local", "remote"):
                    raise ValueError("observation.mode 只允许 local 或 remote")
                env["SYNLOVE_OBSERVATION_MODE"] = mode
            if name == "session":
                mode = str(service.get("mode", "local")).strip().lower()
                if mode not in ("local", "remote"):
                    raise ValueError("session.mode 只允许 local 或 remote")
                env["SYNLOVE_SESSION_MODE"] = mode
        env["OPENCODE_SERVER_URL"] = self.get_service("opencode")["baseURL"]

    def apply_model_config(self, config, env=None):
        """config 为已合并的模型配置；仅覆盖 llm.provider 指定的本地能力提供方。"""
        cfg = self.get_service("llm")
        provider = config.get("providers", {}).get(cfg.get("provider", "gemma-local"))
        if provider is not None:
            provider["baseURL"] = cfg["baseURL"]
            key_env = cfg.get("apiKeyEnv") or "SYNLOVE_LLM_API_KEY"
            # Harness/兼容 SDK 要求非空凭据；无鉴权服务使用明确占位值，不复用旧供应商密钥。
            key = ServiceCredential.resolve_key(cfg, env) or "synlove-no-auth"
            provider["apiKeyEnv"] = key_env
            provider["apiKey"] = key
            if cfg.get("api"):
                provider["api"] = cfg["api"]
            models = provider.get("models", [])
            if models:
                provider["models"] = [{**models[0], "id": cfg["model"]}]
            default = config.get("defaultModel", {})
            if default.get("provider") == cfg.get("provider", "gemma-local"):
                default["model"] = cfg["model"]
