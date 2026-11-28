"""设备登记规范：区分设备类型、接入方式、父设备与当前实际支持的执行适配器。

本类无需注册参数；设备标识最长64字符，父设备链最多32层；Android支持原生直连，ADB网关仍预留。
"""
import re
import uuid


class DeviceProfile:
    """集中维护登记约束，防止GUI、安装器与服务器分别猜测设备能力。"""

    KINDS = ("windows", "linux", "android", "embedded", "gateway")
    TRANSPORTS = ("direct", "adb", "gateway")
    # Android 原生客户端通过 HTTPS 主动连接 VPS，与 Windows/Linux 客户端使用同一中继协议。
    DIRECT_KINDS = ("windows", "linux", "android", "gateway")

    @classmethod
    def describe_types(cls):
        """无传入参数；返回设备类型及适配器支持范围，预留类型不会被描述为已实现。"""
        return {
            "kinds": list(cls.KINDS),
            "transports": [
                {"id": "direct", "implemented": True,
                 "kinds": list(cls.DIRECT_KINDS), "requiresParent": False},
                {"id": "adb", "implemented": False,
                 "kinds": ["android"], "requiresParent": True},
                {"id": "gateway", "implemented": False,
                 "kinds": ["android", "embedded"], "requiresParent": True},
            ],
            "capabilitiesFrom": "verifiedHeartbeat",
            "androidDirectClient": True,
        }

    @classmethod
    def normalize_registration(cls, data):
        """data为登记对象；规范设备字段，不接收公钥、在线状态或用户声明的执行能力。"""
        if not isinstance(data, dict):
            raise ValueError("设备登记必须为对象")
        ident = data.get("deviceId") or "device" + uuid.uuid4().hex
        if not isinstance(ident, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,63}", ident):
            raise ValueError("设备标识格式错误")
        kind = data.get("kind", "windows")
        transport = data.get("transport", "direct")
        parent = data.get("parentId", "")
        if kind not in cls.KINDS or transport not in cls.TRANSPORTS:
            raise ValueError("设备类型或传输类型错误")
        if not isinstance(parent, str):
            raise ValueError("父设备标识必须为字符串")
        if parent and not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,63}", parent):
            raise ValueError("父设备标识格式错误")
        if transport == "direct" and parent:
            raise ValueError("直连设备不通过父设备转发")
        if transport != "direct" and not parent:
            raise ValueError("ADB或网关接入必须指定父设备")
        if transport == "adb" and kind != "android":
            raise ValueError("ADB接入仅适用于安卓设备")
        if transport == "gateway" and kind not in ("android", "embedded"):
            raise ValueError("网关接入仅适用于安卓或嵌入式子设备")
        name = data.get("name") or ident
        if not isinstance(name, str) or len(name) > 80:
            raise ValueError("设备名称必须为不超过80字符的字符串")
        return {"id": ident, "name": name, "kind": kind,
                "transport": transport, "parentId": parent}

    @staticmethod
    def validate_parent(db, device):
        """db为事务内连接，device为规范登记字段；检查父设备存在及关系无环。"""
        seen = {device["id"]}
        parent = device["parentId"]
        for _ in range(32):
            if not parent:
                return
            if parent in seen:
                raise ValueError("父设备关系不能形成循环")
            seen.add(parent)
            row = db.execute("SELECT parentId FROM devices WHERE id=?", (parent,)).fetchone()
            if not row:
                raise ValueError("父设备尚未登记")
            parent = row["parentId"] or ""
        raise ValueError("父设备层级过深")

    @classmethod
    def supports_direct_client(cls, kind, transport):
        """kind/transport为登记属性；返回Python或Android原生客户端已实现的组合。"""
        return transport == "direct" and kind in cls.DIRECT_KINDS

    @staticmethod
    def validate_capabilities(kind, caps):
        """kind/caps为已登记类型与心跳能力；Android只允许已实现的桌面、媒体和长期监听能力。"""
        android_allowed = ("device.probe", "avatar.action", "avatar.speech", "media.display", "voice.turn",
                           "audio.capture", "audio.playback", "shell.exec", "shell.root", "root.exec",
                           "gui.tree", "gui.control",
                           "screen.capture", "screen.observe", "ui.tree", "file.transfer", "file.stream",
                           "software.locate", "update.install", "notification.listen",
                           "camera.presence", "location.coarse", "audio.route")
        if kind == "android" and any(item not in android_allowed for item in caps):
            raise ValueError("Android终端声明了未实现或未授权的能力")
        return caps
