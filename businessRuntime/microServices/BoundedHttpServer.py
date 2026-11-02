"""有界HTTP服务。BoundedHttpServer(address,handler) 默认最多32个请求线程，排队64条。"""

import threading
from http.server import ThreadingHTTPServer


class BoundedHttpServer(ThreadingHTTPServer):
    """用信号量限制慢连接占用；超过容量直接503，不创建额外线程。"""

    daemon_threads = True
    request_queue_size = 64

    def __init__(self, address, handler):
        """address为监听地址，handler为协议处理器；连接与请求线程均有容量限制。"""
        self.slots = threading.BoundedSemaphore(32)
        super().__init__(address, handler)

    def process_request(self, request, address):
        """request为已接受socket，address为客户端；无空槽时关闭连接并返回503。"""
        if not self.slots.acquire(False):
            try:
                request.settimeout(1)
                request.sendall(b"HTTP/1.1 503 Service Unavailable\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
            finally:
                self.shutdown_request(request)
            return
        try:
            super().process_request(request, address)
        except BaseException:
            self.slots.release()
            raise

    def finish_request(self, request, address):
        """request/address为工作线程连接；在读请求行之前设置超时，防止慢头部永久占槽。"""
        request.settimeout(30)
        try:
            super().finish_request(request, address)
        except (ConnectionError, TimeoutError):
            # 断网或主动断连是正常传输失败，不记录成大量业务崩溃堆栈。
            pass

    def process_request_thread(self, request, address):
        """request/address为已取得容量的连接；异常或正常退出都释放容量。"""
        try:
            super().process_request_thread(request, address)
        finally:
            self.slots.release()
