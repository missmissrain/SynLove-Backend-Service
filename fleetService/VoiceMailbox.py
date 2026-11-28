"""语音中继信箱：分块暂存已验签设备录音，后台串行调用主人格语音网关并回传音频。

注册参数root为私有运行目录；gateway默认回环8812，支持依赖注入测试。
默认录音20秒、单块12KiB、每设备最多两项未完成任务、十分钟后清理暂存。
"""
import base64
import hashlib
import json
import re
import shutil
import threading
import time
import urllib.request
import io
import wave
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor


class VoiceMailbox:
    """将长时推理与三秒心跳分开，不阻塞其他终端的控制链路。"""

    def __init__(self, root, gateway="http://127.0.0.1:8812", call=None, playback=None):
        """root为私有目录、gateway为回环网关；call为测试注入，playback为独立播放路由。"""
        if gateway != "http://127.0.0.1:8812":
            raise ValueError("语音网关必须是指定回环服务")
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.gateway = gateway
        self.lock = threading.RLock()
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="SynLoveVoice")
        self.call = call or self._call_gateway
        self.follow = {}
        self.playback = playback
        for path in self.root.glob("*/*/job.json"):
            item = json.loads(path.read_text(encoding="utf-8"))
            if item["state"] in ("queued", "processing"):
                item.update(state="failed", error="语音任务被重启中断，请重新唤醒")
                self._save_job(path.parent, item)

    def exchange_voice(self, device, body):
        """device来自验签身份；body含action、id及可选分块，拒绝越权读取和重复变更。"""
        ident = body.get("id", "")
        action = body.get("action")
        if not isinstance(ident, str) or not re.fullmatch("[a-f0-9]{32}", ident):
            raise ValueError("语音任务标识错误")
        if action == "prewarm":
            return self._prewarm(device)
        folder = self.root / hashlib.sha256(device.encode()).hexdigest()[:24] / ident
        with self.lock:
            self._cleanup()
            path = folder / "job.json"
            if action == "begin" and not path.exists():
                size = body.get("size")
                sha = body.get("sha256", "")
                if type(size) is not int or not 44 < size <= 640044 or not re.fullmatch("[a-f0-9]{64}", str(sha)):
                    raise ValueError("录音长度或摘要无效")
                jobs = [json.loads(p.read_text(encoding="utf-8"))
                        for p in folder.parent.glob("*/job.json")]
                if sum(x["state"] in ("uploading", "queued", "processing") for x in jobs) >= 2:
                    raise ValueError("此设备仍有语音任务未完成")
                if sum(1 for _ in self.root.glob("*/*/job.json")) >= 32:
                    raise ValueError("语音暂存已满")
                folder.mkdir(parents=True)
                self._save_job(folder, {"id": ident, "device": device, "size": size, "sha256": sha,
                                       "state": "uploading", "received": 0, "created": time.time()})
            if not path.exists():
                raise ValueError("语音任务不存在")
            job = json.loads(path.read_text(encoding="utf-8"))
            if job["device"] != device:
                raise ValueError("语音任务不属于此设备")
            if action == "begin":
                if job["size"] != body.get("size") or job["sha256"] != body.get("sha256"):
                    raise ValueError("同一语音标识不可变更内容")
            elif action == "write":
                index = body.get("index")
                data = body.get("data", "")
                if type(index) is not int or not 0 <= index < 64 or not isinstance(data, str) or len(data) > 16384:
                    raise ValueError("语音分块错误")
                raw = base64.b64decode(data, validate=True)
                part = folder / (str(index) + ".part")
                if part.exists():
                    if part.read_bytes() != raw:
                        raise ValueError("语音分块不可覆盖")
                else:
                    if job["state"] != "uploading" or index != len(list(folder.glob("*.part"))) or not raw:
                        raise ValueError("录音分块顺序错误")
                    if job["received"] + len(raw) > job["size"]:
                        raise ValueError("录音超过声明大小")
                    part.write_bytes(raw)
                    job["received"] += len(raw)
                    self._save_job(folder, job)
            elif action == "commit" and job["state"] == "uploading":
                raw = b"".join(p.read_bytes() for p in sorted(folder.glob("*.part"),
                                                          key=lambda p: int(p.stem)))
                if len(raw) != job["size"] or hashlib.sha256(raw).hexdigest() != job["sha256"]:
                    raise ValueError("录音完整性校验失败")
                with wave.open(io.BytesIO(raw), "rb") as audio:
                    if (audio.getnchannels(), audio.getsampwidth(), audio.getframerate()) != (1, 2, 16000):
                        raise ValueError("录音必须为16kHz单声道PCM")
                    if audio.getnframes() > 320000 or len(audio.readframes(320001)) != audio.getnframes() * 2:
                        raise ValueError("录音帧长度无效")
                job.update(state="queued", committed=time.time(),
                           followUp=self.follow.get(device, 0) > time.time(),
                           wake=body.get("wake") is True)
                self._save_job(folder, job)
                self.pool.submit(self._process_job, folder, raw)
            elif action == "read":
                if job["state"] != "done" or not (folder / "reply.wav").is_file():
                    raise ValueError("语音尚未生成")
                offset = body.get("offset")
                size = (folder / "reply.wav").stat().st_size
                if type(offset) is not int or not 0 <= offset < size or offset % 12288:
                    raise ValueError("音频偏移错误")
                with (folder / "reply.wav").open("rb") as audio:
                    audio.seek(offset)
                    raw = audio.read(12288)
                return {"ok": True, "data": base64.b64encode(raw).decode(), "offset": offset}
            elif action == "played":
                if job["state"] == "done" and job.get("result", {}).get("state") == "replied":
                    self.follow[device] = time.time() + 15
            elif action not in ("status", "begin", "commit"):
                raise ValueError("未知语音操作")
            return {"ok": True, **{k: job[k] for k in ("id", "state", "received")},
                    "result": job.get("result"), "error": job.get("error", "")}

    def _prewarm(self, device):
        """device 为已验签终端；请求网关预载流式 ASR 并在就绪后播提示音。"""
        request = urllib.request.Request(self.gateway + "/v1/voice/prewarm", data=b"",
            method="POST", headers={"X-Device-Id": device})
        client = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with client.open(request, timeout=60) as res:
            return json.loads(res.read(4096))

    def _process_job(self, folder, raw):
        """folder/raw为已校验录音；只执行一次，进程中断后保留失败而非重放主人格操作。"""
        with self.lock:
            job = json.loads((folder / "job.json").read_text(encoding="utf-8"))
            job["state"] = "processing"
            self._save_job(folder, job)
        stage = "generation"
        start = time.monotonic()
        started = time.time()
        try:
            try:
                result = self.call(raw, job["device"], job["id"], job["followUp"],
                                   job.get("wake", False))
            except TypeError:
                result = self.call(raw, job["device"], job["id"], job["followUp"])
            if result.get("ok") is not True:
                raise RuntimeError(result.get("error", "语音网关失败"))
            timings = result.setdefault("timings", {})
            timings["gatewayRoundtripSeconds"] = round(time.monotonic() - start, 3)
            timings["uploadSeconds"] = round(job.get("committed", job["created"]) - job["created"], 3)
            timings["queueSeconds"] = round(started - job.get("committed", job["created"]), 3)
            audio = result.pop("audioBase64", None)
            if audio:
                data = base64.b64decode(audio, validate=True)
                if len(data) > 8 * 1024 * 1024 or not data.startswith(b"RIFF"):
                    raise ValueError("播报音频格式无效")
                (folder / "reply.wav").write_bytes(data)
                result.update(audioBytes=len(data), audioSha256=hashlib.sha256(data).hexdigest())
                stage = "playback"
                mark = time.monotonic()
                routed = self.playback(job["device"], job["id"], data) if self.playback else {}
                timings["playbackRouteSeconds"] = round(time.monotonic() - mark, 3)
                if routed.get("audioSuppressed"):
                    result.update(routed, audioBytes=0)
                if routed.get("remotePlaybackCompleted"):
                    result.update(routed, audioBytes=0)
                    with self.lock:
                        self.follow[job["device"]] = time.time() + 15
            result.pop("audioPath", None)
            timings["serverTurnSeconds"] = round(time.time() - job["created"], 3)
            job.update(state="done", result=result)
        except Exception as exc:
            message = ("语音已生成，但电脑播放未完成，请检查电脑连接和音频设备"
                       if stage == "playback" else "语音处理失败：" + type(exc).__name__)
            job.update(state="failed", error=message)
        with self.lock:
            self._save_job(folder, job)

    def _call_gateway(self, raw, device, ident, follow, wake=False):
        """raw为WAV；device/ident标识真实设备与幂等任务；follow由播报回执决定，wake由KWS决定。"""
        request = urllib.request.Request(self.gateway + "/v1/voice/turn", data=raw,
            headers={"Content-Type": "audio/wav", "X-Device-Id": device,
                     "X-Session-Id": ident, "X-Follow-Up": "1" if follow else "0",
                     "X-Wake-Hit": "1" if wake else "0"})
        client = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with client.open(request, timeout=240) as res:
            raw = res.read(12 * 1024 * 1024 + 1)
        if len(raw) > 12 * 1024 * 1024:
            raise ValueError("语音网关响应过大")
        return json.loads(raw)

    def _save_job(self, folder, job):
        """folder/job为私有任务目录与元数据；原子落盘，音频不进入日志。"""
        temp = folder / "job.tmp"
        temp.write_text(json.dumps(job, ensure_ascii=False), encoding="utf-8")
        temp.replace(folder / "job.json")

    def _cleanup(self):
        """无参数；清理十分钟前的终态或未上传任务，不删除正在执行的推理。"""
        for path in self.root.glob("*/*/job.json"):
            job = json.loads(path.read_text(encoding="utf-8"))
            if time.time() - job["created"] > 600 and job["state"] not in ("queued", "processing"):
                target = path.parent.resolve()
                target.relative_to(self.root.resolve())
                if target != self.root.resolve():
                    shutil.rmtree(target)
