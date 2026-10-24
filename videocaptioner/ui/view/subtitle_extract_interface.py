"""内嵌字幕提取界面。

从 mkv / mp4 等容器里把内嵌的软字幕轨导出成 SRT，
导出后可以直接拿到「主页」去做断句和翻译。
"""

import os
from pathlib import Path

from PyQt5.QtCore import Qt, QThread, pyqtSignal
from PyQt5.QtWidgets import (
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    BodyLabel,
    InfoBar,
    InfoBarPosition,
    ProgressBar,
    PushButton,
    SubtitleLabel,
    TableWidget,
    ToolButton,
)
from qfluentwidgets import FluentIcon as FIF

from videocaptioner.core.constant import (
    INFOBAR_DURATION_ERROR,
    INFOBAR_DURATION_INFO,
    INFOBAR_DURATION_SUCCESS,
    INFOBAR_DURATION_WARNING,
)
from videocaptioner.core.subtitle.subtitle_extractor import (
    SUPPORTED_CONTAINERS,
    SubtitleTrack,
    extract_all_text_tracks,
    extract_track,
    list_subtitle_tracks,
)
from videocaptioner.core.utils.logger import setup_logger
from videocaptioner.core.utils.platform_utils import open_folder

logger = setup_logger("subtitle_extract_interface")


class _ProbeThread(QThread):
    """读取字幕轨（ffprobe 可能要几百毫秒，不能卡界面）。"""

    done = pyqtSignal(list, str)  # tracks, error

    def __init__(self, video_path: str):
        super().__init__()
        self.video_path = video_path

    def run(self):
        try:
            tracks = list_subtitle_tracks(self.video_path)
            self.done.emit(tracks, "")
        except Exception as e:
            logger.exception("读取字幕轨失败")
            self.done.emit([], str(e))


class _ExtractThread(QThread):
    """导出字幕轨。"""

    progress = pyqtSignal(str)
    done = pyqtSignal(list, list)  # saved_paths, skipped_messages

    def __init__(self, video_path: str, tracks: list):
        super().__init__()
        self.video_path = video_path
        self.tracks = tracks

    def run(self):
        saved = []
        skipped = []
        try:
            for track in self.tracks:
                self.progress.emit(f"正在处理：{track.display_name()}")
                try:
                    saved.append(
                        extract_track(
                            self.video_path, track, progress=self.progress.emit
                        )
                    )
                except Exception as e:
                    logger.warning("提取失败 %s: %s", track.display_name(), e)
                    skipped.append(f"{track.display_name()}：{e}")
        except Exception as e:
            logger.exception("提取过程异常")
            skipped.append(str(e))
        self.done.emit(saved, skipped)


