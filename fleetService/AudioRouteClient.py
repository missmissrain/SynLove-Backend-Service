"""功能：调用主项目持有的统一音频路由，不在终端库另存主人偏好。

注册参数token为可选本机网关鉴权；默认只连接127.0.0.1:8812、超时35秒。
不接收自定义URL，不允许模型将语音转到任意公网地址。
"""
import json
import urllib.request


class AudioRouteClient:
    """隔离主项目与终端传输协议，仅交换结构化路由结果。"""

    def __init__(self, token=""):
        """token从服务私有配置读取，禁止记录；无代理访问本机语音网关。"""
        self.token = token

    def request_route(self, body):
        """body为status/set/clear/resolve/camera操作；失败抛错，不回落到其他扬声器。"""
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = "Bearer " + self.token
        request = urllib.request.Request("http://127.0.0.1:8812/v1/audio/route",
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"), headers=headers)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(request, timeout=300 if body.get("action") == "speak" else 65) as response:
            value = json.loads(response.read(65536))
        if not isinstance(value, dict) or value.get("ok") is not True:
            raise RuntimeError("统一音频路由不可用")
        return value
