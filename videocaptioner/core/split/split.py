import atexit
import difflib
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Union

from videocaptioner.core.asr.asr_data import ASRData, ASRDataSeg
from videocaptioner.core.entities import SubtitleSplitTypeEnum
from videocaptioner.core.split.split_by_llm import split_by_llm
from videocaptioner.core.utils.logger import setup_logger
from videocaptioner.core.utils.text_utils import (
    count_words,
    is_mainly_cjk,
    is_pure_punctuation,
    is_space_separated_language,
)

logger = setup_logger("subtitle_splitter")

# ==================== 配置常量 ====================

# 字数限制
MAX_WORD_COUNT_CJK = 25  # CJK文本单行最大字数
MAX_WORD_COUNT_ENGLISH = 18  # 英文文本单行最大单词数

# Segments阈值
SEGMENT_WORD_THRESHOLD = 500  # 长文本Segments阈值(字数)

# 时间间隔
MAX_GAP = 1500  # 允许的最大时间间隔(毫秒)
MERGE_SHORT_GAP = 200  # 短Segments合并时间阈值(毫秒)
MERGE_VERY_SHORT_GAP = 500  # 极短Segments合并时间阈值(毫秒)

# 短Segments合并阈值
MERGE_MIN_WORDS = 5  # 短Segments最小字数阈值
MERGE_VERY_SHORT_WORDS = 3  # 极短Segments字数阈值

# 分割相关
SPLIT_SEARCH_RANGE = 30  # 分割点前后搜索范围
TIME_GAP_WINDOW_SIZE = 5  # 时间间隔窗口大小
TIME_GAP_MULTIPLIER = 3  # 大间隔判断倍数
MIN_GROUP_SIZE = 5  # 最小分组大小

# 规则分割
RULE_SPLIT_GAP = 500  # 规则分割时间间隔阈值(毫秒)
RULE_MIN_SEGMENT_SIZE = 4  # 规则分割最小Segments大小

# 断句停顿阈值：说完一句话后停顿 >= 这个时长，就另起一条字幕。
# 不管是一个人还是两个人在说 —— 停顿本身就是天然的句子边界。
# 这是"几句话被并进同一句"的直接原因：以前无论间隔多大，
# 只要合起来没超过字数上限就会被拼成一条。
PAUSE_SPLIT_MS = 1000

# 常见词分割
PREFIX_WORD_RATIO = 0.6  # 前缀词分割比例
SUFFIX_WORD_RATIO = 0.4  # 后缀词分割比例

# 匹配相关
MATCH_SIMILARITY_THRESHOLD = 0.5  # 文本匹配相似度阈值
MATCH_MAX_SHIFT = 30  # 匹配滑动窗口最大偏移
MATCH_MAX_UNMATCHED = 5  # 允许的最大未匹配句子数
MATCH_LARGE_SHIFT = 100  # 未匹配时的大偏移量


def preprocess_segments(
    segments: List[ASRDataSeg], need_lower: bool = True
) -> List[ASRDataSeg]:
    """预处理ASRSegments

    1. 移除纯标点符号的Segments
    2. 为需要空格分隔的语言添加空格（英语、俄语、阿拉伯语等，不包括CJK）

    Args:
        segments: ASR数据Segments列表
        need_lower: 是否转小写（仅对拉丁和西里尔字母有效）

    Returns:
        处理后的Segments列表
    """
    new_segments = []
    for seg in segments:
        if not is_pure_punctuation(seg.text):
            text = seg.text.strip()
            # 检查是否为需要空格分隔的语言（不包括CJK）
            if is_space_separated_language(text):
                if need_lower:
                    text = text.lower()
                seg.text = text + " "
            new_segments.append(seg)
    return new_segments


