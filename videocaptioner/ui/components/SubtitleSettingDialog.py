from qfluentwidgets import (
    BodyLabel,
    ComboBoxSettingCard,
    MessageBoxBase,
    SwitchSettingCard,
)
from qfluentwidgets import FluentIcon as FIF

from videocaptioner.core.entities import SubtitleSplitTypeEnum
from videocaptioner.ui.common.config import cfg
from videocaptioner.ui.components.SpinBoxSettingCard import SpinBoxSettingCard


class SubtitleSettingDialog(MessageBoxBase):
    """字幕设置对话框"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.titleLabel = BodyLabel(self.tr("字幕设置"), self)

        # 创建设置卡片
        self.split_card = SwitchSettingCard(
            FIF.ALIGNMENT,
            self.tr("字幕分割"),
            self.tr("字幕是否使用大语言模型进行智能断句"),
            cfg.need_split,
            self,
        )

        # 分割类型：句子分段 / 语义分段
        self.split_type_card = ComboBoxSettingCard(
            cfg.split_type,
            FIF.TILES,
            self.tr("字幕分割类型"),
            self.tr("根据句子或者根据语义对字幕进行断句"),
            [t.value for t in SubtitleSplitTypeEnum],
            self,
        )

        self.word_count_cjk_card = SpinBoxSettingCard(
            cfg.max_word_count_cjk,
            FIF.TILES,  # type: ignore
            self.tr("中文最大字数"),
            self.tr("单条字幕的最大字数 (对于中日韩等字符)"),
            minimum=8,
            maximum=50,
            parent=self,
        )

        self.word_count_english_card = SpinBoxSettingCard(
            cfg.max_word_count_english,
            FIF.TILES,  # type: ignore
            self.tr("英文最大单词数"),
            self.tr("单条字幕的最大单词数 (英文)"),
            minimum=8,
            maximum=50,
            parent=self,
        )

        # 停顿达到阈值就断句（同一个人说话也算）
        self.speaker_gap_card = SpinBoxSettingCard(
            cfg.speaker_gap_ms,
            FIF.PEOPLE,  # type: ignore
            self.tr("停顿断句阈值"),
            self.tr("说话停顿达到此毫秒数就另起一条字幕（默认 1000 毫秒，即 1 秒）"),
            minimum=200,
            maximum=5000,
            step=100,
            parent=self,
        )

        self.remove_punctuation_card = SwitchSettingCard(
            FIF.EDIT,
            self.tr("去除末尾标点符号"),
            self.tr("是否去除中文字幕中的末尾标点符号"),
            cfg.remove_punctuation,
            self,
        )

        # 添加到布局
        self.viewLayout.addWidget(self.titleLabel)
        self.viewLayout.addWidget(self.split_card)
        self.viewLayout.addWidget(self.split_type_card)
        self.viewLayout.addWidget(self.word_count_cjk_card)
        self.viewLayout.addWidget(self.word_count_english_card)
        self.viewLayout.addWidget(self.speaker_gap_card)
        self.viewLayout.addWidget(self.remove_punctuation_card)
        # 设置间距
        self.viewLayout.setSpacing(10)

        # 设置窗口标题和宽度
        self.setWindowTitle(self.tr("字幕设置"))
        self.widget.setMinimumWidth(380)

        # 只显示取消按钮
        self.yesButton.hide()
        self.cancelButton.setText(self.tr("关闭"))
