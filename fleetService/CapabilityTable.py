"""功能：终端能力与命令类型的唯一映射表，新增能力只改这一处。

注册参数无；本类只做「命令类型 → 所需能力」的查询与校验，不发命令、不碰数据库。
DeviceStore、TerminalMcp 以及后续管理端都从这里取映射，禁止各自再维护一份。
"""


class CapabilityTable:
    """命令类型到设备能力的唯一映射。"""

    # 命令类型 → 所需能力；键支持别名，值必须与终端上报的能力名一致。
    COMMANDS = {
        "cmd": "shell.exec",
        "music": "music.play",
        "probe": "device.probe",
        "software": "software.locate",
        "file": "file.transfer",
        "screen_capture": "screen.capture",
        "codex_job": "coding.run",
        "audio": "audio.playback",
        "screen_observe": "screen.observe",
        "media": "media.display",
        "media.display": "media.display",
        "notices": "notification.listen",
        "notice": "notification.listen",
        "gui": "gui.control",
        "gui.action": "gui.control",
        "remote_gui": "gui.control",
        "tree": "gui.tree",
        "ui_tree": "gui.tree",
        "gui.tree": "gui.tree",
        "ui.tree": "gui.tree",
        "control_tree": "gui.tree",
        "screen.capture": "screen.capture",
        "remote_screen_capture": "screen.capture",
        "update": "update.install",
        "update.install": "update.install",
        "root.status": "shell.exec",
        "listening_start": "audio.capture",
        "listening_stop": "audio.capture",
        "listening_status": "audio.capture",
        "audio.route": "audio.route",
        "route": "audio.route",
        "camera.presence": "camera.presence",
        "camera": "camera.presence",
        "location.coarse": "location.coarse",
        "location": "location.coarse",
        "device.probe": "device.probe",
        "avatar.action": "avatar.action",
        "avatar.speech": "avatar.speech",
        "shell.exec": "shell.exec",
        "audio.playback": "audio.playback",
    }

    # 规范化：不同历史叫法统一到设备真正执行的命令类型。
    ALIASES = {
        "media.display": "media",
        "notice": "notices",
        "gui.action": "gui",
        "remote_gui": "gui",
        "ui_tree": "tree",
        "gui.tree": "tree",
        "ui.tree": "tree",
        "control_tree": "tree",
        "screen.capture": "screen_capture",
        "remote_screen_capture": "screen_capture",
        "update.install": "update",
        "root.status": "cmd",
        "route": "audio.route",
        "camera": "camera.presence",
        "location": "location.coarse",
        "device.probe": "probe",
        "shell.exec": "shell.exec",
        "avatar.action": "avatar.action",
        "avatar.speech": "avatar.speech",
        "audio.playback": "audio",
    }

    @classmethod
    def capability_for(cls, kind):
        """kind为外部命令类型；返回所需能力名，未知返回 None。"""
        if not isinstance(kind, str):
            return None
        return cls.COMMANDS.get(kind.strip())

    @classmethod
    def command_type(cls, kind):
        """kind为外部命令类型；返回设备侧实际执行的类型。"""
        if not isinstance(kind, str):
            raise ValueError("命令类型无效")
        text = kind.strip()
        if text in cls.ALIASES:
            return cls.ALIASES[text]
        if text in cls.COMMANDS:
            return text
        raise ValueError("未知命令类型：" + text)

    @classmethod
    def require(cls, kind, capabilities):
        """kind为命令类型、capabilities为设备能力；不满足时抛 ValueError。"""
        needed = cls.capability_for(kind)
        if not needed:
            raise ValueError("未知命令类型：" + str(kind))
        if needed not in set(capabilities or []):
            raise ValueError("设备缺少能力：" + needed)
        return needed

    @classmethod
    def all_capabilities(cls):
        """无参数；返回本表涉及的全部能力名，供自检与文档使用。"""
        return sorted(set(cls.COMMANDS.values()))
