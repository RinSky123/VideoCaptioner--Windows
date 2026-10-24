"""让所有子进程静默运行，不弹出控制台窗口。

GUI 由 pythonw.exe 承载，本身没有控制台。此时再启动 ffmpeg / ffprobe /
whisper 这类控制台程序，Windows 会给它们新建一个控制台窗口——也就是用户
看到一闪而过的黑框。项目里部分调用点手动传了 CREATE_NO_WINDOW，但散落在
十几处，还有 pydub 等第三方库内部自己起进程的地方，逐一保证并不现实。

这里在进程级别给 subprocess.Popen 统一补上 CREATE_NO_WINDOW，从根源上
避免弹窗，也覆盖第三方库内部的调用。

注意必须用子类而不是替换成函数：标准库 asyncio.windows_utils 里有
``class Popen(subprocess.Popen)``，替换成函数会直接让它在导入时报
TypeError。
"""

import subprocess
import sys

CREATE_NO_WINDOW = 0x08000000

# creationflags 在 Popen.__init__ 的签名中位于第 14 个位置
_CREATIONFLAGS_POS = 13


def _merge_creationflags(existing: int) -> int:
    """在保留调用方意图的前提下补上 CREATE_NO_WINDOW。"""
    if existing & subprocess.CREATE_NEW_CONSOLE:
        # 调用方明确要求新控制台，尊重原意
        return existing
    return existing | CREATE_NO_WINDOW


def install_silent_subprocess() -> None:
    """给 subprocess.Popen 注入 CREATE_NO_WINDOW（仅 Windows）。"""
    if sys.platform != "win32":
        return
    if getattr(subprocess.Popen, "_silent_patched", False):
        return

    base = subprocess.Popen

    class SilentPopen(base):  # type: ignore[misc, valid-type]
        """Popen 子类：默认不创建控制台窗口。"""

        _silent_patched = True

        def __init__(self, *args, **kwargs):
            flags = kwargs.get("creationflags", 0)
            if len(args) > _CREATIONFLAGS_POS:
                flags = args[_CREATIONFLAGS_POS]
                args = list(args)
                args[_CREATIONFLAGS_POS] = _merge_creationflags(flags)
                args = tuple(args)
            else:
                kwargs["creationflags"] = _merge_creationflags(flags)
            super().__init__(*args, **kwargs)

    SilentPopen.__name__ = "Popen"
    SilentPopen.__qualname__ = "Popen"
    subprocess.Popen = SilentPopen  # type: ignore[assignment]

    try:
        from videocaptioner.core.utils.logger import setup_logger

        setup_logger("silent_process").debug(
            "已启用子进程静默模式（CREATE_NO_WINDOW）"
        )
    except Exception:
        pass


def silent_run_kwargs() -> dict:
    """给不经过 Popen 补丁的调用点使用的参数。"""
    if sys.platform != "win32":
        return {}
    return {"creationflags": CREATE_NO_WINDOW}
