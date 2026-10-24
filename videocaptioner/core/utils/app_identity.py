"""Windows 任务栏身份修复。

问题：本程序由 pythonw.exe 承载，Windows 会把窗口归到 pythonw 名下，
于是任务栏显示的是 Python 的图标和名称；右键"固定到任务栏"保存的是
pythonw.exe，而它带不上我们的脚本参数，所以点固定项无法启动。

解决：
1. 给进程设置 AppUserModelID，任务栏按这个 ID 归组；
2. 在开始菜单放一个同 ID 的快捷方式，指向 VideoCaptioner.exe。

两者 ID 一致时，任务栏会用快捷方式的图标和名字，固定项也指向 exe。
"""

import os
import sys

APP_USER_MODEL_ID = "VideoCaptioner.App.1"

# GETPROPERTYSTOREFLAGS: 0x2 = GPS_READWRITE
# （pywin32 未导出该常量，这里直接给数值）
_GPS_READWRITE = 2

_SHORTCUT_NAME = "VideoCaptioner.lnk"


def _root_dir() -> str:
    """便携目录根：<root>/videocaptioner/core/utils/app_identity.py -> <root>/app/.."""
    # __file__ = <root>/app/videocaptioner/core/utils/app_identity.py
    here = os.path.abspath(__file__)
    # utils -> core -> videocaptioner -> app -> <root>
    app_dir = os.path.dirname(
        os.path.dirname(os.path.dirname(os.path.dirname(here)))
    )
    return os.path.dirname(app_dir)


def _launcher_path() -> str:
    """返回启动器 exe 的路径（便携版为 <根目录>/VideoCaptioner.exe）。"""
    candidate = os.path.join(_root_dir(), "VideoCaptioner.exe")
    if os.path.isfile(candidate):
        return candidate
    # 源码运行 / 其他布局时退回当前解释器所在目录
    return os.path.join(os.path.dirname(sys.executable), "VideoCaptioner.exe")


def _set_process_app_id() -> bool:
    try:
        import ctypes
        from ctypes import wintypes

        shell32 = ctypes.windll.shell32
        # 必须声明宽字符参数类型，否则字符串会按 ANSI 传递而设置失败
        shell32.SetCurrentProcessExplicitAppUserModelID.argtypes = [wintypes.LPCWSTR]
        shell32.SetCurrentProcessExplicitAppUserModelID.restype = ctypes.c_long
        ret = shell32.SetCurrentProcessExplicitAppUserModelID(APP_USER_MODEL_ID)
        return ret == 0
    except Exception:
        return False


def _start_menu_dir() -> str:
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    return os.path.join(base, "Microsoft", "Windows", "Start Menu", "Programs")


def _icon_path() -> str:
    ico = os.path.join(_root_dir(), "app.ico")
    if os.path.isfile(ico):
        return ico
    return _launcher_path()


def ensure_start_menu_shortcut() -> str | None:
    """创建/更新带 AppUserModelID 的开始菜单快捷方式，返回其路径。"""
    launcher = _launcher_path()
    if not os.path.isfile(launcher):
        return None

    target = os.path.join(_start_menu_dir(), "VideoCaptioner", _SHORTCUT_NAME)

    com_initialized = False
    try:
        import pythoncom
        from win32com.propsys import propsys, pscon
        from win32com.shell import shell

        # 在 GUI 线程里 COM 通常已初始化，重复初始化会抛错，这里只做兜底
        try:
            pythoncom.CoInitialize()
            com_initialized = True
        except Exception:
            pass

        os.makedirs(os.path.dirname(target), exist_ok=True)

        # 已存在且指向正确、且已带 AUMID 则跳过
        if os.path.isfile(target):
            try:
                existing = propsys.SHGetPropertyStoreFromParsingName(
                    target, None, 0, propsys.IID_IPropertyStore
                )
                if APP_USER_MODEL_ID in str(
                    existing.GetValue(pscon.PKEY_AppUserModel_ID)
                ):
                    return target
            except Exception:
                pass

        shortcut = pythoncom.CoCreateInstance(
            shell.CLSID_ShellLink, None,
            pythoncom.CLSCTX_INPROC_SERVER, shell.IID_IShellLink,
        )
        shortcut.SetPath(launcher)
        shortcut.SetWorkingDirectory(os.path.dirname(launcher))
        shortcut.SetDescription("VideoCaptioner 卡卡字幕助手")
        shortcut.SetIconLocation(_icon_path(), 0)

        persist = shortcut.QueryInterface(pythoncom.IID_IPersistFile)
        persist.Save(target, 0)

        prop_store = propsys.SHGetPropertyStoreFromParsingName(
            target, None, _GPS_READWRITE, propsys.IID_IPropertyStore
        )
        prop_store.SetValue(
            pscon.PKEY_AppUserModel_ID,
            propsys.PROPVARIANTType(APP_USER_MODEL_ID),
        )
        prop_store.Commit()
        return target
    except Exception as e:
        try:
            from videocaptioner.core.utils.logger import setup_logger

            setup_logger("app_identity").warning(
                "创建开始菜单快捷方式失败: %s", e
            )
        except Exception:
            pass
        return None
    finally:
        if com_initialized:
            try:
                pythoncom.CoUninitialize()
            except Exception:
                pass


def set_window_app_id(hwnd: int) -> bool:
    """把 AppUserModelID 写到窗口属性上。

    这是控制任务栏分组最可靠的方式：Windows 按窗口的 AUMID 归组，
    与开始菜单里同 ID 的快捷方式匹配后，任务栏就显示该快捷方式的
    图标和名称，固定项也指向快捷方式（即 VideoCaptioner.exe）。
    """
    if sys.platform != "win32" or not hwnd:
        return False

    com_initialized = False
    try:
        import pythoncom
        from win32com.propsys import propsys, pscon

        try:
            pythoncom.CoInitialize()
            com_initialized = True
        except Exception:
            pass

        store = propsys.SHGetPropertyStoreForWindow(
            hwnd, propsys.IID_IPropertyStore
        )
        store.SetValue(
            pscon.PKEY_AppUserModel_ID,
            propsys.PROPVARIANTType(APP_USER_MODEL_ID),
        )
        store.Commit()
        return True
    except Exception:
        return False
    finally:
        if com_initialized:
            try:
                pythoncom.CoUninitialize()
            except Exception:
                pass


def apply_app_identity(window=None) -> None:
    """在 GUI 启动时调用：设置标识并准备开始菜单快捷方式。

    Args:
        window: Qt 窗口；给了就在窗口就绪后把 AUMID 写到窗口属性上。
    """
    if sys.platform != "win32":
        return

    _set_process_app_id()

    # 快捷方式只在便携版目录下创建，避免源码运行时乱写开始菜单
    if os.path.isfile(_launcher_path()):
        ensure_start_menu_shortcut()

    if window is not None:
        try:
            set_window_app_id(int(window.winId()))
        except Exception:
            pass
