# -*- coding: utf-8 -*-
"""多音轨选择对话框"""

from typing import List, Optional

from qfluentwidgets import BodyLabel, ComboBox, MessageBoxBase, SubtitleLabel


class AudioTrackSelectionDialog(MessageBoxBase):
    """让用户选择要识别哪一个音轨"""

    def __init__(
        self, audio_streams: List, parent=None, file_name: str = ""
    ) -> None:
        super().__init__(parent)
        self.audio_streams = audio_streams or []
        self.selected_index: Optional[int] = None  # 取消时为 None

        self.titleLabel = SubtitleLabel(self.tr("检测到多个音轨"), self)
        self.viewLayout.addWidget(self.titleLabel)

        if file_name:
            desc = self.tr("文件「{}」包含 {} 个音轨，请选择要识别的那一个：").format(
                file_name, len(self.audio_streams)
            )
        else:
            desc = self.tr("该文件包含 {} 个音轨，请选择要识别的那一个：").format(
                len(self.audio_streams)
            )
        self.descLabel = BodyLabel(desc, self)
        self.descLabel.setWordWrap(True)
        self.viewLayout.addWidget(self.descLabel)

        self.comboBox = ComboBox(self)
        for i, stream in enumerate(self.audio_streams):
            self.comboBox.addItem(self._format_stream(stream, i), userData=i)
        self.comboBox.setMinimumWidth(320)
        self.viewLayout.addWidget(self.comboBox)

        self.hintLabel = BodyLabel(
            self.tr("无法播放声音时，可以逐个音轨试听后再做选择。"), self
        )
        self.hintLabel.setStyleSheet("color: rgba(255, 255, 255, 0.55);")
        self.viewLayout.addWidget(self.hintLabel)

        self.setWindowTitle(self.tr("选择音轨"))
        self.widget.setMinimumWidth(460)

        self.yesButton.setText(self.tr("开始识别"))
        self.cancelButton.setText(self.tr("取消"))

    @staticmethod
    def _format_stream(stream, array_index: int) -> str:
        """把音轨信息格式化成可读文本"""
        parts = [f"{array_index + 1}."]

        title = getattr(stream, "title", "") or ""
        language = getattr(stream, "language", "") or ""
        codec = getattr(stream, "codec", "") or ""

        label = title or language or ""
        if label:
            parts.append(label)
        else:
            parts.append("默认音轨")

        extras = []
        if title and language:
            extras.append(language)
        if codec:
            extras.append(codec)
        if extras:
            parts.append("(" + " · ".join(extras) + ")")

        return " ".join(parts)

    def accept(self) -> None:
        idx = self.comboBox.currentData()
        self.selected_index = idx if idx is not None else 0
        super().accept()

    def reject(self) -> None:
        self.selected_index = None
        super().reject()


def detect_and_choose_audio_track(
    file_path: str, parent=None, video_info=None
) -> tuple:
    """检测音轨，多音轨时弹窗让用户选择。

    Returns:
        (audio_track_index, cancelled)
        单音轨或检测失败时返回 (0, False)；
        用户取消时返回 (0, True)。
    """
    from pathlib import Path

    from videocaptioner.core.utils.video_utils import get_video_info

    try:
        if video_info is None:
            video_info = get_video_info(file_path)
        streams = getattr(video_info, "audio_streams", None) or []
    except Exception:
        return 0, False

    if len(streams) <= 1:
        return 0, False

    dialog = AudioTrackSelectionDialog(
        streams, parent, file_name=Path(file_path).name
    )
    if not dialog.exec():
        return 0, True

    return dialog.selected_index or 0, False
