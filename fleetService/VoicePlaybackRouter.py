"""功能：把手机语音回复发送到配置选定的播放终端，采音设备与播放设备独立。

注册参数store为设备命令库、path为私有voiceRouting.json；无映射时保持采音设备播放。
默认单块16KiB，整段远程派发180秒，只有真实played回执才确认成功，不自动换输出设备。
"""
import base64
import hashlib
import json
import time
import uuid
from pathlib import Path



def _still_suitable(route, fresh, keys=("mode", "target", "revision", "reason")):
    """route 为原路由、fresh 为最新路由提供者；一致返回 True，异常或变化返回 False。"""
    def guard():
        """无参数；复核路由是否仍然一致。"""
        try:
            value = fresh()
        except Exception:
            return False
        if not isinstance(value, dict):
            return False
        return all(value.get(key) == route.get(key) for key in keys)
    return guard


class VoicePlaybackRouter:
    """通过已有签名命令和设备能力派发TTS，不开放音频公网地址或执行shell。"""

    def __init__(self, store, path, queue_command=None, resolver=None):
        """store为持久命令库，path为私有路由配置路径；每轮重新加载配置。"""
        self.store, self.path = store, Path(path)
        self.queue_command = queue_command or store.queue_command
        self.resolver = resolver

    def route_audio(self, source, ident, raw):
        """source为收音设备、ident为语音任务、raw为WAV；返回播放终端与实际完成状态。"""
        if self.resolver is not None:
            route = self.resolver({"action": "resolve", "source": source, "purpose": "reply",
                                   "askIfUncertain": True})["route"]
            if route.get("mode") != "tts":
                return {"audioSuppressed": True, "audioRoute": route}
            target = route["target"]
            if target == source:
                return {"audioRoute": route, "playbackDeviceId": source}
            still_suitable = _still_suitable(
                route, lambda: self.resolver({"action": "resolve", "source": source,
                                              "purpose": "reply"})["route"])
            result = self.route_to(target, ident, raw, not_after=time.time() + 120,
                                   guard=still_suitable, require_active=False)
            return {**result, "audioRoute": route}
        config = json.loads(self.path.read_text("utf-8-sig")) if self.path.is_file() else {}
        target = config.get("outputs", {}).get(source, source)
        if target == source:
            return {}
        if not isinstance(target, str) or not target:
            raise ValueError("语音播放设备配置无效")
        return self.route_to(target, ident, raw)

    def route_to(self, target, ident, raw, not_after=None, guard=None, require_active=True):
        """target为明确输出设备；not_after为主动语句截止秒，guard在入队播放前再次复核状态。"""
        if not_after is not None and time.time() >= not_after:
            raise TimeoutError("主动语音已经过期")
        deadline = time.monotonic() + 180
        pending = [self._queue_command(target, ident, "begin", {
            "size": len(raw), "sha256": hashlib.sha256(raw).hexdigest()
        })]
        for offset in range(0, len(raw), 16384):
            pending.append(self._queue_command(target, ident, "write", {
                "offset": offset, "data": base64.b64encode(raw[offset:offset+16384]).decode()
            }))
            # 执行器按队列顺序串行执行，最多八项同批，避免逐块等待完整心跳往返。
            if len(pending) >= 8:
                for key in pending:
                    self._wait_command(key, deadline)
                pending.clear()
        for key in pending:
            self._wait_command(key, deadline)
        if (not_after is not None and time.time() >= not_after) or (guard and not guard()):
            raise TimeoutError("状态改变或主动语音过期，不再播放")
        key = self._queue_command(target, ident, "play",
            {} if not_after is None else {"notAfter": not_after, "requireActive": require_active})
        reply = self._wait_command(key, deadline)
        if reply.get("state") != "played":
            raise RuntimeError("播放设备未确认播完")
        return {"playbackDeviceId": target, "remotePlaybackCompleted": True}

    def _queue_command(self, target, ident, action, args):
        """target为设备、ident为语音ID、action/args为操作；固定幂等ID并持久入队。"""
        key = uuid.uuid5(uuid.NAMESPACE_URL,
                        "synlove-audio:" + ident + ":" + action + ":" + str(args.get("offset", ""))).hex
        self.queue_command(target, {"id": key, "type": "audio",
            "params": {"action": action, "audioId": ident, **args}})
        return key

    def _wait_command(self, key, deadline):
        """key为已有命令、deadline为整轮期限；只查原命令，拒绝虚假或失败回执。"""
        while time.monotonic() < deadline:
            value = self.store.command_result(key)
            if value["state"] == "done":
                result = value.get("result") or {}
                if not result.get("ok"):
                    raise RuntimeError("播放设备执行失败")
                return result
            time.sleep(.15)
        raise TimeoutError("播放设备未及时响应")
