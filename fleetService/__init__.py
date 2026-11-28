"""功能：设备管理（fleet）服务包，负责设备登记、指令队列、语音信箱与播放路由。

注册参数：无。本包属于后台服务，不再由终端库持有。
依赖说明：设备协议与隐私脱敏仍由 terminal-pc 提供，这里按相对位置加入 sys.path。
"""
import sys
from pathlib import Path

_TERMINAL = Path(__file__).resolve().parents[2] / "terminal-pc"
if _TERMINAL.is_dir() and str(_TERMINAL) not in sys.path:
    sys.path.insert(0, str(_TERMINAL))
