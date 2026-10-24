import os
import shutil
import subprocess
import time
from abc import ABC, abstractmethod
from pathlib import Path

import requests
from PyQt5.QtCore import QThread, pyqtSignal

from videocaptioner.config import CACHE_PATH
from videocaptioner.core.utils.logger import setup_logger
from videocaptioner.core.utils.platform_utils import get_subprocess_kwargs

logger = setup_logger("download_thread")

# 进度信号的最小间隔（毫秒）。
# 每 8KB 回调一次时，1.4GB 会产生约 18 万次跨线程信号，
# 全部积压到 GUI 线程处理；窗口在后台时绘制受限，这里更明显。
PROGRESS_INTERVAL_MS = 100

# 断点续传时最多尝试的次数（网络抖动时自动续传，不用从头下）
MAX_RESUME_ATTEMPTS = 6

# 单次连接读取超时（秒）。只限制"多久没收到数据"，不限制总时长。
READ_TIMEOUT = 30


class DownloadIncompleteError(Exception):
    """下载的文件大小与服务器声明不一致（通常是网络中断）。"""


class _Cancelled(Exception):
    """用户主动取消。"""


class BaseDownloader(ABC):
    """下载器基类"""

    def __init__(self, url: str, save_path: Path, progress_callback):
        self.url = url
        self.save_path = save_path
        self.progress_callback = progress_callback
        self._cancelled = False

    @abstractmethod
    def download(self) -> bool:
        """执行下载，返回是否成功"""
        pass

    def cancel(self):
        """取消下载"""
        self._cancelled = True


class Aria2Downloader(BaseDownloader):
    """aria2c 多线程下载器"""

    def __init__(self, url: str, save_path: Path, progress_callback):
        super().__init__(url, save_path, progress_callback)
        self.process = None

    @staticmethod
    def is_available() -> bool:
        """检查 aria2c 是否可用"""
        return shutil.which("aria2c") is not None

    def download(self) -> bool:
        temp_dir = CACHE_PATH / "download_cache"
        temp_dir.mkdir(parents=True, exist_ok=True)
        temp_file = temp_dir / self.save_path.name

        cmd = [
            "aria2c",
            "--no-conf",
            "--show-console-readout=false",
            "--summary-interval=1",
            "--max-connection-per-server=2",
            "--split=2",
            "--connect-timeout=10",
            "--timeout=10",
            "--max-tries=2",
            "--retry-wait=1",
            "--continue=true",
            "--auto-file-renaming=false",
            "--allow-overwrite=true",
            "--check-certificate=false",
            f"--dir={temp_dir}",
            f"--out={temp_file.name}",
            self.url,
        ]

        subprocess_args = {
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "universal_newlines": True,
            "encoding": "utf-8",
            **get_subprocess_kwargs(),
        }

        logger.info(f"使用 aria2c 下载: {self.url}")
        self.process = subprocess.Popen(cmd, **subprocess_args)

        while True:
            if self._cancelled:
                self.process.terminate()
                return False

            if self.process.poll() is not None:
                break

            line = self.process.stdout.readline()
            self._parse_progress(line)

        if self.process.returncode == 0:
            self.save_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(temp_file), self.save_path)
            return True
        else:
            error = self.process.stderr.read()
            self._last_error = (error or "").strip()[:500]
            logger.error(f"aria2c 下载失败: {error}")
            return False

    def _parse_progress(self, line: str):
        """解析 aria2c 输出格式: [#40ca1b 2.4MiB/74MiB(3%) CN:2 DL:3.9MiB ETA:18s]"""
        if "[#" not in line or "]" not in line:
            return

        try:
            progress_part = line.split("(")[1].split(")")[0]
            percent = float(progress_part.strip("%"))

            speed = "0"
            eta = ""
            if "DL:" in line:
                speed = line.split("DL:")[1].split()[0]
            if "ETA:" in line:
                eta = line.split("ETA:")[1].split("]")[0]

            status = f"速度: {speed}/s, 剩余: {eta}"
            self.progress_callback(percent, status)
        except Exception:
            pass

    def cancel(self):
        super().cancel()
        if self.process:
            self.process.terminate()
            self.process.wait()


