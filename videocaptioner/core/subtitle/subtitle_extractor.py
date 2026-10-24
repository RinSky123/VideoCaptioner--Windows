"""从 mkv / mp4 等容器中提取内嵌软字幕。

参考 gMKVExtractGUI 的思路：先用 ffprobe 列出容器里的字幕轨，
再用 ffmpeg 把选定的轨道导出为 SRT。

与 gMKVExtractGUI 的区别：
- 不需要 mkvextract / MKVToolNix，只用程序自带的 ffmpeg/ffprobe
- 只做「字幕轨 -> SRT」这一件事，够用且轻
- 转成 SRT 而不是原样抽取，因为 SRT 能被本程序直接拿去断句/翻译
"""

import json
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, List, Optional

from videocaptioner.core.utils.logger import setup_logger

logger = setup_logger("subtitle_extractor")

# 这些容器格式默认就可能带内嵌字幕
SUPPORTED_CONTAINERS = {
    ".mkv", ".mk3d", ".mp4", ".m4v", ".mov", ".avi", ".ts", ".m2ts", ".webm", ".flv",
}

# 字幕编码 -> 是否图文（无法直接转成文字）
_IMAGE_BASED_CODECS = {
    "hdmv_pgs_subtitle", "dvd_subtitle", "dvb_subtitle", "xsub",
}

_CODEC_LABELS = {
    "subrip": "SRT",
    "srt": "SRT",
    "ass": "ASS",
    "ssa": "ASS",
    "webvtt": "WebVTT",
    "mov_text": "MP4 文本字幕",
    "hdmv_pgs_subtitle": "蓝光 PGS（图形）",
    "dvd_subtitle": "DVD 字幕（图形）",
    "dvb_subtitle": "DVB 字幕（图形）",
    "xsub": "XSUB（图形）",
    "text": "文本字幕",
}

_LANG_NAMES = {
    "chi": "中文", "zho": "中文", "zh": "中文", "cmn": "中文",
    "en": "英语", "eng": "英语",
    "ja": "日语", "jpn": "日语",
    "ko": "韩语", "kor": "韩语",
    "fr": "法语", "fre": "法语", "fra": "法语",
    "de": "德语", "ger": "德语", "deu": "德语",
    "es": "西班牙语", "spa": "西班牙语",
    "ru": "俄语", "rus": "俄语",
    "pt": "葡萄牙语", "por": "葡萄牙语",
    "it": "意大利语", "ita": "意大利语",
    "th": "泰语", "tha": "泰语",
    "vi": "越南语", "vie": "越南语",
}


@dataclass
class SubtitleTrack:
    """容器里的一条字幕轨。"""

    index: int              # 全局 stream index（ffmpeg -map 0:<index> 用）
    subtitle_ordinal: int   # 第几条字幕轨（0 起），用于 -map 0:s:<n>
    codec: str
    language: str = ""
    title: str = ""
    is_default: bool = False
    is_forced: bool = False
    is_image_based: bool = False

    @property
    def codec_label(self) -> str:
        return _CODEC_LABELS.get(self.codec, self.codec or "未知")

    @property
    def language_label(self) -> str:
        if not self.language or self.language == "und":
            return "未标注语言"
        return _LANG_NAMES.get(self.language.lower(), self.language)

    def display_name(self) -> str:
        """给用户看的完整描述。"""
        parts = [f"{self.subtitle_ordinal + 1}. {self.language_label}", self.codec_label]
        if self.title:
            parts.insert(2, f"「{self.title}」")
        if self.is_default:
            parts.append("默认")
        if self.is_forced:
            parts.append("强制")
        if self.is_image_based:
            parts.append("图形字幕·无法转文字")
        return " · ".join(parts)


def _ffmpeg_dir() -> Optional[Path]:
    """定位随包附带的 ffmpeg/ffprobe。"""
    try:
        from videocaptioner.config import BIN_PATH

        if (Path(BIN_PATH) / "ffprobe.exe").is_file():
            return Path(BIN_PATH)
    except Exception:
        pass
    which = shutil.which("ffprobe")
    if which:
        return Path(which).parent
    return None


def ffprobe_path() -> Optional[str]:
    d = _ffmpeg_dir()
    return str(d / "ffprobe.exe") if d else None


def ffmpeg_path() -> Optional[str]:
    d = _ffmpeg_dir()
    if d and (d / "ffmpeg.exe").is_file():
        return str(d / "ffmpeg.exe")
    return shutil.which("ffmpeg")


def _run_hidden(cmd: List[str], timeout: int = 60) -> subprocess.CompletedProcess:
    """静默运行外部命令。"""
    kwargs = {}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        **kwargs,
    )


