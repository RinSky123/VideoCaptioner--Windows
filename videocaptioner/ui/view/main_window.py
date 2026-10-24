import atexit
import os
import shutil

import psutil
from PyQt5.QtCore import QSize, Qt, QThread, QUrl
from PyQt5.QtGui import QDesktopServices, QIcon
from PyQt5.QtWidgets import QApplication
from qfluentwidgets import FluentIcon as FIF
from qfluentwidgets import (
    FluentWindow,
    InfoBar,
    InfoBarPosition,
    MessageBox,
    NavigationItemPosition,
    SplashScreen,
)

from videocaptioner.config import ASSETS_PATH, GITHUB_REPO_URL
from videocaptioner.core.constant import INFOBAR_DURATION_FOREVER
from videocaptioner.core.utils.logger import setup_logger
from videocaptioner.ui.common.config import cfg
from videocaptioner.ui.components.DonateDialog import DonateDialog
from videocaptioner.ui.thread.version_checker_thread import VersionChecker
from videocaptioner.ui.view.batch_process_interface import BatchProcessInterface
from videocaptioner.ui.view.home_interface import HomeInterface
from videocaptioner.ui.view.llm_logs_interface import LLMLogsInterface
from videocaptioner.ui.view.setting_interface import SettingInterface
from videocaptioner.ui.view.subtitle_extract_interface import (
    SubtitleExtractInterface,
)
from videocaptioner.ui.view.subtitle_style_interface import SubtitleStyleInterface

LOGO_PATH = ASSETS_PATH / "logo.png"
logger = setup_logger("main_window")

# 本优化版自己的仓库（用户反馈走这里）
PORTABLE_REPO_URL = "https://github.com/RinSky123/VideoCaptioner-Portable-CN"
# 上游原项目（原版功能的作者）
UPSTREAM_REPO_URL = "https://github.com/WEIFENG2333/VideoCaptioner"


