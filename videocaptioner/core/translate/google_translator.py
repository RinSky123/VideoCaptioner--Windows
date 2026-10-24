"""Google 翻译器（免 API Key）

原来的两种接法都已失效：
- https://translate.google.com/m            → 现在返回 429
- https://translate.googleapis.com/...      → 现在返回 429

改用 Chrome 词典扩展所用的 `clients5.google.com/translate_a/t` 接口，
它返回简洁的 JSON，单次可用换行分隔多段文本。该站点在部分网络环境下
需要代理才能访问，失败时退回旧的页面抓取方式。
"""

import html
import re
from typing import Callable, List, Optional

import requests

from videocaptioner.core.entities import SubtitleProcessData
from videocaptioner.core.translate.base import BaseTranslator, logger
from videocaptioner.core.translate.types import TargetLanguage, get_language_code
from videocaptioner.core.utils.cache import generate_cache_key

_CLIENTS5_URL = "https://clients5.google.com/translate_a/t"
_LEGACY_URL = "https://translate.google.com/m"

_UA_CHROME = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
_UA_LEGACY = (
    "Mozilla/4.0 (compatible;MSIE 6.0;Windows NT 5.1;SV1;.NET CLR 1.1.4322;"
    ".NET CLR 2.0.50727;.NET CLR 3.0.04506.30)"
)

# 该接口单次可用换行分隔多段文本
_MAX_BATCH = 10


class GoogleTranslator(BaseTranslator):
    """谷歌翻译（免 API Key）"""

    def __init__(
        self,
        thread_num: int,
        batch_num: int,
        target_language: TargetLanguage,
        timeout: int,
        update_callback: Optional[Callable],
    ):
        super().__init__(
            thread_num=thread_num,
            batch_num=batch_num,
            target_language=target_language,
            update_callback=update_callback,
        )
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": _UA_CHROME})

    # ------------------------------------------------------------------ 翻译
    def _request_clients5(self, texts: List[str], target_lang: str) -> List[str]:
        """通过 clients5 接口批量翻译

        该接口支持重复传 q，每段文本对应一条结果；用换行拼接只会
        得到一整段译文，因此这里必须用重复参数。
        """
        params = [("client", "dict-chrome-ex"), ("sl", "auto"), ("tl", target_lang)]
        params += [("q", text) for text in texts]

        response = self.session.get(
            _CLIENTS5_URL, params=params, timeout=self.timeout
        )
        response.raise_for_status()

        try:
            body = response.json()
        except ValueError:
            raise RuntimeError(f"谷歌返回了非 JSON 内容：{response.text[:200]}")

        # 每段文本返回一项：[["译文1", "源语言"], ["译文2", "源语言"], ...]
        if isinstance(body, list) and body and isinstance(body[0], list):
            flat = [item[0] for item in body if item]
        elif isinstance(body, list):
            flat = [item for item in body if isinstance(item, str)]
        else:
            raise RuntimeError(f"谷歌返回格式无法识别：{str(body)[:200]}")

        return flat

    def _request_legacy(self, text: str, target_lang: str) -> str:
        """旧的页面抓取方式，作为备用"""
        response = requests.get(
            _LEGACY_URL,
            params={"tl": target_lang, "sl": "auto", "q": text},
            headers={"User-Agent": _UA_LEGACY},
            timeout=self.timeout,
        )
        response.raise_for_status()

        match = re.findall(
            r'(?s)class="(?:t0|result-container)">(.*?)<', response.text
        )
        if not match:
            raise RuntimeError("无法从谷歌返回页面提取译文")
        return html.unescape(match[0])

    def _translate_chunk(
        self, subtitle_chunk: List[SubtitleProcessData]
    ) -> List[SubtitleProcessData]:
        """翻译字幕块"""
        target_lang = get_language_code(self.target_language, "google")

        pending = [d for d in subtitle_chunk if d.original_text.strip()]

        for start in range(0, len(pending), _MAX_BATCH):
            batch = pending[start : start + _MAX_BATCH]
            texts = [html.unescape(d.original_text[:2000]) for d in batch]

            try:
                translated = self._request_clients5(texts, target_lang)
                if len(translated) == len(batch):
                    for data, text in zip(batch, translated):
                        data.translated_text = text
                    continue
                logger.warning(
                    f"Google clients5 returned {len(translated)} results "
                    f"for {len(batch)} inputs, falling back"
                )
            except Exception as e:
                logger.warning(f"Google clients5 failed, trying legacy: {e}")

            # 回退：逐条走旧接口
            for data, text in zip(batch, texts):
                try:
                    data.translated_text = self._request_legacy(text, target_lang)
                except Exception as e:
                    logger.error(f"Google translation failed {data.index}: {e}")

        return subtitle_chunk

    def _get_cache_key(self, chunk: List[SubtitleProcessData]) -> str:
        """生成缓存键"""
        class_name = self.__class__.__name__
        chunk_key = generate_cache_key(chunk)
        lang = self.target_language.value
        return f"{class_name}:{chunk_key}:{lang}"