class CurlDownloader(BaseDownloader):
    """用系统自带的 curl.exe 下载。

    Windows 10 1803 之后 curl.exe 是系统组件，不依赖用户装任何东西。
    用外部进程而不是 Python 的 requests，有两个直接好处：
    - 下载进度解析全部在子进程里，Python 侧只按行读取，不占用 GIL，
      界面不会因为下载而卡顿（requests 逐块回调在后台线程里跑，
      大文件时仍会和 GUI 抢 GIL）；
    - curl 自带断点续传（-C -）与超时/重试，网络中断能接着下。
    """

    PROGRESS_TEMPLATE = (
        "%{json}"  # curl 7.70+ 支持；解析失败时退回按字节数显示
    )

    def __init__(self, url: str, save_path: Path, progress_callback):
        super().__init__(url, save_path, progress_callback)
        self.process = None
        self._last_error = ""

    @staticmethod
    def is_available() -> bool:
        return shutil.which("curl") is not None

    @property
    def last_error(self) -> str:
        return self._last_error

    def download(self) -> bool:
        self.save_path.parent.mkdir(parents=True, exist_ok=True)
        temp_file = self.save_path.with_name(self.save_path.name + ".part")

        total = _probe_remote_size(self.url)
        if total:
            logger.info("文件总大小: %s", _format_size(total))

        # 已有部分文件比服务器还大，说明是别的版本残留，丢掉
        if temp_file.exists() and total and temp_file.stat().st_size > total:
            temp_file.unlink(missing_ok=True)

        self.progress_callback(0, "正在连接...")

        cmd = [
            "curl",
            "--location",
            "--fail",
            "--silent",
            "--show-error",
            "--retry", "3",
            "--retry-delay", "2",
            "--retry-all-errors",
            "--connect-timeout", "20",
            "--speed-limit", "1024",
            "--speed-time", "60",
            "--continue-at", "-",
            "--output", str(temp_file),
            self.url,
        ]

        logger.info("使用 curl 下载: %s", self.url)
        self.process = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            universal_newlines=True,
            encoding="utf-8",
            errors="replace",
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )

        # curl 不输出进度时就轮询文件大小，保证进度条仍然在动
        last_size = -1
        while self.process.poll() is None:
            if self._cancelled:
                self.process.kill()
                logger.info("下载已取消")
                return False
            size = temp_file.stat().st_size if temp_file.exists() else 0
            if size != last_size:
                last_size = size
                self._report(size, total)
            time.sleep(0.5)

        stderr = ""
        try:
            stderr = self.process.stderr.read() or ""
        except Exception:
            pass
        rc = self.process.returncode

        if rc != 0:
            self._last_error = _summarize_curl_error(rc, stderr)
            logger.error("curl 下载失败 rc=%s: %s", rc, stderr[-500:])
            return False

        actual = temp_file.stat().st_size if temp_file.exists() else 0
        if total:
            if actual != total:
                self._last_error = (
                    f"文件不完整：{_format_size(actual)} / {_format_size(total)}"
                )
                logger.error(self._last_error)
                return False
        elif actual == 0:
            self._last_error = "下载到的文件为空"
            return False

        self._report(actual, total or actual)
        os.replace(temp_file, self.save_path)
        logger.info("下载完成: %s (%s)", self.save_path, _format_size(actual))
        return True

    def _report(self, done: int, total: int) -> None:
        if total > 0:
            percent = min(done / total * 100, 99.9)
            status = f"已下载: {_format_size(done)} / {_format_size(total)}"
        else:
            percent = 0
            status = f"已下载: {_format_size(done)}"
        self.progress_callback(percent, status)

    def cancel(self):
        super().cancel()
        if self.process:
            try:
                self.process.kill()
                self.process.wait(timeout=5)
            except Exception:
                pass


def _format_size(bytes_size: int) -> str:
    size = float(bytes_size)
    for unit in ["B", "KB", "MB", "GB"]:
        if size < 1024:
            return f"{size:.1f}{unit}"
        size /= 1024
    return f"{size:.1f}TB"


