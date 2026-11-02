"""业务HTTP协议处理器。ServiceHttpHandler由有界服务器注册，业务实例通过server.service注入。"""

import hmac
import json
import re
import threading
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlsplit


class ServiceHttpHandler(BaseHTTPRequestHandler):
    """仅解析有界HTTP请求；业务能力由外部ServiceHost持有。"""

    def log_message(self, *args):
        """args为默认访问日志参数；禁用正文和带凭据URL日志。"""
        return

    def send_json(self, code, value):
        """code为状态码；value为可序列化响应。"""
        data = json.dumps(value, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        """无参数；处理状态、结果和文件查询。"""
        self.dispatch()

    def do_POST(self):
        """无参数；处理文件上传、任务提交和核心配置请求。"""
        self.dispatch()

    def dispatch(self):
        """无参数；鉴权后路由，拒绝转移编码、超限输入和未知路径。"""
        service = self.server.service
        self.connection.settimeout(30)
        try:
            origin = self.headers.get("Origin")
            host_header = self.headers.get("Host", "")
            if origin and origin not in ("http://" + host_header, "https://" + host_header):
                return self.send_json(403, {"error": "cross-origin requests denied"})
            if service.key and not hmac.compare_digest(
                    self.headers.get("Authorization", ""), "Bearer " + service.key):
                return self.send_json(401, {"error": "unauthorized"})
            if not service.key:
                hostname = urlsplit("http://" + self.headers.get("Host", "")).hostname
                if hostname not in ("127.0.0.1", "localhost", "::1"):
                    return self.send_json(403, {"error": "invalid host"})
            if self.headers.get("Transfer-Encoding"):
                return self.send_json(400, {"error": "chunked requests unsupported"})
            route = urlsplit(self.path)
            query = parse_qs(route.query)
            if service.name in ("reminders", "digest") and route.path.startswith("/v1/notifications"):
                from microServices.NotificationStore import NotificationStore
                store = NotificationStore(service.root / "runtime" / "notifications.sqlite")
                if self.command == "GET" and route.path == "/v1/notifications":
                    return self.send_json(200, {"items": store.pending()})
                if self.command == "POST" and re.fullmatch(r"/v1/notifications/[a-f0-9]{64}/ack", route.path):
                    store.acknowledge(route.path.split("/")[-2])
                    return self.send_json(200, {"ok": True})
                return self.send_json(404, {"error": "unknown notification route"})
            if self.command == "GET" and route.path == "/v1/core-status" and service.name == "core":
                import socket
                status = {}
                for label, port in (("harness", int(service.cfg.get("harnessPort", 3080))),
                                    ("inject", int(service.cfg.get("injectPort", 3081)))):
                    with socket.socket() as probe:
                        probe.settimeout(1)
                        status[label] = probe.connect_ex(("127.0.0.1", port)) == 0
                return self.send_json(200, status)
            if self.command == "GET" and route.path in ("/health", "/v1/health"):
                receipt = service.root / "runtime" / "migrationReceipt.json"
                migration = json.loads(receipt.read_text(encoding="utf-8")) if receipt.is_file() else {}
                return self.send_json(200, {"ok": service.readiness.check_ready(), "service": service.name,
                                           "protocol": 1, "busy": service.slot.locked(),
                                           "migrationId": migration.get("migrationId")})
            if self.command == "GET" and route.path == "/v1/tools":
                return self.send_json(200, service.tool_catalog())
            if self.command == "GET" and route.path == "/v1/snapshot":
                return self.send_json(200, service.business_snapshot())
            if self.command == "GET" and route.path.startswith("/v1/operations/"):
                result = service.store.get(route.path.rsplit("/", 1)[1])
                return self.send_json(200 if result else 404, result or {"error": "not found"})
            if self.command == "GET" and route.path.startswith("/v1/files/"):
                path = service.files.resolve(route.path.rsplit("/", 1)[1])
                if not path.is_file():
                    return self.send_json(404, {"error": "not found"})
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(path.stat().st_size))
                self.end_headers()
                with path.open("rb") as source:
                    while True:
                        chunk = source.read(1024 * 1024)
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                return
            size = int(self.headers.get("Content-Length", "0"))
            if self.command == "POST" and route.path == "/v1/shutdown":
                self.send_json(202, {"ok": True})
                threading.Thread(target=service.server.shutdown, daemon=True).start()
                return
            if self.command == "POST" and route.path == "/v1/files":
                key = service.files.receive(self.rfile, size, query.get("suffix", [""])[0])
                return self.send_json(201, {"id": key})
            if not 0 <= size <= 1024 * 1024:
                return self.send_json(413, {"error": "request too large"})
            data = self.rfile.read(size)
            if len(data) != size:
                raise ValueError("请求提前断开")
            if route.path == "/v1/core":
                output = service.core_request(self.command, query.get("path", [""])[0],
                                              data if self.command == "POST" else None)
                return self.send_json(200, json.loads(output))
            if self.command == "POST" and route.path == "/v1/rpc":
                if "application/json" not in self.headers.get("Content-Type", ""):
                    return self.send_json(415, {"error": "JSON required"})
                return self.send_json(202, service.submit(json.loads(data)))
            self.send_json(404, {"error": "unknown route"})
        except BlockingIOError:
            self.send_json(429, {"error": "busy"})
        except (ValueError, TypeError, KeyError):
            self.send_json(400, {"error": "invalid request"})
        except (ConnectionError, TimeoutError):
            self.close_connection = True
        except Exception:
            self.send_json(502, {"error": "upstream unavailable"})
