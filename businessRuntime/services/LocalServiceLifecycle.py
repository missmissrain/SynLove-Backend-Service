"""后台语音依赖边界。参数name/config指定独立模型API；业务宿主不再创建TTS或ASR模型进程。"""


class LocalServiceLifecycle:
    """保留旧流水线调用接口，所有模型生命周期交给配套模型服务。"""

    def __init__(self, name, config):
        """name为模型能力名，config为独立模型服务配置。"""
        self.name, self.config = name, config

    def ensure_service(self, idle_seconds=300, max_chars=120, model=None):
        """兼容旧参数；仅external可继续，配置错误明确拒绝，不在业务进程内拉模型。"""
        if self.config.get("management") != "external":
            raise RuntimeError("后台模型依赖必须配置external，由独立模型服务维护")