class MainWindow(FluentWindow):
    def __init__(self):
        super().__init__()
        self.initWindow()

        # 创建子界面
        self.homeInterface = HomeInterface(self)
        self.settingInterface = SettingInterface(self)
        self.subtitleStyleInterface = SubtitleStyleInterface(self)
        self.batchProcessInterface = BatchProcessInterface(self)
        self.subtitleExtractInterface = SubtitleExtractInterface(self)
        self.llmLogsInterface = LLMLogsInterface(self)

        # 初始化版本检查器
        self.versionChecker = VersionChecker()
        self.versionChecker.newVersionAvailable.connect(self.onNewVersion)
        self.versionChecker.announcementAvailable.connect(self.onAnnouncement)

        self.versionThread = QThread()
        self.versionChecker.moveToThread(self.versionThread)
        self.versionThread.started.connect(self.versionChecker.perform_check)
        self.versionThread.start()

        # 初始化导航界面
        self.initNavigation()
        self.splashScreen.finish()

        # 检查系统依赖
        self._check_ffmpeg()

        # 注册退出处理， 清理进程
        atexit.register(self.stop)

    def initNavigation(self):
        """初始化导航栏"""
        # 添加导航项
        self.addSubInterface(self.homeInterface, FIF.HOME, self.tr("主页"))
        self.addSubInterface(self.batchProcessInterface, FIF.VIDEO, self.tr("批量处理"))
        self.addSubInterface(
            self.subtitleExtractInterface, FIF.COPY, self.tr("字幕提取")
        )
        self.addSubInterface(self.subtitleStyleInterface, FIF.FONT, self.tr("字幕样式"))
        self.addSubInterface(self.llmLogsInterface, FIF.HISTORY, self.tr("请求日志"))

        self.navigationInterface.addSeparator()

        # 在底部添加自定义小部件
        self.navigationInterface.addItem(
            routeKey="avatar",
            text=self.tr("关于与反馈"),
            icon=FIF.GITHUB,
            onClick=self.onGithubDialog,
            position=NavigationItemPosition.BOTTOM,
        )
        self.addSubInterface(
            self.settingInterface,
            FIF.SETTING,
            self.tr("设置"),
            NavigationItemPosition.BOTTOM,
        )

        # 设置默认界面
        self.switchTo(self.homeInterface)

    def switchTo(self, interface):
        if interface.windowTitle():
            self.setWindowTitle(interface.windowTitle())
        else:
            self.setWindowTitle(self.tr("卡卡字幕助手 -- VideoCaptioner"))
        self.stackedWidget.setCurrentWidget(interface, popOut=False)

    def initWindow(self):
        """初始化窗口"""
        self.resize(1050, 800)
        self.setMinimumWidth(700)
        self.setWindowIcon(QIcon(str(LOGO_PATH)))
        self.setWindowTitle(self.tr("卡卡字幕助手 -- VideoCaptioner"))

        self.setMicaEffectEnabled(cfg.get(cfg.micaEnabled))

        # 创建启动画面
        self.splashScreen = SplashScreen(self.windowIcon(), self)
        self.splashScreen.setIconSize(QSize(106, 106))
        self.splashScreen.raise_()

        # 设置窗口位置, 居中
        desktop = QApplication.desktop().availableGeometry()
        w, h = desktop.width(), desktop.height()
        self.move(w // 2 - self.width() // 2, h // 2 - self.height() // 2)

        self.show()
        QApplication.processEvents()

    def onGithubDialog(self):
        """打开 GitHub 信息。

        这里有两个地址，别搞混：
        - 本优化版（便携增强版）→ 用户反馈问题、提需求走这里
        - 原项目 → 上游作者的仓库，原版功能的功劳归他
        """
        content = self.tr(
            "【本优化版】\n"
            "VideoCaptioner 便携增强版，在原项目 v1.4.2 基础上加入了\n"
            "下载修复、停顿断句、字幕提取、增量更新等改进。\n"
            "遇到本版的问题或想提建议，欢迎在这里反馈：\n"
            f"{PORTABLE_REPO_URL}\n\n"
            "【原项目】\n"
            "VideoCaptioner 由 WEIFENG2333 独立开发完成，原版功能请访问：\n"
            f"{UPSTREAM_REPO_URL}\n\n"
            "如果这个软件对你有帮助，也请给原项目点个 Star。"
        )

        w = MessageBox(self.tr("关于与反馈"), content, self)
        w.yesButton.setText(self.tr("打开本版仓库"))
        w.cancelButton.setText(self.tr("打开原项目"))

        if w.exec():
            QDesktopServices.openUrl(QUrl(PORTABLE_REPO_URL))
        else:
            QDesktopServices.openUrl(QUrl(UPSTREAM_REPO_URL))

    def onNewVersion(self, version, update_required, update_info, download_url):
        """新版本提示"""
        if update_required:
            title = "发现新版本, 需要更新"
            content = f"发现新版本 {version}\n\n" f"更新内容：\n{update_info}"
        else:
            title = "发现新版本"
            content = f"发现新版本 {version}\n\n{update_info}"

        w = MessageBox(title, content, self)
        w.yesButton.setText("立即更新")
        w.cancelButton.setText("稍后再说")

        if w.exec() or update_required:
            QDesktopServices.openUrl(QUrl(download_url))

        if update_required:
            self.homeInterface.setEnabled(False)
            self.batchProcessInterface.setEnabled(False)
            InfoBar.error(
                title="需要更新",
                content=self.tr("当前版本部分功能已被禁用。请尽快更新。"),
                isClosable=False,
                position=InfoBarPosition.BOTTOM,
                duration=-1,
                parent=self,
            )

    def onAnnouncement(self, content):
        """显示公告。

        这个槽由后台线程的信号触发，可能在窗口还没完全就绪时进来。
        直接 exec() 一个没有父窗口归属的模态框，一旦抢不到激活状态，
        用户看到的就是「界面点不动」——所以这里推迟到事件循环空闲再弹，
        并保证弹窗拿到焦点、可用 Esc 关闭。
        """
        from PyQt5.QtCore import QTimer

        # 延后一拍：让主窗口先完成布局与激活，弹窗才有正确的父窗口状态
        QTimer.singleShot(0, lambda: self._show_announcement(content))

    def _show_announcement(self, content):
        try:
            if not self.isVisible():
                return

            w = MessageBox("公告", content, self)
            w.yesButton.setText(self.tr("我知道了"))
            w.cancelButton.hide()

            # 保证用户一定能关掉：Esc 生效、弹窗置顶并激活
            w.setWindowModality(Qt.ApplicationModal)
            w.raise_()
            w.activateWindow()
            w.exec_()
        except Exception:
            # 公告只是锦上添花，出错绝不该影响主界面
            logger.exception("显示公告失败")

    def resizeEvent(self, e):
        super().resizeEvent(e)
        if hasattr(self, "splashScreen"):
            self.splashScreen.resize(self.size())

    def closeEvent(self, event):
        # 关闭所有子界面
        # self.homeInterface.close()
        # self.batchProcessInterface.close()
        # self.subtitleStyleInterface.close()
        # self.settingInterface.close()
        super().closeEvent(event)

        # 强制退出应用程序
        QApplication.quit()

        # 确保所有线程和进程都被终止 要是一些错误退出就不会处理了。
        # import os
        # os._exit(0)

    def stop(self):
        # 找到 FFmpeg 进程并关闭
        process = psutil.Process(os.getpid())
        for child in process.children(recursive=True):
            child.kill()

    def _check_ffmpeg(self):
        """检查 FFmpeg 是否已安装"""
        if shutil.which("ffmpeg") is None:
            InfoBar.warning(
                self.tr("FFmpeg 未安装"),
                self.tr("软件处理音视频文件时需要 FFmpeg，请先安装"),
                duration=INFOBAR_DURATION_FOREVER,
                position=InfoBarPosition.BOTTOM,
                parent=self,
            )
