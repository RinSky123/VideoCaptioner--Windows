"""增量更新对话框。

让用户选一个更新包（.zip），程序会：
1. 显示包里的版本号和文件数
2. 校验完整性
3. 应用更新，并说明哪些东西不会被改动
"""

import os
from pathlib import Path

from PyQt5.QtCore import QThread, pyqtSignal
from PyQt5.QtWidgets import QFileDialog, QHBoxLayout, QVBoxLayout, QWidget
from qfluentwidgets import (
    BodyLabel,
    MessageBoxBase,
    ProgressBar,
    PushButton,
    SubtitleLabel,
)
from qfluentwidgets import FluentIcon as FIF

from videocaptioner.core.utils.logger import setup_logger
from videocaptioner.core.utils.updater import (
    app_version,
    apply_update,
    read_manifest_from_zip,
    verify_update_zip,
)

logger = setup_logger("update_dialog")


class _UpdateThread(QThread):
    progress = pyqtSignal(str)
    done = pyqtSignal(bool, str)

    def __init__(self, zip_path: str, root: Path):
        super().__init__()
        self.zip_path = zip_path
        self.root = root

    def run(self):
        try:
            self.progress.emit("正在校验更新包…")
            ok, reason, _ = verify_update_zip(self.zip_path, self.progress.emit)
            if not ok:
                self.done.emit(False, reason)
                return

            self.progress.emit("正在应用更新…")
            ok, reason = apply_update(
                self.zip_path, root=self.root, progress=self.progress.emit
            )
            self.done.emit(ok, reason)
        except Exception as e:
            logger.exception("更新过程异常")
            self.done.emit(False, str(e))


class UpdateDialog(MessageBoxBase):
    """增量更新对话框。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.zip_path = ""
        self.thread = None

        self.widget.setMinimumWidth(560)
        self._setup_ui()

    def _setup_ui(self):
        layout = QVBoxLayout()
        layout.setSpacing(10)

        title = SubtitleLabel(self.tr("检查更新"), self)
        layout.addWidget(title)

        self.current_label = BodyLabel(
            self.tr(f"当前版本：{app_version()}"), self
        )
        layout.addWidget(self.current_label)

        desc = BodyLabel(
            self.tr(
                "选择一个新的增量更新包（.zip）。更新只会替换有变化的文件，\n"
                "不会动你的设置、缓存、日志和已下载的语音识别模型。"
            ),
            self,
        )
        desc.setWordWrap(True)
        layout.addWidget(desc)

        pick_row = QHBoxLayout()
        self.pick_btn = PushButton(self.tr("选择更新包"), self, FIF.FOLDER)
        self.pick_btn.clicked.connect(self._choose_zip)
        pick_row.addWidget(self.pick_btn)
        pick_row.addStretch(1)
        layout.addLayout(pick_row)

        self.info_label = BodyLabel("", self)
        self.info_label.setWordWrap(True)
        layout.addWidget(self.info_label)

        self.progress_bar = ProgressBar(self)
        self.progress_bar.setVisible(False)
        layout.addWidget(self.progress_bar)

        self.status_label = BodyLabel("", self)
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        self.viewLayout.addLayout(layout)

        self.yesButton.setText(self.tr("开始更新"))
        self.yesButton.setEnabled(False)
        self.cancelButton.setText(self.tr("关闭"))
        self.yesButton.clicked.disconnect()
        self.yesButton.clicked.connect(self._start_update)

    def validate(self) -> bool:
        # 不让 MessageBoxBase 在点 yes 后自动关闭；更新结束由我们决定
        return False

    def _choose_zip(self):
        path, _ = QFileDialog.getOpenFileName(
            self, self.tr("选择更新包"), "", f"{self.tr('更新包')} (*.zip)"
        )
        if not path:
            return

        try:
            manifest = read_manifest_from_zip(path)
        except Exception as e:
            self.info_label.setText(self.tr(f"无法读取更新包：{e}"))
            self.yesButton.setEnabled(False)
            return

        self.zip_path = path
        self.info_label.setText(
            self.tr(
                f"更新包版本：{manifest.version}\n"
                f"包含 {len(manifest.files)} 个文件\n"
                + (f"\n说明：{manifest.notes}" if manifest.notes else "")
            )
        )
        self.yesButton.setEnabled(True)
        self.status_label.setText("")

    def _start_update(self):
        if not self.zip_path:
            return

        self.pick_btn.setEnabled(False)
        self.yesButton.setEnabled(False)
        self.progress_bar.setVisible(True)
        self.progress_bar.setValue(0)

        root = Path(self.zip_path).resolve().parent
        # 便携版根目录：包含 app 与 runtime 的那一层
        from videocaptioner.core.utils.updater import portable_root

        self.thread = _UpdateThread(self.zip_path, portable_root())
        self.thread.progress.connect(self.status_label.setText)
        self.thread.done.connect(self._on_done)
        self.thread.start()

    def _on_done(self, ok: bool, message: str):
        self.pick_btn.setEnabled(True)
        self.progress_bar.setVisible(False)

        if ok:
            self.status_label.setText(
                self.tr(f"{message}\n\n请关闭并重新启动程序，让更新生效。")
            )
            self.yesButton.setEnabled(False)
        else:
            self.status_label.setText(self.tr(f"更新未完成：{message}"))
            self.yesButton.setEnabled(True)