def _probe_remote_size(url: str) -> int:
    """查询文件总大小；失败返回 0（此时只能靠下载后目视判断）。"""
    if shutil.which("curl"):
        try:
            result = subprocess.run(
                [
                    "curl",
                    "--location",
                    "--silent",
                    "--head",
                    "--max-time", "30",
                    url,
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=40,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            for line in (result.stdout or "").splitlines():
                if line.lower().startswith("content-length:"):
                    value = line.split(":", 1)[1].strip()
                    if value.isdigit() and int(value) > 0:
                        return int(value)
        except Exception as e:
            logger.warning("curl 查询大小失败: %s", e)

    try:
        with requests.get(url, headers={"Range": "bytes=0-0"}, stream=True, timeout=30) as r:
            content_range = r.headers.get("content-range") or ""
            if "/" in content_range:
                total = content_range.rsplit("/", 1)[1].strip()
                if total.isdigit():
                    return int(total)
    except requests.RequestException:
        pass
    return 0


def _summarize_requests_error(error: Exception) -> str:
    """把 requests 的异常转成用户看得懂的说明。"""
    text = str(error)
    if "IncompleteRead" in text or "Connection broken" in text:
        return "传输中断，服务器只发送了部分数据"
    if "timed out" in text.lower() or "timeout" in text.lower():
        return "连接超时"
    if "Connection aborted" in text or "ConnectionResetError" in text:
        return "连接被重置"
    if "Name or service not known" in text or "getaddrinfo" in text:
        return "无法解析服务器地址"
    return text[:200]


def _summarize_curl_error(rc: int, stderr: str) -> str:
    """把 curl 的返回码翻译成用户看得懂的说明。"""
    messages = {
        6: "无法解析服务器地址（DNS 失败）",
        7: "连接不上服务器（网络不通或被拦截）",
        18: "传输中断，服务器只发送了部分数据",
        22: "服务器返回错误（文件不存在或需要权限）",
        28: "连接超时",
        35: "SSL 握手失败",
        56: "接收数据时连接被重置",
    }
    base = messages.get(rc, f"下载进程异常退出（代码 {rc}）")
    detail = (stderr or "").strip().splitlines()
    if detail:
        tail = detail[-1][:200]
        if tail:
            base = f"{base}：(curl) {tail}"
    return base


class RequestsDownloader(BaseDownloader):
    """Python requests 下载器（回退方案）

    与旧实现的区别（旧实现是"下载到一半就当中断，然后拿半截文件去解压"的根源）：

    1. 先向服务器查询文件的真实大小；
    2. 断线后按已有字节数用 Range 续传，而不是从头再下 1.4GB；
    3. 下载结束逐字节核对总大小，不一致就丢错误，绝不把残包交给解压器；
    4. 写到 .part 临时文件，校验通过才原子改名成正式文件名。
    """

    CHUNK_SIZE = 64 * 1024

    def download(self) -> bool:
        logger.info(f"使用 requests 下载: {self.url}")
        self.progress_callback(0, "正在连接...")

        self.save_path.parent.mkdir(parents=True, exist_ok=True)
        temp_file = self.save_path.with_name(self.save_path.name + ".part")

        total_size = self._query_total_size()
        if total_size:
            logger.info("文件总大小: %s", self._format_size(total_size))

        last_error = ""
        for attempt in range(1, MAX_RESUME_ATTEMPTS + 1):
            if self._cancelled:
                return False

            try:
                self._download_once(temp_file, total_size)
            except _Cancelled:
                logger.info("下载已取消")
                return False
            except requests.RequestException as e:
                last_error = _summarize_requests_error(e)
                logger.warning("第 %d 次下载中断: %s", attempt, e)
            except DownloadIncompleteError as e:
                last_error = _summarize_requests_error(e)
                logger.warning("第 %d 次下载不完整: %s", attempt, e)
            else:
                # 没有异常：核对字节数
                actual = temp_file.stat().st_size if temp_file.exists() else 0
                if not total_size or actual == total_size:
                    os.replace(temp_file, self.save_path)
                    logger.info("下载完成: %s (%s)", self.save_path, self._format_size(actual))
                    return True
                last_error = (
                    f"文件不完整：已下载 {self._format_size(actual)} / "
                    f"{self._format_size(total_size)}"
                )
                logger.warning("%s，准备续传", last_error)

            if attempt < MAX_RESUME_ATTEMPTS:
                done = temp_file.stat().st_size if temp_file.exists() else 0
                if total_size and done >= total_size:
                    # 大小已够但内容校验没通过，只能重来
                    temp_file.unlink(missing_ok=True)
                self.progress_callback(
                    (done / total_size * 100) if total_size else 0,
                    f"网络中断，正在续传（第 {attempt + 1} 次）…",
                )
                time.sleep(min(2 * attempt, 8))

        self._last_error = last_error or "下载失败"
        logger.error("下载多次重试仍失败: %s", self._last_error)
        return False

    def _download_once(self, temp_file: Path, total_size: int) -> None:
        """下载（或续传）一次，异常由调用方统一处理。"""
        done = temp_file.stat().st_size if temp_file.exists() else 0

        headers = {}
        if done and total_size:
            if done >= total_size:
                return  # 已经够了，交给上层校验
            headers["Range"] = f"bytes={done}-"

        with requests.get(
            self.url,
            stream=True,
            headers=headers,
            timeout=(READ_TIMEOUT, READ_TIMEOUT),
        ) as response:
            if response.status_code == 416:
                # Range 越界：本地文件比服务器还大，丢掉重下
                temp_file.unlink(missing_ok=True)
                raise DownloadIncompleteError("本地缓存文件异常，已丢弃")

            response.raise_for_status()

            if not total_size:
                total_size = self._total_from_response(response)
            if done and response.status_code != 206:
                # 服务器不支持续传，只能从头开始
                logger.info("服务器未返回 206，重新完整下载")
                done = 0
                mode = "wb"
            else:
                mode = "ab" if done else "wb"

            self._current_total = total_size

            with open(temp_file, mode) as f:
                for chunk in response.iter_content(chunk_size=self.CHUNK_SIZE):
                    if self._cancelled:
                        raise _Cancelled()
                    if not chunk:
                        continue
                    f.write(chunk)
                    done += len(chunk)
                    self._report(done, total_size)

    def _report(self, done: int, total_size: int) -> None:
        if total_size > 0:
            percent = min(done / total_size * 100, 100)
            status = (
                f"已下载: {self._format_size(done)} / {self._format_size(total_size)}"
            )
        else:
            percent = 0
            status = f"已下载: {self._format_size(done)}"
        self.progress_callback(percent, status)

    def _query_total_size(self) -> int:
        """用 Range: bytes=0-0 拿到文件总大小；拿不到返回 0。"""
        try:
            with requests.get(
                self.url,
                headers={"Range": "bytes=0-0"},
                stream=True,
                timeout=(READ_TIMEOUT, READ_TIMEOUT),
            ) as r:
                if r.status_code == 206:
                    return self._total_from_response(r)
                if r.status_code == 200:
                    return int(r.headers.get("content-length") or 0)
        except requests.RequestException as e:
            logger.warning("无法获取文件大小: %s", e)
        return 0

    @staticmethod
    def _total_from_response(response) -> int:
        """从 Content-Range / Content-Length 解析文件总大小。"""
        content_range = response.headers.get("content-range") or ""
        if "/" in content_range:
            total = content_range.rsplit("/", 1)[1].strip()
            if total.isdigit():
                return int(total)
        if response.status_code == 200:
            length = response.headers.get("content-length")
            if length and length.isdigit():
                return int(length)
        return 0

    @staticmethod
    def _format_size(bytes_size: int) -> str:
        """格式化文件大小"""
        size = float(bytes_size)
        for unit in ["B", "KB", "MB", "GB"]:
            if size < 1024:
                return f"{size:.1f}{unit}"
            size /= 1024
        return f"{size:.1f}TB"

    @property
    def last_error(self) -> str:
        return getattr(self, "_last_error", "")


class FileDownloadThread(QThread):
    """文件下载线程"""

    progress = pyqtSignal(float, str)
    finished = pyqtSignal()
    error = pyqtSignal(str)

    def __init__(self, url: str, save_path: str):
        super().__init__()
        self.url = url
        self.save_path = Path(save_path)
        self.downloader: BaseDownloader | None = None
        self._last_progress_at = 0.0
        self._cancelled = False

    def run(self):
        try:
            self.progress.emit(0, self.tr("正在连接..."))

            # 下载器优先级：
            #   1. aria2c —— 多线程、断点续传最稳（若用户自己装了）
            #   2. curl   —— Windows 自带，外部进程不抢 GIL，界面不卡
            #   3. requests —— 最后兜底，带续传与大小校验
            if Aria2Downloader.is_available():
                self.downloader = Aria2Downloader(
                    self.url, self.save_path, self._on_progress
                )
            elif CurlDownloader.is_available():
                logger.info("使用系统 curl 下载")
                self.downloader = CurlDownloader(
                    self.url, self.save_path, self._on_progress
                )
            else:
                logger.warning("aria2c 与 curl 均不可用，回退到 requests 下载")
                self.downloader = RequestsDownloader(
                    self.url, self.save_path, self._on_progress
                )

            success = self.downloader.download()

            if success:
                self.finished.emit()
            else:
                detail = getattr(self.downloader, "last_error", "") or ""
                message = self.tr("下载失败")
                if detail:
                    message = f"{message}：{detail}"
                elif self._cancelled:
                    message = self.tr("下载已取消")
                self.error.emit(message)

        except Exception as e:
            logger.exception("下载异常")
            self.error.emit(str(e))

    def _on_progress(self, percent: float, status: str):
        """进度回调（节流）

        下载器按数据块回调，频率可以高达每秒上千次。这里限制发送频率，
        避免 GUI 线程被进度事件淹没——界面在后台时尤其容易因此失去响应。
        最后一条（100%）始终放行，保证结束状态准确。
        """
        now = time.monotonic() * 1000
        if percent < 100 and now - self._last_progress_at < PROGRESS_INTERVAL_MS:
            return
        self._last_progress_at = now
        self.progress.emit(percent, status)

    def stop(self):
        """停止下载"""
        self._cancelled = True
        if self.downloader:
            self.downloader.cancel()