class SubtitleExtractInterface(QWidget):
    """从视频文件里提取内嵌字幕。"""

    def __init__(self, parent=None):
        super().__init__(parent=parent)
        self.setObjectName("subtitleExtractInterface")
        self.setWindowTitle(self.tr("字幕提取"))
        self.setAcceptDrops(True)

        self.video_path = ""
        self.tracks = []
        self.probe_thread = None
        self.extract_thread = None

        self._init_ui()

    # ------------------------------------------------------------------ UI
    def _init_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(30, 20, 30, 20)
        layout.setSpacing(12)

        title = SubtitleLabel(self.tr("提取内嵌字幕"), self)
        layout.addWidget(title)

        desc = BodyLabel(
            self.tr(
                "从 MKV / MP4 等视频里把内嵌的软字幕轨导出成 SRT，"
                "导出后可以拿到「主页」继续断句和翻译。"
            ),
            self,
        )
        desc.setWordWrap(True)
        layout.addWidget(desc)

        # 文件选择行
        file_row = QHBoxLayout()
        file_row.setSpacing(8)

        self.file_label = BodyLabel(self.tr("还没有选择视频文件"), self)
        self.file_label.setWordWrap(True)

        self.choose_btn = PushButton(self.tr("选择视频"), self, FIF.FOLDER)
        self.choose_btn.clicked.connect(self._choose_file)

        self.open_folder_btn = PushButton(self.tr("打开所在文件夹"), self, FIF.FOLDER)
        self.open_folder_btn.clicked.connect(self._open_folder)
        self.open_folder_btn.setEnabled(False)

        file_row.addWidget(self.file_label, 1)
        file_row.addWidget(self.choose_btn)
        file_row.addWidget(self.open_folder_btn)
        layout.addLayout(file_row)

        # 轨道列表
        self.table = TableWidget(self)
        self.table.setColumnCount(4)
        self.table.setHorizontalHeaderLabels(
            [self.tr("字幕轨"), self.tr("语言"), self.tr("格式"), self.tr("可提取")]
        )
        self.table.setBorderVisible(True)
        self.table.setBorderRadius(8)
        self.table.setEditTriggers(TableWidget.NoEditTriggers)
        self.table.verticalHeader().setDefaultSectionSize(42)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.Stretch)
        header.setSectionResizeMode(1, QHeaderView.Fixed)
        header.setSectionResizeMode(2, QHeaderView.Fixed)
        header.setSectionResizeMode(3, QHeaderView.Fixed)
        self.table.setColumnWidth(1, 130)
        self.table.setColumnWidth(2, 150)
        self.table.setColumnWidth(3, 150)
        layout.addWidget(self.table, 1)

        # 操作行
        action_row = QHBoxLayout()
        action_row.setSpacing(8)

        self.extract_selected_btn = PushButton(self.tr("提取选中"), self, FIF.DOWNLOAD)
        self.extract_selected_btn.clicked.connect(self._extract_selected)
        self.extract_selected_btn.setEnabled(False)

        self.extract_all_btn = PushButton(self.tr("全部提取"), self, FIF.DOWNLOAD)
        self.extract_all_btn.clicked.connect(self._extract_all)
        self.extract_all_btn.setEnabled(False)

        self.refresh_btn = ToolButton(FIF.SYNC, self)
        self.refresh_btn.setToolTip(self.tr("重新读取字幕轨"))
        self.refresh_btn.clicked.connect(self._reload_tracks)
        self.refresh_btn.setEnabled(False)

        action_row.addWidget(self.file_label, 0) if False else None
        action_row.addStretch(1)
        action_row.addWidget(self.refresh_btn)
        action_row.addWidget(self.extract_selected_btn)
        action_row.addWidget(self.extract_all_btn)
        layout.addLayout(action_row)

        # 进度
        self.progress_bar = ProgressBar(self)
        self.progress_bar.setVisible(False)
        self.progress_label = BodyLabel("", self)
        self.progress_label.setVisible(False)
        layout.addWidget(self.progress_bar)
        layout.addWidget(self.progress_label)

    # -------------------------------------------------------------- 文件选择
    def _choose_file(self):
        patterns = " ".join(f"*{ext}" for ext in sorted(SUPPORTED_CONTAINERS))
        path, _ = QFileDialog.getOpenFileName(
            self,
            self.tr("选择视频文件"),
            "",
            f"{self.tr('视频文件')} ({patterns})",
        )
        if path:
            self.load_video(path)

    def _open_folder(self):
        if self.video_path:
            open_folder(str(Path(self.video_path).parent))

    def load_video(self, path: str):
        """载入视频并读取字幕轨。"""
        if not os.path.isfile(path):
            return

        self.video_path = path
        self.file_label.setText(Path(path).name)
        self.file_label.setToolTip(path)
        self.open_folder_btn.setEnabled(True)
        self._reload_tracks()

    def _reload_tracks(self):
        if not self.video_path:
            return

        self.table.setRowCount(0)
        self.extract_selected_btn.setEnabled(False)
        self.extract_all_btn.setEnabled(False)
        self.refresh_btn.setEnabled(False)
        self._set_busy(True, self.tr("正在读取字幕轨…"))

        self.probe_thread = _ProbeThread(self.video_path)
        self.probe_thread.done.connect(self._on_tracks_loaded)
        self.probe_thread.start()

    def _on_tracks_loaded(self, tracks: list, error: str):
        self._set_busy(False)
        self.refresh_btn.setEnabled(True)

        if error:
            self._show_error(self.tr("读取失败"), error)
            return

        self.tracks = tracks
        self._populate_table()

        if not tracks:
            InfoBar.info(
                self.tr("没有字幕轨"),
                self.tr("这个视频里没有内嵌软字幕。如果字幕是画面上的硬字幕，需要 OCR，本功能不支持。"),
                duration=5000,
                position=InfoBarPosition.BOTTOM,
                parent=self,
            )
            return

        extractable = [t for t in tracks if not t.is_image_based]
        self.extract_all_btn.setEnabled(bool(extractable))
        # 提示放在底部，避免遮住上方的说明文字
        InfoBar.success(
            self.tr("读取完成"),
            self.tr(f"找到 {len(tracks)} 条字幕轨，其中 {len(extractable)} 条可以转成 SRT"),
            duration=3000,
            position=InfoBarPosition.BOTTOM,
            parent=self,
        )

    def _populate_table(self):
        self.table.setRowCount(len(self.tracks))

        for row, track in enumerate(self.tracks):
            # display_name() 本身已带序号，这里不要再加一次
            name_item = QTableWidgetItem(track.display_name())
            name_item.setToolTip(track.title or track.codec)
            self.table.setItem(row, 0, name_item)

            lang_item = QTableWidgetItem(track.language_label)
            lang_item.setTextAlignment(Qt.AlignCenter)
            self.table.setItem(row, 1, lang_item)

            codec_item = QTableWidgetItem(track.codec_label)
            codec_item.setTextAlignment(Qt.AlignCenter)
            self.table.setItem(row, 2, codec_item)

            if track.is_image_based:
                ok_item = QTableWidgetItem(self.tr("不支持（图形）"))
                ok_item.setForeground(Qt.red)
            elif track.is_forced:
                ok_item = QTableWidgetItem(self.tr("可提取（强制）"))
                ok_item.setForeground(Qt.darkYellow)
            else:
                ok_item = QTableWidgetItem(self.tr("可提取"))
                ok_item.setForeground(Qt.green)
            ok_item.setTextAlignment(Qt.AlignCenter)
            self.table.setItem(row, 3, ok_item)

    # ------------------------------------------------------------------ 提取
    def _selected_rows(self) -> list:
        rows = {index.row() for index in self.table.selectedIndexes()}
        return sorted(rows)

    def _extract_selected(self):
        rows = self._selected_rows()
        if not rows:
            self._show_warning(self.tr("请先选中要提取的字幕轨"))
            return

        tracks = [self.tracks[r] for r in rows if 0 <= r < len(self.tracks)]
        self._start_extract(tracks)

    def _extract_all(self):
        tracks = [t for t in self.tracks if not t.is_image_based]
        if not tracks:
            self._show_warning(self.tr("没有可以提取的字幕轨"))
            return
        self._start_extract(tracks)

    def _start_extract(self, tracks: list):
        if not self.video_path or not tracks:
            return

        self._set_busy(True, self.tr("正在提取字幕…"))
        self.progress_bar.setVisible(True)
        self.progress_bar.setValue(0)
        self.progress_label.setVisible(True)

        self.extract_thread = _ExtractThread(self.video_path, tracks)
        self.extract_thread.progress.connect(self.progress_label.setText)
        self.extract_thread.done.connect(self._on_extract_done)
        self.extract_thread.start()

    def _on_extract_done(self, saved: list, skipped: list):
        self._set_busy(False)
        self.progress_bar.setVisible(False)
        self.progress_label.setVisible(False)

        if saved:
            names = "\n".join(p.name for p in saved)
            InfoBar.success(
                self.tr("提取完成"),
                self.tr(f"已导出 {len(saved)} 个字幕文件：\n{names}"),
                duration=6000,
                position=InfoBarPosition.BOTTOM,
                parent=self,
            )
            open_folder(str(Path(saved[0]).parent))

        if skipped:
            InfoBar.warning(
                self.tr("部分字幕未提取"),
                "\n".join(skipped[:5]),
                duration=8000,
                position=InfoBarPosition.BOTTOM,
                parent=self,
            )

        if not saved and not skipped:
            self._show_warning(self.tr("没有提取到任何字幕"))

    # ------------------------------------------------------------------ 辅助
    def _set_busy(self, busy: bool, message: str = ""):
        self.choose_btn.setEnabled(not busy)
        self.extract_all_btn.setEnabled(False)
        self.extract_selected_btn.setEnabled(False)
        self.refresh_btn.setEnabled(False)

        if busy:
            self.progress_label.setText(message)
            self.progress_label.setVisible(bool(message))
            return

        # 空闲：清空状态文字，避免残留"正在读取…"
        self.progress_label.setText("")
        self.progress_label.setVisible(False)

        if self.video_path:
            self.refresh_btn.setEnabled(True)
            self.extract_all_btn.setEnabled(
                any(not t.is_image_based for t in self.tracks)
            )
            self.extract_selected_btn.setEnabled(bool(self.tracks))

    def _show_error(self, title: str, content: str):
        InfoBar.error(title, content, duration=INFOBAR_DURATION_ERROR, parent=self)

    def _show_warning(self, content: str):
        InfoBar.warning(
            self.tr("提示"), content, duration=INFOBAR_DURATION_WARNING, parent=self
        )

    # ------------------------------------------------------------ 拖放支持
    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            for url in event.mimeData().urls():
                path = url.toLocalFile()
                if Path(path).suffix.lower() in SUPPORTED_CONTAINERS:
                    event.acceptProposedAction()
                    return
        event.ignore()

    def dropEvent(self, event):
        for url in event.mimeData().urls():
            path = url.toLocalFile()
            if os.path.isfile(path) and Path(path).suffix.lower() in SUPPORTED_CONTAINERS:
                self.load_video(path)
                return
