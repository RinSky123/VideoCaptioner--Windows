"""Bing 翻译器（免 API Key）

微软旧版 `edge.microsoft.com/translate/auth` 已返回 404 失效，
这里改用 Bing 网页翻译器自己使用的 `ttranslatev3` 接口：

1. GET https://cn.bing.com/translator 拿到会话 Cookie、IG、IID
   以及页面内嵌的 `params_AbusePreventionHelper`（key + token）。
2. POST https://cn.bing.com/ttranslatev3 （表单编码）批量翻译。
3. 令牌过期/失效时自动刷新一次再重试。

该接口在国内外网络环境下一般都可直接访问，不需要 API Key。
"""

import html
import re
import threading
from typing import Callable, List, Optional

import requests

from videocaptioner.core.entities import SubtitleProcessData
from videocaptioner.core.translate.base import BaseTranslator, logger
from videocaptioner.core.translate.types import TargetLanguage, get_language_code
from videocaptioner.core.utils.cache import generate_cache_key

_PAGE_URL = "https://cn.bing.com/translator"
_TRANSLATE_URL = "https://cn.bing.com/ttranslatev3"

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36 Edg/131.0.0.0"
)

# 单次请求最多携带的文本条数，避免请求体过大被拒
_MAX_BATCH = 10


class BingTranslator(BaseTranslator):
    """必应翻译（网页接口，免 API Key）"""

    def __init__(
        self,
        thread_num: int,
        batch_num: int,
        target_language: TargetLanguage,
        update_callback: Optional[Callable],
    ):
        super().__init__(
            thread_num=thread_num,
            batch_num=batch_num,
            target_language=target_language,
            update_callback=update_callback,
        )
        self.timeout = 20
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": _USER_AGENT})

        self._ig = ""
        self._iid = ""
        self._key = ""
        self._token = ""
        self._request_count = 0
        # 多个线程同时翻译，刷新令牌必须串行
        self._auth_lock = threading.Lock()

        self._init_session()

    # ------------------------------------------------------------------ 认证
    def _init_session(self) -> None:
        """获取 IG / IID / abuse-prevention key 与 token"""
        try:
            response = self.session.get(_PAGE_URL, timeout=self.timeout)
            response.raise_for_status()
            page = response.text
        except Exception as e:
            logger.error(f"Failed to load Bing translator page: {e}")
            raise RuntimeError(f"无法访问必应翻译页面：{e}")

        ig = re.search(r'IG:"([0-9A-Fa-f]+)"', page)
        iid = re.search(r'data-iid="([^"]+)"', page)
        abuse = re.search(r"params_AbusePreventionHelper\s*=\s*\[([^\]]+)\]", page)

        if not ig or not abuse:
            logger.error("Bing page layout changed; token not found")
            raise RuntimeError("必应翻译接口解析失败，页面结构可能已变化")

        values = [part.strip().strip('"') for part in abuse.group(1).split(",")]
        if len(values) < 2:
            raise RuntimeError("必应翻译令牌格式异常")

        self._ig = ig.group(1)
        self._iid = iid.group(1) if iid else "translator.5023"
        self._key = values[0]
        self._token = values[1]
        self._request_count = 0

    def _refresh_token(self) -> None:
        """串行刷新令牌，避免多线程重复请求"""
        with self._auth_lock:
            # 其它线程可能已经刷新过
            if self._request_count == 0:
                self._init_session()

    # ------------------------------------------------------------------ 翻译
    def _request(self, texts: List[str], target_lang: str) -> List[str]:
        """调用一次 ttranslatev3，返回等长的译文列表

        该接口每次只处理一个 text 字段（重复传 text 只认第一条），
        但会保留换行，所以用换行拼接多条，返回后再按行拆开。
        """
        joined = "\n".join(texts)
        params = {
            "isVertical": "1",
            "IG": self._ig,
            "IID": f"{self._iid}.{self._request_count}",
        }
        data = {
            "fromLang": "auto-detect",
            "to": target_lang,
            "token": self._token,
            "key": self._key,
            "text": joined,
        }

        response = self.session.post(
            _TRANSLATE_URL,
            params=params,
            data=data,
            headers={"Referer": _PAGE_URL},
            timeout=self.timeout,
        )
        response.raise_for_status()

        try:
            body = response.json()
        except ValueError:
            raise RuntimeError(f"必应返回了非 JSON 内容：{response.text[:200]}")

        if isinstance(body, dict):
            # {"statusCode": 205} 表示令牌失效
            raise RuntimeError(f"必应翻译失败（statusCode={body.get('statusCode')}）")

        if not body:
            return ["" for _ in texts]

        translations = body[0].get("translations") or []
        full = translations[0]["text"] if translations else ""

        lines = full.split("\n")
        if len(lines) == len(texts):
            return lines
        if len(texts) == 1:
            return [full]
        # 行数对不上（模型合并了句子），按原条数逐条重译
        logger.warning(
            f"Bing merged lines ({len(lines)} != {len(texts)}), retrying one by one"
        )
        if len(texts) > 1:
            return [self._request([t], target_lang)[0] for t in texts]
        return [full]

    def _translate_chunk(
        self, subtitle_chunk: List[SubtitleProcessData]
    ) -> List[SubtitleProcessData]:
        """翻译字幕块"""
        target_lang = get_language_code(self.target_language, "bing")

        pending = [d for d in subtitle_chunk if d.original_text.strip()]
        for start in range(0, len(pending), _MAX_BATCH):
            batch = pending[start : start + _MAX_BATCH]
            texts = [html.unescape(d.original_text[:5000]) for d in batch]

            for attempt in (1, 2):
                try:
                    with self._auth_lock:
                        self._request_count += 1
                    translated = self._request(texts, target_lang)
                    if len(translated) != len(batch):
                        raise RuntimeError(
                            f"必应返回条数不匹配：期望 {len(batch)}，实际 {len(translated)}"
                        )
                    for data, text in zip(batch, translated):
                        data.translated_text = text
                    break
                except Exception as e:
                    if attempt == 1:
                        logger.warning(f"Bing translation retry after: {e}")
                        with self._auth_lock:
                            self._request_count = 0
                        self._refresh_token()
                    else:
                        logger.error(f"Bing translation failed: {e}")
                        raise

        return subtitle_chunk

    def _get_cache_key(self, chunk: List[SubtitleProcessData]) -> str:
        """生成缓存键"""
        class_name = self.__class__.__name__
        chunk_key = generate_cache_key(chunk)
        lang = self.target_language.value
        return f"{class_name}:{chunk_key}:{lang}"
