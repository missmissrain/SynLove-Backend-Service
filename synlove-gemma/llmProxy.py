# -*- coding: utf-8 -*-
"""SynLove LLM 修正代理：把客户端 Chat Completions 请求里的多模态内容重排后转发给 llama-server。

背景：Gemma 3 的视觉注意力依赖 <start_of_image> 位于用户轮次前部。
客户端统一以 [text, image_url] 顺序发送，图像 token 落在文本之后时
模型会声称"没有图片"。本代理把每个消息里的 image_url 部分移到最前，
使模型稳定看到图像；同时把 llama-server 收进回环地址，公网入口只有本代理。

职责边界：
- 只做 JSON body 重排与字节流转发，不缓存、不改写语义；
- 鉴权沿用 llama-server 的 --api-key，Authorization 头原样透传；
- 支持流式响应（chunked 逐块透传）；
- 请求体上限 32MB，超限返回 413；上游不可达返回 502。

用法：python llmProxy.py --listen-port 8080 --upstream 127.0.0.1:8180
"""
import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MAX_BODY = 32 * 1024 * 1024  # 请求体上限：32MB，足够容纳多张 base64 图片


def reorder_content(messages):
    """把每条消息 content 数组中的 image_url 部分移到最前（保持各类内部相对顺序）。

    纯文本消息、字符串 content 与非列表结构原样返回。
    """
    if not isinstance(messages, list):
        return messages
    for msg in messages:
        content = msg.get("content") if isinstance(msg, dict) else None
        if not isinstance(content, list):
            continue
        images = [p for p in content if isinstance(p, dict) and p.get("type") == "image_url"]
        if images:
            others = [p for p in content if not (isinstance(p, dict) and p.get("type") == "image_url")]
            msg["content"] = images + others
    return messages


class ProxyHandler(BaseHTTPRequestHandler):
    """逐请求转发处理器：/v1/chat/completions 重排，其余路径原样透传。"""

    protocol_version = "HTTP/1.1"
    upstream = "http://127.0.0.1:8180"  # 由 main() 覆写

    def log_message(self, fmt, *args):  # 安静模式：避免日志记录请求内容
        pass

    def _forward(self, method):
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            self._reply_json(413, {"error": {"message": "request body too large"}})
            return
        body = self.rfile.read(length) if length else None

        if method == "POST" and self.path.startswith("/v1/chat/completions") and body:
            try:
                payload = json.loads(body)
                payload["messages"] = reorder_content(payload.get("messages"))
                body = json.dumps(payload, ensure_ascii=False).encode()
            except Exception:
                pass  # 非 JSON 请求原样转发，让上游报错

        req = urllib.request.Request(self.upstream + self.path, data=body, method=method)
        for header in ("Content-Type", "Authorization", "Accept"):
            if self.headers.get(header):
                req.add_header(header, self.headers[header])
        try:
            upstream_resp = urllib.request.urlopen(req, timeout=1800)
        except urllib.error.HTTPError as err:
            self._reply_json(err.code, {"error": {"message": f"upstream {err.code}",
                                                  "type": "proxy_error"}})
            return
        except Exception:
            self._reply_json(502, {"error": {"message": "upstream unreachable",
                                             "type": "proxy_error"}})
            return

        with upstream_resp:
            self.send_response(upstream_resp.status)
            for key, value in upstream_resp.headers.items():
                if key.lower() in ("transfer-encoding", "connection", "content-length"):
                    continue
                self.send_header(key, value)
            is_stream = "chunked" in upstream_resp.headers.get("Transfer-Encoding", "").lower()
            if is_stream:
                self.send_header("Transfer-Encoding", "chunked")
            else:
                data = upstream_resp.read()
                self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            if is_stream:
                while True:
                    chunk = upstream_resp.read(4096)
                    if not chunk:
                        break
                    self.wfile.write(f"{len(chunk):x}\r\n".encode() + chunk + b"\r\n")
                    self.wfile.flush()
                self.wfile.write(b"0\r\n\r\n")
            else:
                self.wfile.write(data)

    def _reply_json(self, code, payload):
        data = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        self._forward("GET")

    def do_POST(self):
        self._forward("POST")


def main(argv=None):
    """入口：解析参数并启动常驻代理线程池服务。"""
    parser = argparse.ArgumentParser(description="SynLove LLM 修正代理")
    parser.add_argument("--listen-port", type=int, default=8080, help="对外监听端口")
    parser.add_argument("--listen-host", default="0.0.0.0", help="对外监听地址")
    parser.add_argument("--upstream", default="127.0.0.1:8180", help="llama-server 回环地址")
    args = parser.parse_args(argv)

    ProxyHandler.upstream = f"http://{args.upstream}"
    server = ThreadingHTTPServer((args.listen_host, args.listen_port), ProxyHandler)
    server.daemon_threads = True
    print(f"[{time.strftime('%F %T')}] llmProxy 就绪："
          f"{args.listen_host}:{args.listen_port} -> {args.upstream}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        sys.exit(0)


if __name__ == "__main__":
    main()
