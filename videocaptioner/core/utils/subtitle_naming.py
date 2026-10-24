"""字幕文件命名：给输出文件带上语言标注。

任务完成后会同时产出「原文」和「译文」两份字幕，仅凭文件名分不清
哪份是什么语言。这里统一生成形如：

    【字幕】视频名-中文.srt
    【字幕】视频名-法语.srt
    【字幕】视频名-中法双语.srt

同时把字幕统一为 SRT 格式，便于各种播放器和编辑器直接使用。
"""

import re
from typing import Optional

from videocaptioner.core.utils.logger import setup_logger

logger = setup_logger("subtitle_naming")

# langdetect 的 ISO 639-1 代码 -> 中文语言名
_LANG_NAMES = {
    "zh-cn": "中文",
    "zh-tw": "繁体中文",
    "zh": "中文",
    "en": "英语",
    "ja": "日语",
    "ko": "韩语",
    "fr": "法语",
    "de": "德语",
    "es": "西班牙语",
    "ru": "俄语",
    "pt": "葡萄牙语",
    "it": "意大利语",
    "nl": "荷兰语",
    "pl": "波兰语",
    "tr": "土耳其语",
    "ar": "阿拉伯语",
    "th": "泰语",
    "vi": "越南语",
    "id": "印尼语",
    "ms": "马来语",
    "tl": "菲律宾语",
    "hi": "印地语",
    "uk": "乌克兰语",
    "cs": "捷克语",
    "sv": "瑞典语",
    "da": "丹麦语",
    "fi": "芬兰语",
    "no": "挪威语",
    "el": "希腊语",
    "he": "希伯来语",
    "hu": "匈牙利语",
    "ro": "罗马尼亚语",
    "fa": "波斯语",
    "ca": "加泰罗尼亚语",
}


# 双语文件名里用的单字缩写：「中英双语」「中日双语」……
_SHORT_LABELS = {
    "简体中文": "中",
    "繁体中文": "繁",
    "中文": "中",
    "粤语": "粤",
    "英语": "英",
    "日语": "日",
    "韩语": "韩",
    "法语": "法",
    "德语": "德",
    "西班牙语": "西",
    "俄语": "俄",
    "葡萄牙语": "葡",
    "意大利语": "意",
    "荷兰语": "荷",
    "波兰语": "波",
    "土耳其语": "土",
    "希腊语": "希",
    "捷克语": "捷",
    "瑞典语": "瑞",
    "丹麦语": "丹",
    "芬兰语": "芬",
    "挪威语": "挪",
    "匈牙利语": "匈",
    "罗马尼亚语": "罗",
    "保加利亚语": "保",
    "乌克兰语": "乌",
    "阿拉伯语": "阿",
    "希伯来语": "希伯",
    "波斯语": "波斯",
    "泰语": "泰",
    "越南语": "越",
    "印尼语": "印尼",
    "马来语": "马来",
    "菲律宾语": "菲",
    "印地语": "印",
}


def short_language_label(name: str) -> str:
    """取语言的单字缩写，用于「中英双语」这类文件名。

    未知语言退化为语言名的第一个字。
    """
    key = (name or "").strip()
    if not key:
        return ""
    # 「英语(美国)」这类变体按主语言处理
    base = re.sub(r"[（(].*?[)）]", "", key).strip() or key
    if base in _SHORT_LABELS:
        return _SHORT_LABELS[base]
    return base[0]


def language_name_from_code(code: str) -> str:
    """把语言代码转成中文名；未知代码原样返回。"""
    key = (code or "").strip().lower()
    if not key:
        return ""
    return _LANG_NAMES.get(key, code)


# 中文语言名 -> 归类用的语言键（同一语言的地区变体归为同一个键）
_LANGUAGE_KEYS = {
    "简体中文": "zh",
    "中文": "zh",
    "繁体中文": "zh-tw",
    "粤语": "yue",
    "英语": "en",
    "英语(美国)": "en",
    "英语(英国)": "en",
    "英语（美国）": "en",
    "英语（英国）": "en",
    "葡萄牙语(巴西)": "pt",
    "葡萄牙语(葡萄牙)": "pt",
    "西班牙语(拉丁美洲)": "es",
}


def language_key(name: str) -> str:
    """把语言名归一成可比较的键，用于判断「原文和译文是否同一种语言」。

    未知语言按去掉地区后缀的名字比较。
    """
    key = (name or "").strip()
    if not key:
        return ""
    if key in _LANGUAGE_KEYS:
        return _LANGUAGE_KEYS[key]
    base = re.sub(r"[（(].*?[)）]", "", key).strip() or key
    return _LANGUAGE_KEYS.get(base, base)


def detect_language_name(text: str) -> str:
    """检测一段文本的语言，返回中文语言名。

    检测失败时返回空字符串，调用方应退化为不加后缀。
    """
    sample = (text or "").strip()
    if len(sample) < 8:
        return ""

    try:
        from langdetect import DetectorFactory, detect

        # 固定随机种子，避免同样的文本每次结果不同
        DetectorFactory.seed = 0
        code = detect(sample[:5000])
        return language_name_from_code(code)
    except Exception:
        return ""


def _sanitize(name: str) -> str:
    """去掉文件名中不允许的字符。"""
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", name or "")
    return cleaned.strip().strip(".")


def build_subtitle_name(
    video_stem: str,
    language: str = "",
    secondary_language: str = "",
    prefix: str = "【字幕】",
) -> str:
    """生成字幕文件名（不含扩展名）。

    Args:
        video_stem: 视频文件名（不含扩展名）
        language: 主要语言标注；为空则不加
        secondary_language: 双语时的第二种语言；两者都有则合并成「中法双语」
        prefix: 文件名前缀
    """
    stem = _sanitize(video_stem) or "subtitle"
    prefix = _sanitize(prefix)

    parts = []
    primary = _sanitize(language)
    secondary = _sanitize(secondary_language)

    if primary and secondary and language_key(primary) != language_key(secondary):
        # 中法双语 / 中日双语：取语言缩写，避免文件名太长
        parts.append(
            f"{short_language_label(primary)}{short_language_label(secondary)}双语"
        )
    elif primary:
        parts.append(primary)
    elif secondary:
        parts.append(secondary)

    if parts:
        return f"{prefix}{stem}-{parts[0]}"
    return f"{prefix}{stem}"


def unique_path(directory, name: str, ext: str = ".srt"):
    """避免覆盖已有文件：重名时追加 (2)、(3)……"""
    from pathlib import Path

    directory = Path(directory)
    candidate = directory / f"{name}{ext}"
    if not candidate.exists():
        return candidate

    index = 2
    while True:
        candidate = directory / f"{name} ({index}){ext}"
        if not candidate.exists():
            return candidate
        index += 1
        if index > 999:
            raise RuntimeError("无法生成唯一的字幕文件名")