def list_subtitle_tracks(video_path: str) -> List[SubtitleTrack]:
    """列出容器里的所有字幕轨。

    Raises:
        FileNotFoundError: 视频不存在
        RuntimeError: ffprobe 缺失或读取失败
    """
    path = Path(video_path)
    if not path.is_file():
        raise FileNotFoundError(f"文件不存在：{video_path}")

    probe = ffprobe_path()
    if not probe:
        raise RuntimeError("找不到 ffprobe，无法读取字幕轨信息")

    result = _run_hidden([
        probe,
        "-v", "error",
        "-print_format", "json",
        "-show_streams",
        "-select_streams", "s",
        str(path),
    ])

    if result.returncode != 0:
        detail = (result.stderr or "").strip()[:300]
        raise RuntimeError(f"读取字幕轨失败：{detail or 'ffprobe 返回错误'}")

    try:
        data = json.loads(result.stdout or "{}")
    except json.JSONDecodeError as e:
        raise RuntimeError(f"解析 ffprobe 输出失败：{e}") from e

    tracks: List[SubtitleTrack] = []
    for ordinal, stream in enumerate(data.get("streams", [])):
        codec = (stream.get("codec_name") or "").lower()
        tags = stream.get("tags") or {}
        disposition = stream.get("disposition") or {}

        tracks.append(
            SubtitleTrack(
                index=int(stream.get("index", ordinal)),
                subtitle_ordinal=ordinal,
                codec=codec,
                language=(tags.get("language") or "").strip(),
                title=(tags.get("title") or "").strip(),
                is_default=bool(disposition.get("default")),
                is_forced=bool(disposition.get("forced")),
                is_image_based=codec in _IMAGE_BASED_CODECS,
            )
        )

    logger.info("在 %s 中发现 %d 条字幕轨", path.name, len(tracks))
    return tracks


def unique_output_path(directory: Path, stem: str, suffix: str = "") -> Path:
    """避免覆盖已有的同名文件。"""
    candidate = directory / f"{stem}{suffix}.srt"
    if not candidate.exists():
        return candidate
    index = 2
    while True:
        candidate = directory / f"{stem}{suffix} ({index}).srt"
        if not candidate.exists():
            return candidate
        index += 1
        if index > 999:
            raise RuntimeError("无法生成唯一的输出文件名")


def extract_track(
    video_path: str,
    track: SubtitleTrack,
    output_path: Optional[str] = None,
    progress: Optional[Callable[[str], None]] = None,
) -> Path:
    """把一条字幕轨导出为 SRT。

    Args:
        video_path: 源视频
        track: 由 list_subtitle_tracks() 得到的轨道
        output_path: 输出路径；省略时写在视频同目录
        progress: 进度文字回调

    Returns:
        实际写出的 SRT 路径
    """
    if track.is_image_based:
        raise ValueError(
            f"「{track.codec_label}」是图形字幕，需要 OCR 才能转成文字，本功能不支持。"
        )

    ffmpeg = ffmpeg_path()
    if not ffmpeg:
        raise RuntimeError("找不到 ffmpeg，无法提取字幕")

    video = Path(video_path)
    if output_path:
        out = Path(output_path)
    else:
        out = unique_output_path(video.parent, video.stem, f"-{track.language_label}")

    out.parent.mkdir(parents=True, exist_ok=True)

    if progress:
        progress(f"正在提取字幕轨 {track.subtitle_ordinal + 1}…")

    # 先写临时文件，成功后再改名，避免失败时留下半个 SRT
    fd, temp_name = tempfile.mkstemp(suffix=".srt", dir=str(out.parent))
    os.close(fd)
    temp_path = Path(temp_name)

    try:
        result = _run_hidden([
            ffmpeg,
            "-v", "error",
            "-y",
            "-i", str(video),
            "-map", f"0:{track.index}",
            "-c:s", "srt",
            "-f", "srt",
            str(temp_path),
        ], timeout=600)

        if result.returncode != 0:
            detail = (result.stderr or "").strip()[:400]
            raise RuntimeError(f"提取失败：{detail or 'ffmpeg 返回错误'}")

        text = temp_path.read_text(encoding="utf-8", errors="replace")
        if not text.strip():
            raise RuntimeError("提取结果为空，这条字幕轨可能没有实际内容")

        # ffmpeg 有时会把 ASS 样式标签写进 SRT，这里清理掉
        cleaned = _strip_ass_tags(text)
        out.write_text(cleaned, encoding="utf-8")

        if progress:
            progress(f"已保存：{out.name}")
        logger.info("字幕已提取：%s -> %s", track.display_name(), out)
        return out

    finally:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass


def _strip_ass_tags(text: str) -> str:
    """去掉混进 SRT 的 ASS 样式标记，如 {\\an8}、{\\i1}。"""
    text = re.sub(r"\{\\[^}]*\}", "", text)
    # 有些转换结果带 <font ...> 标签
    text = re.sub(r"</?font[^>]*>", "", text, flags=re.IGNORECASE)
    # 清理因删除标签而留下的行首空白
    lines = [line.rstrip() for line in text.splitlines()]
    return "\n".join(lines).strip() + "\n"


def extract_all_text_tracks(
    video_path: str,
    progress: Optional[Callable[[str], None]] = None,
) -> tuple[List[Path], List[str]]:
    """提取所有可转换的文本字幕轨。

    Returns:
        (成功的文件列表, 跳过的说明列表)
    """
    tracks = list_subtitle_tracks(video_path)
    saved: List[Path] = []
    skipped: List[str] = []

    for track in tracks:
        if track.is_image_based:
            skipped.append(f"{track.display_name()}：图形字幕，不支持")
            continue
        try:
            saved.append(extract_track(video_path, track, progress=progress))
        except Exception as e:
            skipped.append(f"{track.display_name()}：{e}")

    return saved, skipped
