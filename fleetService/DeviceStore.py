"""服务器设备数据库：统一维护登记、心跳、通知收件及远控指令，SQLite事务去重。

注册参数path为数据库，offline_seconds默认60秒；设备类型及传输协议独立于执行能力。
"""
import hashlib
import json
import re
import secrets
import sqlite3
import threading
import time
import uuid
from fleetService.CapabilityTable import CapabilityTable
from fleetService.DeviceProfile import DeviceProfile
from terminalConnection.privacy.TransientResults import TransientResults
from terminalConnection.privacy.CommandPrivacy import CommandPrivacy


class DeviceStore:
    """服务器设备状态的唯一写入者；不以配置启用代替在线心跳。"""

    def __init__(self, path, offline_seconds=60):
        """path为实例数据库；事务锁只用于短SQL，不持有网络连接或执行命令。"""
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        self.offline_seconds = offline_seconds
        self.transient = TransientResults()
        self.db.executescript("""
        PRAGMA journal_mode=WAL;
        PRAGMA busy_timeout=5000;
        CREATE TABLE IF NOT EXISTS devices(id TEXT PRIMARY KEY,name TEXT,kind TEXT,platform TEXT,
          transport TEXT,parentId TEXT,publicKey TEXT,enabled INTEGER,lastSeen REAL DEFAULT 0,
          bootId TEXT,leaseUntil REAL DEFAULT 0,capabilities TEXT DEFAULT '[]',settings TEXT DEFAULT '{}');
        CREATE TABLE IF NOT EXISTS pairs(code TEXT PRIMARY KEY,deviceId TEXT,expires REAL);
        CREATE TABLE IF NOT EXISTS messages(id TEXT PRIMARY KEY,deviceId TEXT,body TEXT,delivered INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS commands(id TEXT PRIMARY KEY,deviceId TEXT,body TEXT,state TEXT,result TEXT,created REAL);
        CREATE TABLE IF NOT EXISTS nonces(id TEXT PRIMARY KEY,expires REAL);
        """)
        for table, name, definition in (
            ("pairs", "publicKey", "TEXT"),
            ("nonces", "reply", "TEXT"),
            ("commands", "owner", "TEXT"),
        ):
            if name not in {row[1] for row in self.db.execute("PRAGMA table_info(" + table + ")")}:
                self.db.execute("ALTER TABLE " + table + " ADD COLUMN " + name + " " + definition)
        self.db.commit()
        # 心跳高频读取未完成队列，避免随着历史文件分块增长而扫描整张表。
        self.db.executescript("""
        CREATE INDEX IF NOT EXISTS commandDeviceStateCreated ON commands(deviceId,state,created);
        CREATE INDEX IF NOT EXISTS nonceExpires ON nonces(expires);
        CREATE INDEX IF NOT EXISTS messageDelivered ON messages(delivered);
        """)

    def list_devices(self):
        """无参数；返回公开设备字段，隐藏公钥、接入码和凭据；在线需当前心跳有效。"""
        with self.lock:
            result = []
            for row in self.db.execute("SELECT * FROM devices ORDER BY name"):
                item = {key: row[key] for key in ("id", "name", "kind", "platform", "transport", "parentId", "enabled", "lastSeen")}
                ready = bool(row["publicKey"])
                supported = DeviceProfile.supports_direct_client(row["kind"], row["transport"])
                item["online"] = bool(ready and supported and row["enabled"] and
                                      row["lastSeen"] > 0 and
                                      time.time() - row["lastSeen"] < self.offline_seconds)
                item["identityReady"] = ready
                item["adapterSupported"] = supported
                item["status"] = ("disabled" if not row["enabled"] else
                                  "reserved" if not ready else
                                  "online" if item["online"] else "offline")
                item["capabilities"] = json.loads(row["capabilities"])
                item["settings"] = json.loads(row["settings"])
                result.append(item)
            return result

    def reserve_device(self, data):
        """data含设备类型和父设备关系；仅预登记，绝不创建身份或在线心跳。"""
        device = DeviceProfile.normalize_registration(data)
        with self.lock, self.db:
            self.register_device(device)
        return device

    def register_device(self, device):
        """device为规范字段；在调用者事务内校验父链并保存，禁止覆盖已配对身份。"""
        ident = device["id"]
        prior = self.db.execute("SELECT publicKey FROM devices WHERE id=?", (ident,)).fetchone()
        if prior and prior["publicKey"]:
            raise ValueError("设备已登记，重新配对前必须明确移除旧身份")
        DeviceProfile.validate_parent(self.db, device)
        self.db.execute(
            "INSERT INTO devices(id,name,kind,platform,transport,parentId,enabled) "
            "VALUES(?,?,?,?,?,?,1) ON CONFLICT(id) DO UPDATE SET name=excluded.name,"
            "kind=excluded.kind,transport=excluded.transport,parentId=excluded.parentId",
            (ident, device["name"], device["kind"], "", device["transport"], device["parentId"]))
        self.db.execute("DELETE FROM pairs WHERE deviceId=?", (ident,))

    def create_pair(self, data):
        """data为直连设备登记字段；创建十分钟接入码，预留ADB和嵌入式设备只能登记。"""
        device = DeviceProfile.normalize_registration(data)
        if not DeviceProfile.supports_direct_client(device["kind"], device["transport"]):
            raise ValueError("该设备适配器尚未实现，请先预登记")
        ident = device["id"]
        code = secrets.token_urlsafe(24)
        with self.lock, self.db:
            self.register_device(device)
            self.db.execute("INSERT INTO pairs(code,deviceId,expires) VALUES(?,?,?)",
                (hashlib.sha256(code.encode()).hexdigest(), ident, time.time()+600))
        return {"deviceId": ident, "pairCode": code}

    def pair_device(self, body):
        """body含一次性接入码和终端公钥；事务消耗接入码，同一设备不得静默替换公钥。"""
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        import base64
        public = body.get("publicKey", "")
        Ed25519PublicKey.from_public_bytes(base64.b64decode(public, validate=True))
        with self.lock, self.db:
            code = hashlib.sha256(str(body.get("pairCode", "")).encode()).hexdigest()
            row = self.db.execute("SELECT * FROM pairs WHERE code=? AND expires>?", (code,time.time())).fetchone()
            if not row:
                raise ValueError("接入码无效或已使用")
            if row["publicKey"]:
                if row["publicKey"] != public:
                    raise ValueError("接入码已被另一身份使用")
                return row["deviceId"]
            device = self.get_device(row["deviceId"])
            if device["publicKey"] and device["publicKey"] != public:
                raise ValueError("设备已有其他身份")
            self.db.execute("UPDATE devices SET publicKey=? WHERE id=?", (public, row["deviceId"]))
            # 保留十分钟的同公钥回执，避免服务器已受理但终端断网导致无法完成安装。
            self.db.execute("UPDATE pairs SET publicKey=? WHERE code=?", (public,code))
            return row["deviceId"]

    def get_device(self, ident):
        """ident为设备ID；内部读取身份信息，只供签名验证，不直接返回GUI。"""
        with self.lock:
            row = self.db.execute("SELECT * FROM devices WHERE id=? AND enabled=1", (ident,)).fetchone()
            if not row:
                raise ValueError("设备未登记或已停用")
            return dict(row)

    def receive_heartbeat(self, ident, body, nonce):
        """ident/body/nonce为已验签心跳；持久收件后确认ID，重复指令由终端日志阻止重执行。"""
        now = time.time()
        boot = body.get("bootId", "")
        if not isinstance(boot,str) or not re.fullmatch("[a-f0-9]{32}", boot):
            raise ValueError("终端启动标识无效")
        if not isinstance(nonce,str) or not re.fullmatch("[a-f0-9]{32}", nonce):
            raise ValueError("心跳标识无效")
        if any(not isinstance(body.get(key,[]),list) or len(body.get(key,[]))>20
               for key in ("events","results")):
            raise ValueError("终端批次必须为最多20项的列表")
        with self.lock, self.db:
            device = self.get_device(ident)
            if not DeviceProfile.supports_direct_client(device["kind"], device["transport"]):
                raise ValueError("预留设备不能通过直连心跳声明在线")
            if not device["publicKey"]:
                raise ValueError("设备尚未完成配对")
            prior=self.db.execute("SELECT reply FROM nonces WHERE id=? AND expires>?",
                (ident+":"+nonce,now)).fetchone()
            if prior:
                if device["bootId"] != boot:
                    return {"ok":False,"conflict":True,"error":"旧实例回执已失效"}
                reply = self.transient.read_result("nonce:" + ident + ":" + nonce, json.loads(prior["reply"]))
                if reply.get("released"):
                    return {"ok":False,"error":"回执正文已释放，请使用新的心跳标识"}
                return reply
            if device["bootId"] and device["bootId"] != boot and device["leaseUntil"] > now:
                return {"ok": False, "error": "设备已有活动实例", "conflict": True}
            if device["bootId"] and device["bootId"] != boot:
                # 已交给旧进程的命令不再派给新进程，避免断网或重启后执行两次。
                unknown=json.dumps({"ok":False,"unknown":True,
                    "error":"原终端实例已离线，执行结果未知；未重新派发"},ensure_ascii=False)
                self.db.execute("UPDATE commands SET state='done',result=? "
                    "WHERE deviceId=? AND state='queued' AND owner IS NOT NULL AND owner<>?",
                    (unknown,ident,boot))
            self.db.execute("DELETE FROM nonces WHERE expires<?", (now,))
            caps = body.get("capabilities", [])
            if not isinstance(caps, list) or len(caps)>32 or not all(isinstance(x,str) and len(x)<80 for x in caps):
                raise ValueError("能力列表无效")
            caps = DeviceProfile.validate_capabilities(device["kind"], caps)
            self.db.execute("UPDATE devices SET lastSeen=?,bootId=?,leaseUntil=?,platform=?,capabilities=? WHERE id=?",
                (now,boot,now+90,str(body.get("platform",""))[:80],json.dumps(sorted(set(caps))),ident))
            accepted = []
            for event in body.get("events", [])[:20]:
                if not isinstance(event,dict):
                    raise ValueError("终端事件必须为对象")
                key = event.get("id","")
                if (not isinstance(key,str) or not re.fullmatch("[a-f0-9]{64}", key)
                    or len(json.dumps(event,ensure_ascii=False).encode("utf-8"))>30000
                    or event.get("source") not in ("codex","opencode")
                    or not isinstance(event.get("text",""),str) or not isinstance(event.get("title",""),str)):
                    raise ValueError("终端事件格式无效")
                value = {**event, "deviceName": device["name"]}
                self.db.execute("INSERT OR IGNORE INTO messages(id,deviceId,body) VALUES(?,?,?)",
                    (ident+":"+key,ident,json.dumps(value,ensure_ascii=False)))
                accepted.append(key)
            accepted_results=[]
            for item in body.get("results", [])[:20]:
                if not isinstance(item,dict):
                    raise ValueError("终端结果必须为对象")
                result = json.dumps(item.get("result"), ensure_ascii=False)
                if len(result)>40000:
                    raise ValueError("终端结果过大")
                prior=self.db.execute("SELECT state,body FROM commands WHERE id=? AND deviceId=?",
                    (item.get("id"),ident)).fetchone()
                if prior:
                    if prior["state"]=="queued" and (
                            json.loads(prior["body"]).get("type")=="screen_observe" or
                            CommandPrivacy.is_private(json.loads(prior["body"]))):
                        result=json.dumps(self.transient.store_result(item["id"],item.get("result")))
                    self.db.execute("UPDATE commands SET state='done',result=? WHERE id=? AND deviceId=? AND state='queued'",
                        (result,item.get("id"),ident))
                    accepted_results.append(item["id"])
            pending = [json.loads(row["body"]) for row in self.db.execute(
                "SELECT body FROM commands WHERE deviceId=? AND state='queued' "
                "AND (owner IS NULL OR owner=?) ORDER BY created LIMIT 10", (ident,boot))]
            commands = []
            for item in pending:
                command = self.transient.read_result("body:" + item["id"], item)
                if command.get("private"):
                    self.db.execute("UPDATE commands SET state='done',result=? WHERE id=?",
                        (json.dumps({"ok":False,"unknown":True,"error":"文件请求正文已释放，禁止自动重放"}), item["id"]))
                else:
                    commands.append(command)
            # Android 原生终端只认识 shell.exec/gui.action 等具体类型，这里把通用类型转过去。
            if device["kind"] == "android":
                for command in commands:
                    params = command.get("params") or {}
                    kind = command.get("type")
                    if kind == "cmd":
                        command["type"] = "shell.exec"
                        command.setdefault("command", params.get("command", ""))
                        command.setdefault("timeoutMs", int(params.get("timeoutSec", 15)) * 1000)
                    elif kind in ("gui", "gui.action", "remote_gui"):
                        command["type"] = "gui.action"
                        for key in ("action", "x", "y", "x2", "y2", "text", "durationMs",
                                    "viewId", "contentDescription", "index"):
                            if key in params and key not in command:
                                command[key] = params[key]
                    elif kind in ("tree", "ui.tree", "gui.tree", "control_tree"):
                        command["type"] = "gui.tree"
                        for key in ("maxDepth", "maxNodes"):
                            if key in params and key not in command:
                                command[key] = params[key]
                    elif kind in ("screen_capture", "screen.capture", "remote_screen_capture"):
                        command["type"] = "screen.capture"
                        for key in ("maxWidth", "jpegQuality"):
                            if key in params and key not in command:
                                command[key] = params[key]
                    elif kind in ("update", "update.install"):
                        command["type"] = "update"
                        for key in ("action", "url", "sha256", "manifestUrl"):
                            if key in params and key not in command:
                                command[key] = params[key]
                    elif kind in ("media", "media.display"):
                        command["type"] = "media"
                        for key in ("url", "kind", "action"):
                            if key in params and key not in command:
                                command[key] = params[key]
                    elif kind in ("notices", "notice"):
                        command["type"] = "notices"
                        for key in ("action", "sinceMinutes", "limit"):
                            if key in params and key not in command:
                                command[key] = params[key]
            for command in commands:
                self.db.execute("UPDATE commands SET owner=? WHERE id=? AND owner IS NULL",
                    (boot,command["id"]))
            reply={"ok": True, "accepted": accepted, "acceptedResults":accepted_results,
                "commands": commands, "settings": json.loads(device["settings"])}
            stored = (self.transient.store_result("nonce:" + ident + ":" + nonce, reply)
                      if any(CommandPrivacy.is_private(item) for item in commands) else reply)
            self.db.execute("INSERT INTO nonces(id,expires,reply) VALUES(?,?,?)",
                (ident+":"+nonce,now+180,json.dumps(stored,ensure_ascii=False)))
            return reply

    def update_device(self, ident, data):
        """ident为已知设备，data可修改名称、启用和通知开关；不接收身份公钥更新。"""
        with self.lock, self.db:
            row = self.db.execute("SELECT * FROM devices WHERE id=?", (ident,)).fetchone()
            if not row:
                raise ValueError("设备不存在")
            settings = json.loads(row["settings"])
            for source in ("codex","opencode"):
                if source in data.get("settings",{}):
                    if type(data["settings"][source]) is not bool:
                        raise ValueError("通知设置必须为布尔值")
                    settings[source] = data["settings"][source]
            enabled = data.get("enabled", bool(row["enabled"]))
            if type(enabled) is not bool:
                raise ValueError("启用设置必须为布尔值")
            self.db.execute("UPDATE devices SET name=?,enabled=?,settings=? WHERE id=?",
                (str(data.get("name",row["name"]))[:80],int(enabled),json.dumps(settings),ident))

    def queue_command(self, device, body):
        """device/body为管理员选中的设备与指令；校验当前能力、在线状态，持久保存后才返回请求ID。"""
        key = body.get("id") or uuid.uuid4().hex
        if not re.fullmatch("[a-f0-9]{32}", key):
            raise ValueError("指令标识无效")
        with self.lock, self.db:
            prior = self.db.execute("SELECT deviceId,body FROM commands WHERE id=?", (key,)).fetchone()
            if prior:
                previous=json.loads(prior["body"])
                requested={**body,"id":key}
                if "expiresAt" not in requested:
                    requested["expiresAt"]=previous["expiresAt"]
                if prior["deviceId"]!=device or previous!=CommandPrivacy.stored_command(requested):
                    raise ValueError("指令ID已被其他设备或内容占用")
                return key
            row = self.get_device(device)
            if not DeviceProfile.supports_direct_client(row["kind"], row["transport"]):
                raise ValueError("该设备执行适配器尚未实现")
            if time.time()-row["lastSeen"]>=self.offline_seconds:
                raise ValueError("目标设备离线")
            required = CapabilityTable.capability_for(body.get("type"))
            if not required:
                raise ValueError("未知指令类型：" + str(body.get("type")))
            if required not in json.loads(row["capabilities"]):
                raise ValueError("设备不支持该能力：" + required)
            params=body.get("params") or {}
            if not isinstance(params,dict):
                raise ValueError("指令参数必须为对象")
            command = {**body, "id":key, "expiresAt":time.time()+240}
            text = json.dumps(command,ensure_ascii=False)
            if len(text)>200000:
                raise ValueError("指令过大")
            if CommandPrivacy.is_private(command):
                self.transient.store_result("body:" + key, command)
                text = json.dumps(CommandPrivacy.stored_command(command),ensure_ascii=False)
            self.db.execute("INSERT OR IGNORE INTO commands(id,deviceId,body,state,result,created) "
                "VALUES(?,?,?,'queued',NULL,?)",
                (key,device,text,time.time()))
            return key

    def command_result(self, key):
        """key为已提交指令ID；查询结果，不重新创建或执行指令。"""
        with self.lock:
            row = self.db.execute("SELECT state,result FROM commands WHERE id=?", (key,)).fetchone()
            if not row:
                raise ValueError("指令不存在")
            value=json.loads(row["result"]) if row["result"] else None
            return {"id":key,"state":row["state"],"result":self.transient.read_result(key,value)}

    def instance_state(self, ident):
        """ident为设备ID；仅供认证管理员读取当前启动实例，便于受控升级后交接租约。"""
        with self.lock:
            row=self.get_device(ident)
            return {"id":ident,"bootId":row["bootId"],"leaseUntil":row["leaseUntil"]}

    def release_instance(self, ident, expected_boot):
        """ident/expected_boot为已停进程的设备和启动ID；比对一致才释放，禁止误释放新实例。"""
        if not isinstance(expected_boot,str) or not re.fullmatch("[a-f0-9]{32}",expected_boot):
            raise ValueError("必须指定已确认停止的实例")
        with self.lock,self.db:
            row=self.get_device(ident)
            if row["bootId"]!=expected_boot:
                raise ValueError("设备实例已变化，未释放租约")
            self.db.execute("UPDATE devices SET leaseUntil=0,lastSeen=0 WHERE id=? AND bootId=?",
                (ident,expected_boot))