class SubtitleSplitter:
    """字幕智能分割器

    使用LLM进行语义Segments,支持缓存、并发处理和规则降级。
    """

    def __init__(
        self,
        thread_num,
        model,
        max_word_count_cjk: int = MAX_WORD_COUNT_CJK,
        max_word_count_english: int = MAX_WORD_COUNT_ENGLISH,
        rules_only: bool = False,
        split_type=SubtitleSplitTypeEnum.SENTENCE,
        speaker_gap_ms: int = PAUSE_SPLIT_MS,
    ):
        """初始化分割器

        Args:
            thread_num: 并发线程数
            model: LLM模型名称
            max_word_count_cjk: CJK最大字数
            max_word_count_english: 英文最大单词数
            rules_only: 只用规则断句，不调用大模型
            split_type: 句子分段 / 语义分段
            speaker_gap_ms: 断句停顿阈值（毫秒）。停顿达到这个时长就另起
                一条字幕，不论是一个人在停顿换气，还是两个人在对话。
        """
        self.thread_num = thread_num
        self.model = model
        self.max_word_count_cjk = max_word_count_cjk
        self.max_word_count_english = max_word_count_english
        # 无 API 场景（公益模型）只用规则断句，不发起 LLM 请求
        self.rules_only = rules_only
        self.split_type = split_type or SubtitleSplitTypeEnum.SENTENCE
        # 停顿达到这个时长就一定断开，不管字数够不够、是不是同一个人
        self.speaker_gap_ms = max(int(speaker_gap_ms or PAUSE_SPLIT_MS), 1)
        self.is_running = True
        self._init_thread_pool()

    def _init_thread_pool(self):
        """初始化线程池并注册清理"""
        self.executor = ThreadPoolExecutor(max_workers=self.thread_num)
        atexit.register(self.stop)

    def split_subtitle(self, subtitle_data: Union[str, ASRData]) -> ASRData:
        """分割字幕(主入口)

        处理流程:
        1. Reading并预处理字幕
        2. 按字数Segments
        3. 并发调用LLM处理
        4. 合并结果并优化

        Args:
            subtitle_data: 字幕文件路径或ASRData对象

        Returns:
            分割后的ASRData对象

        Raises:
            RuntimeError: Raised on split failure
        """
        try:
            # 1. Reading字幕
            if isinstance(subtitle_data, str):
                asr_data = ASRData.from_subtitle_file(subtitle_data)
            else:
                asr_data = subtitle_data

            if not asr_data.is_word_timestamp():
                asr_data = asr_data.split_to_word_segments()

            # 2. 预处理
            asr_data.segments = preprocess_segments(asr_data.segments, need_lower=False)
            txt = asr_data.to_txt().replace("\n", "")

            # 3. 确定Segments数并分割
            total_word_count = count_words(txt)
            num_segments = self._determine_num_segments(total_word_count)
            logger.debug(f"Based on word count {total_word_count},determined segment count: {num_segments}")

            asr_data_list = self._split_asr_data(asr_data, num_segments)

            # 4. 并发处理
            processed_segments = self._process_segments(asr_data_list)

            # 5. 合并并优化
            final_segments = self._merge_processed_segments(processed_segments)

            return ASRData(final_segments)

        except Exception as e:
            logger.error(f"Split failed:{str(e)}")
            raise RuntimeError(f"Split failed:{str(e)}")

    def _determine_num_segments(
        self, word_count: int, threshold: int = SEGMENT_WORD_THRESHOLD
    ) -> int:
        """Based on word count确定Segments数

        Args:
            word_count: 总字数
            threshold: 每段目标字数

        Returns:
            Segments数(最小为1)
        """
        num_segments = word_count // threshold
        if word_count % threshold > 0:
            num_segments += 1
        return max(1, num_segments)

    def _split_asr_data(self, asr_data: ASRData, num_segments: int) -> List[ASRData]:
        """按时间间隔智能分割长文本

        策略:
        1. 计算平均分割点
        2. 在分割点附近寻找最大时间间隔
        3. 在间隔处切分以保证语义完整

        Args:
            asr_data: ASR数据对象
            num_segments: 目标Segments数

        Returns:
            分割后的ASRData列表
        """
        total_segs = len(asr_data.segments)
        total_word_count = count_words(asr_data.to_txt())
        words_per_segment = total_word_count // num_segments

        if num_segments <= 1 or total_segs <= num_segments:
            return [asr_data]

        # 计算初始分割点
        split_indices = [i * words_per_segment for i in range(1, num_segments)]

        # 调整分割点:在附近寻找最大时间间隔
        adjusted_split_indices = []
        for split_point in split_indices:
            start = max(0, split_point - SPLIT_SEARCH_RANGE)
            end = min(total_segs - 1, split_point + SPLIT_SEARCH_RANGE)

            # 寻找最大间隔点
            max_gap = -1
            best_index = split_point

            for j in range(start, end):
                gap = (
                    asr_data.segments[j + 1].start_time - asr_data.segments[j].end_time
                )
                if gap > max_gap:
                    max_gap = gap
                    best_index = j

            adjusted_split_indices.append(best_index)

        # 去重并排序
        adjusted_split_indices = sorted(list(set(adjusted_split_indices)))

        # 执行分割
        segments = []
        prev_index = 0
        for index in adjusted_split_indices:
            part = ASRData(asr_data.segments[prev_index : index + 1])
            segments.append(part)
            prev_index = index + 1

        if prev_index < total_segs:
            part = ASRData(asr_data.segments[prev_index:])
            segments.append(part)

        return segments

    def _process_segments(self, asr_data_list: List[ASRData]) -> List[List[ASRDataSeg]]:
        """并发处理AllSegments"""
        futures = []
        for asr_data in asr_data_list:
            if not self.executor:
                raise ValueError("Thread pool not initialized")
            future = self.executor.submit(self._process_single_segment, asr_data)
            futures.append(future)

        processed_segments = []
        for future in as_completed(futures):
            if not self.is_running:
                break
            try:
                result = future.result()
                processed_segments.append(result)
            except Exception as e:
                logger.error(f"Segment processing failed:{str(e)}")

        return processed_segments

    def _process_single_segment(self, asr_data_part: ASRData) -> List[ASRDataSeg]:
        """处理单个Segments(带重试和降级)"""
        if not asr_data_part.segments:
            return []
        if self.rules_only:
            return self._process_by_rules(asr_data_part.segments)
        try:
            return self._process_by_llm(asr_data_part.segments)
        except Exception as e:
            logger.warning(f"LLM processing failed, falling back to rules: {str(e)}")
            return self._process_by_rules(asr_data_part.segments)

    def _process_by_llm(self, segments: List[ASRDataSeg]) -> List[ASRDataSeg]:
        """使用LLM进行智能Segments

        Args:
            segments: ASRSegments列表

        Returns:
            处理后的Segments列表
        """
        txt = "".join([seg.text for seg in segments])
        logger.debug(f"Calling API for segmentation,text length: {count_words(txt)}")

        sentences = split_by_llm(
            text=txt,
            model=self.model,
            max_word_count_cjk=self.max_word_count_cjk,
            max_word_count_english=self.max_word_count_english,
            prompt_path=self.split_type.prompt_path(),
        )

        return self._merge_segments_based_on_sentences(segments, sentences)

    def _process_by_rules(self, segments: List[ASRDataSeg]) -> List[ASRDataSeg]:
        """使用规则进行基础分割(LLM降级方案)

        规则:
        1. Grouped by time gaps
        2. 按常见词分割长句
        3. 拆分超长Segments

        Args:
            segments: ASRSegments列表

        Returns:
            处理后的Segments列表
        """
        logger.debug(f"Segments: {len(segments)}")

        # 1. 先按停顿切块：间隔达到阈值就是两句话（同一个人换气也算）。
        #    默认 1 秒，比原 RULE_SPLIT_GAP（500ms）更贴近"句子边界"的直觉。
        segment_groups = self._group_by_time_gaps(
            segments, max_gap=self.speaker_gap_ms, check_large_gaps=False
        )
        logger.debug(f"Grouped by time gaps: {len(segment_groups)}")

        # 2. 按常见词分割长句
        common_result_groups = []
        for group in segment_groups:
            max_word_count = (
                self.max_word_count_cjk
                if is_mainly_cjk("".join(seg.text for seg in group))
                else self.max_word_count_english
            )
            if count_words("".join(seg.text for seg in group)) > max_word_count:
                split_groups = self._split_by_common_words(group)
                common_result_groups.extend(split_groups)
            else:
                common_result_groups.append(group)

        # 3. 合并成字幕；这里不会再跨越停顿，因为输入已经是切好的块
        result_segments = []
        for group in common_result_groups:
            result_segments.extend(self._merge_block_by_length(group))

        return result_segments

    def _group_by_time_gaps(
        self,
        segments: List[ASRDataSeg],
        max_gap: int = MAX_GAP,
        check_large_gaps: bool = False,
    ) -> List[List[ASRDataSeg]]:
        """Grouped by time gaps

        Args:
            segments: Segments列表
            max_gap: 断句阈值(ms)。间隔 **达到** 这个值就断开（含等于），
                而不是"超过"，因为 1 秒这种边界值本身就该算作停顿。
            check_large_gaps: 是否检查异常大间隔

        Returns:
            分组后的列表
        """
        if not segments:
            return []

        result = []
        current_group = [segments[0]]
        recent_gaps = []

        for i in range(1, len(segments)):
            time_gap = segments[i].start_time - segments[i - 1].end_time

            # 检查异常大间隔
            if check_large_gaps:
                recent_gaps.append(time_gap)
                if len(recent_gaps) > TIME_GAP_WINDOW_SIZE:
                    recent_gaps.pop(0)
                if len(recent_gaps) == TIME_GAP_WINDOW_SIZE:
                    avg_gap = sum(recent_gaps) / len(recent_gaps)
                    if (
                        time_gap > avg_gap * TIME_GAP_MULTIPLIER
                        and len(current_group) > MIN_GROUP_SIZE
                    ):
                        result.append(current_group)
                        current_group = []
                        recent_gaps = []

            # 间隔达到阈值（含等于）就分组
            if time_gap >= max_gap:
                if current_group:
                    result.append(current_group)
                current_group = []
                recent_gaps = []

            current_group.append(segments[i])

        if current_group:
            result.append(current_group)

        # 防御：任何情况下都不要把空组交给下游（会生成空字幕）
        return [group for group in result if group]

    def _split_by_common_words(
        self, segments: List[ASRDataSeg]
    ) -> List[List[ASRDataSeg]]:
        """在常见连接词处分割

        Args:
            segments: ASRSegments列表

        Returns:
            分割后的分组列表
        """
        # 前缀分割词(在这些词前面分割)
        prefix_split_words = {
            # 英文
            "and",
            "or",
            "but",
            "if",
            "then",
            "because",
            "as",
            "until",
            "while",
            "what",
            "when",
            "where",
            "nor",
            "yet",
            "so",
            "for",
            "however",
            "moreover",
            # 中文
            "和",
            "及",
            "与",
            "但",
            "而",
            "或",
            "因",
            "我",
            "你",
            "他",
            "她",
            "它",
            "咱",
            "您",
            "这",
            "那",
            "哪",
        }

        # 后缀分割词(在这些词后面分割)
        suffix_split_words = {
            # 标点
            ".",
            ",",
            "!",
            "?",
            "。",
            "，",
            "！",
            "？",
            # 中文语气词
            "的",
            "了",
            "着",
            "过",
            "吗",
            "呢",
            "吧",
            "啊",
            "呀",
            "嘛",
            "啦",
            # 英文代词
            "mine",
            "yours",
            "hers",
            "its",
            "ours",
            "theirs",
            "either",
            "neither",
        }

        result = []
        current_group = []

        for i, seg in enumerate(segments):
            max_word_count = (
                self.max_word_count_cjk
                if is_mainly_cjk(seg.text)
                else self.max_word_count_english
            )

            # 前缀词分割
            if any(
                seg.text.lower().startswith(word) for word in prefix_split_words
            ) and len(current_group) >= int(max_word_count * PREFIX_WORD_RATIO):
                result.append(current_group)
                logger.debug(f"Split before prefix word {seg.text} ")
                current_group = []

            # 后缀词分割
            if (
                i > 0
                and any(
                    segments[i - 1].text.lower().endswith(word)
                    for word in suffix_split_words
                )
                and len(current_group) >= int(max_word_count * SUFFIX_WORD_RATIO)
            ):
                result.append(current_group)
                logger.debug(f"Split after suffix word {segments[i - 1].text} ")
                current_group = []

            current_group.append(seg)

        if current_group:
            result.append(current_group)

        return result

    def _split_long_segment(self, segments: List[ASRDataSeg]) -> List[ASRDataSeg]:
        """把一组词级片段合成一条条字幕。

        顺序很重要：
            1. 先按停顿切块 —— 停顿达到阈值（默认 1 秒）就是两句话，
               不论是同一个人换气还是两个人对话，都不许并进同一条；
            2. 每块再按字数上限继续拆；
            3. 每块合并成一条字幕，时间取该块的首尾。

        Args:
            segments: Segments列表

        Returns:
            拆分后的Segments列表
        """
        if not segments:
            return []

        # 1. 先按停顿切块（说话人切换处必须断开）
        blocks = self._group_by_time_gaps(
            segments, max_gap=self.speaker_gap_ms, check_large_gaps=False
        )

        result_segs = []
        for block in blocks:
            result_segs.extend(self._merge_block_by_length(block))

        result_segs.sort(key=lambda seg: seg.start_time)
        return result_segs

    def _merge_block_by_length(self, segments: List[ASRDataSeg]) -> List[ASRDataSeg]:
        """把一块内部连续的词合并成不超过字数上限的字幕。

        只在"同一段连续语音"内部合并，不会跨越停顿。
        """
        if not segments:
            return []

        merged_text = "".join(seg.text for seg in segments)
        max_word_count = (
            self.max_word_count_cjk
            if is_mainly_cjk(merged_text)
            else self.max_word_count_english
        )

        # 够短，整块就是一条
        if count_words(merged_text) <= max_word_count or len(segments) < 2:
            return [
                ASRDataSeg(
                    merged_text.strip(),
                    segments[0].start_time,
                    segments[-1].end_time,
                )
            ]

        # 太长：在最靠近中段、且停顿最大的位置切开
        n = len(segments)
        gaps = [
            segments[i + 1].start_time - segments[i].end_time for i in range(n - 1)
        ]
        all_equal = all(abs(gap - gaps[0]) < 1e-6 for gap in gaps)

        if all_equal:
            split_index = n // 2
        else:
            start_idx = max(n // 6, 1)
            end_idx = min((5 * n) // 6, n - 2)
            split_index = max(
                range(start_idx, max(end_idx, start_idx + 1)),
                key=lambda i: gaps[i],
                default=n // 2,
            )
            if split_index <= 0 or split_index >= n - 1:
                split_index = n // 2

        first = self._merge_block_by_length(segments[: split_index + 1])
        second = self._merge_block_by_length(segments[split_index + 1 :])
        return first + second

    def _merge_processed_segments(
        self, processed_segments: List[List[ASRDataSeg]]
    ) -> List[ASRDataSeg]:
        """合并All处理后的Segments并排序"""
        final_segments = []
        for segments in processed_segments:
            final_segments.extend(segments)

        final_segments.sort(key=lambda seg: seg.start_time)
        return final_segments

    def merge_short_segment(self, segments: List[ASRDataSeg]) -> None:
        """deprecated
        合并短Segments优化

        合并条件:
        1. 时间间隔小 + 字数少
        2. 合并后不超过最大字数限制

        Args:
            segments: Segments列表(原地修改)
        """
        if not segments:
            return

        i = 0
        while i < len(segments) - 1:
            current_seg = segments[i]
            next_seg = segments[i + 1]

            time_gap = abs(next_seg.start_time - current_seg.end_time)
            current_words = count_words(current_seg.text)
            next_words = count_words(next_seg.text)
            total_words = current_words + next_words
            max_word_count = (
                self.max_word_count_cjk
                if is_mainly_cjk(current_seg.text)
                else self.max_word_count_english
            )

            # 判断是否合并
            should_merge = (
                time_gap < MERGE_SHORT_GAP
                and (current_words < MERGE_MIN_WORDS or next_words < MERGE_MIN_WORDS)
                and total_words <= max_word_count
            ) or (
                time_gap < MERGE_VERY_SHORT_GAP
                and (
                    current_words < MERGE_VERY_SHORT_WORDS
                    or next_words < MERGE_VERY_SHORT_WORDS
                )
                and total_words <= max_word_count
            )

            if should_merge:
                logger.debug(
                    f"合并短Segments: {current_seg.text} + {next_seg.text} (间隔:{time_gap}ms)"
                )

                # 合并文本
                if is_mainly_cjk(current_seg.text):
                    current_seg.text += next_seg.text
                else:
                    current_seg.text += " " + next_seg.text
                current_seg.end_time = next_seg.end_time

                segments.pop(i + 1)
            else:
                i += 1

    def _merge_segments_based_on_sentences(
        self,
        segments: List[ASRDataSeg],
        sentences: List[str],
        max_unmatched: int = MATCH_MAX_UNMATCHED,
    ) -> List[ASRDataSeg]:
        """基于LLM返回的句子列表合并ASRSegments

        使用滑动窗口匹配算法:
        1. 对每个LLM句子,寻找最佳匹配的ASRSegments序列
        2. 使用相似度算法进行匹配
        3. 合并匹配的Segments

        Args:
            segments: ASRSegments列表
            sentences: LLM返回的句子列表
            max_unmatched: 允许的最大未匹配句子数

        Returns:
            合并后的Segments列表

        Raises:
            ValueError: Unmatched sentences exceeded threshold时
        """

        def preprocess_text(s: str) -> str:
            """文本标准化:小写+空格规范化"""
            return " ".join(s.lower().split())

        asr_texts = [seg.text for seg in segments]
        asr_len = len(asr_texts)
        asr_index = 0
        threshold = MATCH_SIMILARITY_THRESHOLD
        max_shift = MATCH_MAX_SHIFT
        unmatched_count = 0

        new_segments = []

        for sentence in sentences:
            logger.debug("==========")
            logger.debug(f"Processing sentence: {sentence}")
            logger.debug("Next sentences: :" + "".join(asr_texts[asr_index : asr_index + 10]))

            sentence_proc = preprocess_text(sentence)
            word_count = count_words(sentence_proc)
            best_ratio = 0.0
            best_pos = None
            best_window_size = 0

            # 滑动窗口大小
            max_window_size = min(word_count * 2, asr_len - asr_index)
            min_window_size = max(1, word_count // 2)
            window_sizes = sorted(
                range(min_window_size, max_window_size + 1),
                key=lambda x: abs(x - word_count),
            )

            # 滑动窗口匹配
            for window_size in window_sizes:
                max_start = min(asr_index + max_shift + 1, asr_len - window_size + 1)
                for start in range(asr_index, max_start):
                    substr = "".join(asr_texts[start : start + window_size])
                    substr_proc = preprocess_text(substr)
                    ratio = difflib.SequenceMatcher(
                        None, sentence_proc, substr_proc
                    ).ratio()

                    if ratio > best_ratio:
                        best_ratio = ratio
                        best_pos = start
                        best_window_size = window_size
                    if ratio == 1.0:
                        break
                if best_ratio == 1.0:
                    break

            # 处理匹配结果
            if best_ratio >= threshold and best_pos is not None:
                start_seg_index = best_pos
                end_seg_index = best_pos + best_window_size - 1

                segs_to_merge = segments[start_seg_index : end_seg_index + 1]

                # 交给 _split_long_segment：它会先按停顿切块（两人对话必须分开），
                # 再按字数上限细分。以前这里先按 MAX_GAP(=1.5s) 分组，
                # 0.4~1.5 秒的换人间隔会被吞掉，两个人的话就并成了一条。
                new_segments.extend(self._split_long_segment(segs_to_merge))

                max_shift = MATCH_MAX_SHIFT
                asr_index = end_seg_index + 1
            else:
                logger.warning(f"Cannot match sentence: {sentence}")
                unmatched_count += 1
                if unmatched_count > max_unmatched:
                    raise ValueError(f"Unmatched sentences exceeded threshold {max_unmatched},processing aborted")
                max_shift = MATCH_LARGE_SHIFT
                asr_index = min(asr_index + 1, asr_len - 1)

        return new_segments

    def stop(self):
        """停止分割器并清理资源"""
        if not self.is_running:
            return
        self.is_running = False
        if hasattr(self, "executor") and self.executor is not None:
            try:
                self.executor.shutdown(wait=False, cancel_futures=True)
            except Exception as e:
                logger.error(f"Error closing thread pool:{str(e)}")
            finally:
                self.executor = None
