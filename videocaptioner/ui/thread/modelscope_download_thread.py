import logging
import sys
import threading
import time
from typing import Callable

from modelscope.hub.callback import ProgressCallback
from modelscope.hub.snapshot_download import snapshot_download
from PyQt5.QtCore import QThread, pyqtSignal

# 进度信号最小间隔（毫秒），避免 GUI 线程被高频进度淹没
PROGRESS_INTERVAL_MS = 100

# 输出抑制最多保留的字符数；超出后丢弃旧内容，防止内存无限增长
_MAX_SINK_CHARS = 256 * 1024


class _BoundedSink:
    """有界输出缓冲，替代会无限增长的 StringIO。

    modelscope 在下载过程中会往 stdout/stderr 写入大量进度信息，
    旧实现用 StringIO 全量收集且直到下载结束才释放。
    """

    def __init__(self, limit: int = _MAX_SINK_CHARS):
        self._limit = limit
        self._buf: list[str] = []
        self._size = 0
        self._lock = threading.Lock()

    def write(self, text) -> int:
        if not isinstance(text, str):
            text = str(text)
        n = len(text)
        with self._lock:
            self._buf.append(text)
            self._size += n
            if self._size > self._limit:
                joined = "".join(self._buf)[-self._limit :]
                self._buf = [joined]
                self._size = len(joined)
        return n

    def flush(self):
        pass

    def isatty(self) -> bool:
        return False

    def writable(self) -> bool:
        return True


class SuppressOutput:
    """上下文管理器：抑制 stdout/stderr 和 modelscope 日志

    注意：替换的是进程级全局对象。多个下载线程同时进来时，
    只让第一个线程负责替换与还原，避免互相覆盖后把 sys.stdout 变成 None。
    """

    _global_lock = threading.RLock()
    _depth = 0
    _saved_stdout = None
    _saved_stderr = None

    def __enter__(self):
        self._saved_logger_levels: dict[str, int] = {}
        for name in ["modelscope", "tqdm"]:
            log = logging.getLogger(name)
            self._saved_logger_levels[name] = log.level
            log.setLevel(logging.CRITICAL)

        with SuppressOutput._global_lock:
            if SuppressOutput._depth == 0:
                SuppressOutput._saved_stdout = sys.stdout
                SuppressOutput._saved_stderr = sys.stderr
                sys.stdout = _BoundedSink()  # type: ignore[assignment]
                sys.stderr = _BoundedSink()  # type: ignore[assignment]
            SuppressOutput._depth += 1
        return self

    def __exit__(self, *args):
        with SuppressOutput._global_lock:
            SuppressOutput._depth -= 1
            if SuppressOutput._depth <= 0:
                SuppressOutput._depth = 0
                # 始终还原到进入前的对象，绝不留 None
                sys.stdout = SuppressOutput._saved_stdout or sys.__stdout__  # type: ignore[assignment]
                sys.stderr = SuppressOutput._saved_stderr or sys.__stderr__  # type: ignore[assignment]
                SuppressOutput._saved_stdout = None
                SuppressOutput._saved_stderr = None

        for name, level in self._saved_logger_levels.items():
            logging.getLogger(name).setLevel(level)


def create_progress_callback_class(
    progress_callback: Callable[[int, str], None],
) -> type[ProgressCallback]:
    """创建一个自定义的 ProgressCallback 类，用于接收下载进度"""

    class CustomProgressCallback(ProgressCallback):
        def __init__(self, filename: str, file_size: int):
            super().__init__(filename, file_size)
            self.downloaded = 0
            self._last_sent = 0
            self._last_at = 0.0

        def update(self, size: int):
            self.downloaded += size
            if self.file_size <= 0:
                return

            percentage = min(int(self.downloaded * 100 / self.file_size), 99)
            now = time.monotonic() * 1000

            # 百分比没变、且距上次发送不足间隔时跳过
            if (
                percentage == self._last_sent
                and now - self._last_at < PROGRESS_INTERVAL_MS
            ):
                return
            if now - self._last_at < PROGRESS_INTERVAL_MS and percentage < 100:
                return

            self._last_sent = percentage
            self._last_at = now
            progress_callback(percentage, f"{self.filename}: {percentage}%")

        def end(self):
            pass

    return CustomProgressCallback


class ModelscopeDownloadThread(QThread):
    progress = pyqtSignal(int, str)
    error = pyqtSignal(str)

    def __init__(self, model_id: str, save_path: str):
        super().__init__()
        self.model_id = model_id
        self.save_path = save_path

    def run(self):
        try:
            self.progress.emit(0, self.tr("开始下载..."))

            callback_class = create_progress_callback_class(self.progress.emit)

            with SuppressOutput():
                snapshot_download(
                    self.model_id,
                    local_dir=self.save_path,
                    progress_callbacks=[callback_class],
                )

            self.progress.emit(100, self.tr("下载完成"))

        except Exception as e:
            self.error.emit(str(e))


if __name__ == "__main__":
    import sys

    from PyQt5.QtCore import QCoreApplication

    app = QCoreApplication(sys.argv)
    model_id = "pengzhendong/faster-whisper-tiny"
    save_path = r"models/faster-whisper-tiny"
    downloader = ModelscopeDownloadThread(model_id, save_path)

    def on_progress(percentage, message):
        print(f"进度: {message}")

    def on_error(error_msg):
        print(f"错误: {error_msg}")
        app.quit()

    def on_finished():
        print("下载完成！")
        app.quit()

    downloader.progress.connect(on_progress)
    downloader.error.connect(on_error)
    downloader.finished.connect(on_finished)

    print(f"开始下载模型 {model_id}")
    downloader.start()

    sys.exit(app.exec_())
