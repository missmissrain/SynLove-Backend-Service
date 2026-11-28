"""服务器本机管理HTTP：统一端口17864，Bearer认证，提供设备、配对、命令和状态接口。

注册参数server.manager、server.token；请求最大64KiB，不开放跨域，也不返回私钥。
"""
import hmac
import json
import os
from http.server import BaseHTTPRequestHandler
from fleetService.DeviceProfile import DeviceProfile


class TerminalAdminHandler(BaseHTTPRequestHandler):
    """仅接受本机管理客户端，远端设备通信统一经过VPS中转。"""

    def do_GET(self):
        """无参数；读取设备列表、服务健康或指定已提交命令结果。"""
        self.handle_request("GET")

    def do_POST(self):
        """无参数；创建一次性配对、更新设备或持久提交指令。"""
        self.handle_request("POST")

    def handle_request(self, method):
        """method为HTTP方法；认证后进行有限路由，错误只回传脱敏类型。"""
        if not hmac.compare_digest(self.headers.get("Authorization","").encode(),
                                   ("Bearer "+self.server.token).encode()):
            self.send_json(401,{"ok":False,"error":"未授权"})
            return
        try:
            body={}
            if method=="POST":
                size=int(self.headers.get("Content-Length","0"))
                if not 0<size<=65536:
                    raise ValueError("请求大小无效")
                body=json.loads(self.rfile.read(size))
                if not isinstance(body,dict):
                    raise ValueError("请求必须为对象")
            manager=self.server.manager
            if self.path=="/v1/devices" and method=="GET":
                result={"ok":True,"devices":manager.registry.list()}
            elif self.path=="/v1/deviceTypes" and method=="GET":
                result={"ok":True,**DeviceProfile.describe_types()}
            elif self.path=="/v1/health" and method=="GET":
                result={"ok":True,"protocol":"synlove-terminal-v1","pid":os.getpid(),
                        "relayConnected":self.server.node.connected,
                        "notificationsConfigured":bool(manager.config.get("notificationTarget")
                                                       and manager.config.get("notificationInbox")),
                        "relayError":getattr(self.server.node,"last_error","")}
            elif self.path=="/v1/pair" and method=="POST":
                result={"ok":True,"bundle":manager.pairing_bundle(body)}
            elif self.path=="/v1/device/reserve" and method=="POST":
                device=manager.store.reserve_device(body)
                manager.write_snapshot()
                result={"ok":True,"device":device}
            elif self.path.startswith("/v1/instance/") and method=="GET":
                result={"ok":True,**manager.store.instance_state(self.path.split("/")[-1])}
            elif self.path=="/v1/instance/release" and method=="POST":
                manager.store.release_instance(body["id"],body["bootId"])
                manager.write_snapshot()
                result={"ok":True}
            elif self.path=="/v1/device" and method=="POST":
                manager.store.update_device(body["id"],body)
                manager.write_snapshot()
                result={"ok":True}
            elif self.path=="/v1/commands" and method=="POST":
                result={"ok":True,"id":manager.queue_command(body["deviceId"],body["command"])}
            elif self.path=="/v1/initiative/text" and method=="POST":
                result=manager.queue_initiative_text(body)
            elif self.path=="/v1/audio/route" and method in ("GET", "POST"):
                result=manager.audio_routes.request_route(body if method=="POST" else {"action":"status"})
            elif self.path.startswith("/v1/commands/") and method=="GET":
                result={"ok":True,**manager.store.command_result(self.path.split("/")[-1])}
            else:
                self.send_json(404,{"ok":False,"error":"接口不存在"})
                return
            self.send_json(200,result)
        except Exception as exc:
            self.send_json(400,{"ok":False,"error":"终端请求失败："+type(exc).__name__})

    def send_json(self,status,data):
        """status/data为响应；JSON UTF8且禁止缓存。"""
        raw=json.dumps(data,ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type","application/json; charset=utf-8")
        self.send_header("Content-Length",str(len(raw)))
        self.send_header("Cache-Control","no-store")
        self.end_headers()
        self.wfile.write(raw)

    def setup(self):
        """无参数；设置请求读取期限，限制慢连接占用管理线程。"""
        super().setup()
        self.connection.settimeout(8)

    def log_message(self,fmt,*args):
        """fmt/args为默认HTTP日志；不记录含接入码或个人路径的请求。"""
        return
