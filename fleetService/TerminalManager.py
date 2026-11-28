"""终端管理业务：验签登记心跳、生成接入文件、持久接收通知并投递服务器现有QQ收件箱。

注册参数config含runtimeDir、keyPath、relay连接、adminToken；通知目标与路径只在服务器私有配置保存。
"""
import hashlib
import json
import os
import time
import re
from pathlib import Path

from terminalConnection.security.DeviceIdentity import DeviceIdentity
from fleetService.DeviceStore import DeviceStore
from fleetService.DeviceRegistry import DeviceRegistry
from terminalConnection.completion.NoticeText import NoticeText
from fleetService.VoiceMailbox import VoiceMailbox
from fleetService.VoicePlaybackRouter import VoicePlaybackRouter
from fleetService.AudioRouteClient import AudioRouteClient


class TerminalManager:
    """管理设备注册与结果交接，不创建微信或QQ消息消费者。"""

    def __init__(self, config):
        """config为服务器私有配置；创建终端库及服务器签名身份，不读取其他设备私钥。"""
        self.config = config
        self.root = Path(config["runtimeDir"])
        self.root.mkdir(parents=True,exist_ok=True)
        self.identity = DeviceIdentity(config["keyPath"])
        self.store = DeviceStore(self.root/"devices.sqlite")
        # 设备清单只由 DeviceRegistry 导出，其他模块一律走 list()/get()。
        self.registry = DeviceRegistry(self.store, self.root/"devices.json")
        self.audio_routes = AudioRouteClient(config.get("voiceGatewayToken", ""))
        router = VoicePlaybackRouter(self.store,
            config.get("voiceRoutingPath") or
            Path(config["keyPath"]).resolve().parent.parent / "config/VoiceRouting.json",
            queue_command=self.queue_command,
            resolver=self.audio_routes.request_route if config.get("unifiedAudioRouting") is True else None)
        self.voice = VoiceMailbox(self.root/"voice", playback=router.route_audio)

    def queue_initiative_text(self, body):
        """body含id/text/expiresAt；仅给已有主人QQ目标写短消息，不接受模型指定收件人。"""
        ident, text, expires = body.get("id"), body.get("text"), body.get("expiresAt")
        if (not isinstance(ident, str) or not re.fullmatch("[a-f0-9]{32}", ident)
                or not isinstance(text, str) or not 1 <= len(text.strip()) <= 160
                or type(expires) not in (int, float) or not time.time() < expires <= time.time() + 180):
            raise ValueError("主动文字格式或时效无效")
        target, path = self.config.get("notificationTarget"), self.config.get("notificationInbox")
        if not target or not path:
            raise ValueError("主人QQ收件通道未配置")
        folder = Path(path)
        folder.mkdir(parents=True, exist_ok=True)
        key = hashlib.sha256(("screenInitiative:" + ident).encode()).hexdigest()
        final = folder / (key + ".json")
        receipts = self.root / "initiativeReceipts"
        receipts.mkdir(parents=True, exist_ok=True)
        # 收件箱已消费文件后，重复ID仍不能产生第二条消息。
        try:
            with (receipts / (ident + ".json")).open("x", encoding="utf-8") as receipt:
                json.dump({"id": ident, "createdAt": time.time()}, receipt)
            temp = folder / (key + ".tmp")
            with temp.open("w", encoding="utf-8") as out:
                json.dump({"id": key, "text": text.strip(), "target": {"userOpenid": target}},
                          out, ensure_ascii=False)
                out.flush()
                os.fsync(out.fileno())
            os.replace(temp, final)
        except FileExistsError:
            pass
        return {"ok": True, "queued": True, "channel": "qq"}

    def pairing_bundle(self, body):
        """body为管理员填写的设备信息；返回十分钟接入文件，内含公钥和一次性码。"""
        pair = self.store.create_pair(body)
        bundle = {**pair, "protocol":"synlove-terminal-v1",
            "serverPublicKey":self.identity.public_key(),
            **{key:self.config[key] for key in ("relayURL","relayToken")},
            "name":str(body.get("name") or pair["deviceId"]), "kind":body.get("kind","windows"),
            "transport":body.get("transport","direct"), "parentId":body.get("parentId","")}
        if self.config.get("tlsCaFile"):
            bundle["tlsCaPem"] = Path(self.config["tlsCaFile"]).read_text(encoding="utf-8")
        self.write_snapshot()
        return bundle

    def queue_command(self, device, command):
        """device/command为管理员请求；服务器本机交宿主执行，其他设备保持签名远控协议。"""
        key = self.store.queue_command(device, command)
        local = getattr(self, "local_device", None)
        if local and device == local.ident:
            local.enqueue_command(key)
        return key

    def receive_message(self, envelope):
        """envelope为来自VPS的配对或签名心跳；验证后返回服务器签名确认。"""
        if envelope.get("op") == "pair":
            device = self.store.pair_device(envelope)
            return self.identity.sign_envelope(device, {"ok":True,"deviceId":device})
        device = envelope.get("deviceId")
        row = self.store.get_device(device)
        body = DeviceIdentity.verify_envelope(row["publicKey"], envelope, device)
        if body.get("op") == "voice":
            with self.store.lock:
                row = self.store.get_device(device)
                if not row["enabled"] or row["bootId"] != body.get("bootId") or row["leaseUntil"] < time.time():
                    raise ValueError("语音设备租约无效")
                if "voice.turn" not in json.loads(row["capabilities"]):
                    raise ValueError("设备未提供语音能力")
            value = self.voice.exchange_voice(device, body)
            return self.identity.sign_envelope(device, value, encoded=True)
        if body.get("op") != "heartbeat":
            raise ValueError("不支持的终端报文")
        reply = self.store.receive_heartbeat(device,body,envelope.get("nonce",""))
        self.write_snapshot()
        # 升级期间旧客户端仍可接收旧格式回执，新客户端统一使用不可变载荷。
        return self.identity.sign_envelope(device,reply,encoded=envelope.get("encoding")=="base64-json")

    def write_snapshot(self):
        """无参数；交给 DeviceRegistry 刷新导出，本类不再直接写文件。"""
        self.registry.refresh()

    def deliver_notifications(self):
        """无参数；将终端持久通知写入服务器既有收件箱，去重ID稳定，不直接调用平台发送。"""
        target = self.config.get("notificationTarget")
        inbox_path = self.config.get("notificationInbox")
        with self.store.lock:
            rows = self.store.db.execute("SELECT * FROM messages WHERE delivered=0 LIMIT 20").fetchall()
            for row in rows:
                event = json.loads(row["body"])
                device = self.store.db.execute("SELECT * FROM devices WHERE id=?",
                    (row["deviceId"],)).fetchone()
                source = event.get("source")
                if (not device or not device["enabled"] or source not in ("codex","opencode")
                    or not json.loads(device["settings"]).get(source,False)):
                    self.store.db.execute("UPDATE messages SET delivered=1 WHERE id=?", (row["id"],))
                    self.store.db.commit()
                    continue
                # 即使尚未配置收件人，也须消耗已关闭的通知，防止稍后开启时补发旧事件。
                if not target or not inbox_path:
                    continue
                inbox = Path(inbox_path)
                inbox.mkdir(parents=True,exist_ok=True)
                key = hashlib.sha256(("terminal:"+row["id"]).encode()).hexdigest()
                body = NoticeText.clean_text(event.get("text", ""))
                if not body:
                    self.store.db.execute("UPDATE messages SET delivered=1 WHERE id=?", (row["id"],))
                    self.store.db.commit()
                    continue
                text = "%s · %s\n%s\n%s" % (
                    device["name"],source,event.get("title","任务完成"),body)
                item = {"id":key,"text":text[:28000],"target":{"userOpenid":target}}
                final = inbox/(key+".json")
                temp = final.with_suffix(".tmp")
                with temp.open("w",encoding="utf-8") as file:
                    json.dump(item,file,ensure_ascii=False)
                    file.flush()
                    os.fsync(file.fileno())
                os.replace(temp,final)
                self.store.db.execute("UPDATE messages SET delivered=1 WHERE id=?", (row["id"],))
                self.store.db.commit()
