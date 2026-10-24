"""GUI entry point — launchable via `videocaptioner` (no args) or `python -m videocaptioner.ui.main`."""

import os
import platform
import sys


def main():
    import traceback

    # 尽早安装：避免 ffmpeg/ffprobe/whisper 等控制台程序弹出黑框窗口。
    # 放在最前面，确保后续所有子进程调用都被覆盖（含第三方库内部调用）。
    from videocaptioner.core.utils.silent_process import install_silent_subprocess

    install_silent_subprocess()

    from PyQt5.QtCore import Qt, QTranslator
    from PyQt5.QtWidgets import QApplication

    from videocaptioner.config import TRANSLATIONS_PATH
    from videocaptioner.core.utils.cache import disable_cache, enable_cache
    from videocaptioner.core.utils.logger import setup_logger

    # Suppress qfluentwidgets ad
    with open(os.devnull, "w") as _devnull:
        sys.stdout, _stdout = _devnull, sys.stdout
        from qfluentwidgets import FluentTranslator
        sys.stdout = _stdout

    from videocaptioner.ui.common.config import cfg
    from videocaptioner.ui.view.main_window import MainWindow

    # Qt platform plugin path
    lib_folder = "Lib" if platform.system() == "Windows" else "lib"
    plugin_path = os.path.join(
        sys.prefix, lib_folder, "site-packages", "PyQt5", "Qt5", "plugins"
    )
    os.environ["QT_QPA_PLATFORM_PLUGIN_PATH"] = plugin_path

    # Logger + global exception hook
    logger = setup_logger("VideoCaptioner")

    def exception_hook(exctype, value, tb):
        logger.error("".join(traceback.format_exception(exctype, value, tb)))
        sys.__excepthook__(exctype, value, tb)

    sys.excepthook = exception_hook

    # Cache
    if cfg.get(cfg.cache_enabled):
        enable_cache()
    else:
        disable_cache()

    # DPI scaling
    if cfg.get(cfg.dpiScale) == "Auto":
        QApplication.setHighDpiScaleFactorRoundingPolicy(
            Qt.HighDpiScaleFactorRoundingPolicy.PassThrough  # type: ignore
        )
        QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)  # type: ignore
    else:
        os.environ["QT_ENABLE_HIGHDPI_SCALING"] = "0"
        os.environ["QT_SCALE_FACTOR"] = str(cfg.get(cfg.dpiScale))
    QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)  # type: ignore

    app = QApplication(sys.argv)
    app.setAttribute(Qt.AA_DontCreateNativeWidgetSiblings, True)  # type: ignore

    # 任务栏/固定项：让 Windows 把窗口归到 VideoCaptioner 而不是 pythonw
    from videocaptioner.core.utils.app_identity import apply_app_identity

    apply_app_identity()  # 进程标识 + 开始菜单快捷方式


    # i18n
    locale = cfg.get(cfg.language).value
    app.installTranslator(FluentTranslator(locale))
    my_translator = QTranslator()
    my_translator.load(str(TRANSLATIONS_PATH / f"VideoCaptioner_{locale.name()}.qm"))
    app.installTranslator(my_translator)

    w = MainWindow()
    w.show()

    # 窗口句柄就绪后写窗口级 AUMID（必须在 show 之后）
    try:
        from videocaptioner.core.utils.app_identity import set_window_app_id

        set_window_app_id(int(w.winId()))
    except Exception:
        pass

    # 主线程卡顿会被记录到 app.log，便于定位“界面无响应”类问题
    from videocaptioner.core.utils.ui_watchdog import UIWatchdog

    watchdog = UIWatchdog(app)
    watchdog.start()
    app.aboutToQuit.connect(watchdog.stop)

    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
