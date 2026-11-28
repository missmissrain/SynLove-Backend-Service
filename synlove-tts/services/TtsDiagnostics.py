# 功能：保存TTS请求事件、普通心跳和致命异常；禁止周期异步遍历解释器线程栈。
# 注册参数：folder为诊断目录；每个PID独立文件，每120秒记录普通JSON心跳，不保存正文或密钥。
import faulthandler
import hashlib
import json
import os
import threading
import time
from pathlib import Path


class TtsDiagnostics:
    """用持有GIL的普通线程记录存活信息，不改变声音提示及采样。"""

    def __init__(self, folder):
        """folder为诊断根目录；启动时创建当前进程的事件与致命异常文件。"""
        folder = Path(folder)
        folder.mkdir(parents=True, exist_ok=True)
        tag = time.strftime("%Y%m%d-%H%M%S") + "-" + str(os.getpid())
        self.path = folder / ("tts-" + tag + ".jsonl")
        self.lock = threading.Lock()
        self.stopped = threading.Event()
        self.started = time.monotonic()
        self.fault = (folder / ("tts-" + tag + "-fault.log")).open("a", encoding="utf-8")
        # 原生定时栈遍历曾在python312.dll!_Py_DumpTracebackThreads内访问异常。
        # 普通心跳不访问其他线程的内部栈；致命异常捕获仅在真实故障时触发。
        faulthandler.cancel_dump_traceback_later()
        faulthandler.enable(file=self.fault, all_threads=True)
        self.write_event("process_start", diagnosticVersion=2,
                         periodicStackDump=False, heartbeatSeconds=120)
        threading.Thread(target=self.write_heartbeat, daemon=True,
                         name="ttsDiagnosticHeartbeat").start()

    def write_heartbeat(self):
        """无参数；每120秒追加进程存活事件，不抓栈、不访问模型、不续期模型保温。"""
        while not self.stopped.wait(120):
            try:
                self.write_event("process_heartbeat",
                                 uptimeSeconds=round(time.monotonic() - self.started, 1))
            except OSError:
                # 诊断磁盘暂时不可写时不能杀死合成线程；下一周期再次尝试。
                continue

    def write_event(self, event, **fields):
        """event为阶段名，fields仅放任务编号、参数和统计，不含音频、正文和鉴权。"""
        row = {"time": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
               "pid": os.getpid(), "event": event, **fields}
        with self.lock, self.path.open("a", encoding="utf-8") as out:
            out.write(json.dumps(row, ensure_ascii=False) + "\n")
            out.flush()
            os.fsync(out.fileno())

    def record_request(self, task_id, payload):
        """task_id为上游任务编号，payload只提取长度、哈希、引导参数和seed。"""
        text = str(payload.get("input", payload.get("text", "")))
        self.write_event("synthesis_start", taskId=task_id, chars=len(text),
                         textSha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
                         seed=payload.get("seed"), cfgScale=payload.get("cfgScale"),
                         cfgScaleRef=payload.get("cfgScaleRef"),
                         cfgScaleIns=payload.get("cfgScaleIns"))
