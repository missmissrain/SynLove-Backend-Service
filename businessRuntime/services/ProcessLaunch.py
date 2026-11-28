"""统一的后台进程静默启动工具。

功能：给所有"后台子进程"提供一致的无窗口启动参数，避免启动/巡检时闪出黑色控制台窗口。

原理（这是本文件存在的唯一理由，改之前务必读完）：
  1. `CREATE_NO_WINDOW` 会让控制台程序拿到一个**存在但不可见**的控制台，
     它再拉起的 tasklist / git / powershell / python 等控制台子进程会**继承**这个控制台，
     因此整棵进程树都不会新建窗口。
  2. `DETACHED_PROCESS` 会让父进程**完全没有控制台**，于是它的每个控制台子进程
     都会被 Windows 新建一个**可见**控制台窗口——这正是"启动时闪黑框"的根因。
  3. pythonw.exe 是 GUI 子系统程序，永远不会拥有控制台，
     所以凡是"会再拉起子进程"的后台 Python 必须用 python.exe + CREATE_NO_WINDOW，
     而不是 pythonw.exe。

注册参数：无（纯函数集合，不持有状态）。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

# CREATE_NO_WINDOW：创建控制台但不显示窗口；子进程会继承，不会各自弹窗。
NO_WINDOW = 0x08000000 if os.name == "nt" else 0


def creation_flags() -> int:
    """无参数；返回当前平台可用的"无窗口"创建标志（非 Windows 返回 0）。"""
    return NO_WINDOW


def startup_info() -> subprocess.STARTUPINFO | None:
    """无参数；返回隐藏窗口的 STARTUPINFO（非 Windows 返回 None）。

    与 CREATE_NO_WINDOW 叠加使用，覆盖个别 Windows 版本忽略创建标志的情况。
    """
    if os.name != "nt":
        return None
    info = subprocess.STARTUPINFO()
    info.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    info.wShowWindow = subprocess.SW_HIDE
    return info


def console_python(path: str | None = None) -> str:
    """参数 path：解释器路径，默认当前解释器；把 pythonw.exe 换成同目录 python.exe。

    用途：pythonw.exe 没有控制台，它拉起的子进程会各自新建可见窗口；
    后台服务统一用 python.exe + CREATE_NO_WINDOW，整棵树共享一个隐藏控制台。
    """
    candidate = Path(path or sys.executable or "python")
    if os.name == "nt" and candidate.name.lower() == "pythonw.exe":
        sibling = candidate.with_name("python.exe")
        if sibling.is_file():
            return str(sibling)
    return str(candidate)


def run_hidden(args, **kwargs):
    """参数 args：命令列表；静默执行并返回 CompletedProcess。

    默认补上 CREATE_NO_WINDOW / 隐藏 STARTUPINFO / stdin=DEVNULL；
    调用方仍可传 capture_output、timeout、text、encoding 等原生参数。
    """
    kwargs.setdefault("creationflags", creation_flags())
    if os.name == "nt":
        kwargs.setdefault("startupinfo", startup_info())
    kwargs.setdefault("stdin", subprocess.DEVNULL)
    return subprocess.run(args, **kwargs)


def spawn_hidden(args, **kwargs) -> subprocess.Popen:
    """参数 args：命令列表；静默启动并返回 Popen（不等待、不占窗口）。

    不要在这里加 DETACHED_PROCESS：那会让子进程失去控制台，
    它自己的控制台子进程就会各自弹出黑框。
    """
    kwargs.setdefault("creationflags", creation_flags())
    if os.name == "nt":
        kwargs.setdefault("startupinfo", startup_info())
    kwargs.setdefault("stdin", subprocess.DEVNULL)
    return subprocess.Popen(args, **kwargs)
