import os
import shutil
import subprocess
import time
from pathlib import Path

from PyQt5.QtCore import Qt, QThread, pyqtSignal
from PyQt5.QtGui import QColor, QShowEvent
from PyQt5.QtWidgets import (
    QHBoxLayout,
    QHeaderView,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    BodyLabel,
    ComboBox,
    ComboBoxSettingCard,
    HyperlinkButton,
    HyperlinkCard,
    InfoBar,
    InfoBarPosition,
    MessageBox,
    MessageBoxBase,
    ProgressBar,
    PushButton,
    SettingCardGroup,
    SingleDirectionScrollArea,
    SubtitleLabel,
    SwitchSettingCard,
    TableItemDelegate,
    TableWidget,
)
from qfluentwidgets import FluentIcon as FIF

from videocaptioner.config import BIN_PATH, MODEL_PATH
from videocaptioner.core.entities import (
    FasterWhisperModelEnum,
    TranscribeLanguageEnum,
    VadMethodEnum,
)
from videocaptioner.core.utils.platform_utils import open_folder
from videocaptioner.ui.common.config import cfg
from videocaptioner.ui.components.LineEditSettingCard import LineEditSettingCard
from videocaptioner.ui.components.SpinBoxSettingCard import DoubleSpinBoxSettingCard
from videocaptioner.core.utils.logger import setup_logger
from videocaptioner.ui.thread.file_download_thread import FileDownloadThread
from videocaptioner.ui.thread.modelscope_download_thread import ModelscopeDownloadThread

logger = setup_logger("faster_whisper_setting")

# 在文件开头添加常量定义
FASTER_WHISPER_PROGRAMS = [
    {
        "label": "GPU（cuda） + CPU 版本",
        "value": "faster-whisper-gpu.7z",
        "type": "GPU",
        "size": "1.35 GB",
        "downloadLink": "https://modelscope.cn/models/bkfengg/whisper-cpp/resolve/master/Faster-Whisper-XXL_r245.2_windows.7z",
    },
    {
        "label": "CPU版本",
        "value": "faster-whisper.exe",
        "type": "CPU",
        "size": "78.7 MB",
        "downloadLink": "https://modelscope.cn/models/bkfengg/whisper-cpp/resolve/master/whisper-faster.exe",
    },
]

FASTER_WHISPER_MODELS = [
    {
        "label": "Tiny",
        "value": "faster-whisper-tiny",
        "size": "77824",
        "downloadLink": "https://huggingface.co/Systran/faster-whisper-tiny",
        "modelScopeLink": "pengzhendong/faster-whisper-tiny",
    },
    {
        "label": "Base",
        "value": "faster-whisper-base",
        "size": "148480",
        "downloadLink": "https://huggingface.co/Systran/faster-whisper-base",
        "modelScopeLink": "pengzhendong/faster-whisper-base",
    },
    {
        "label": "Small",
        "value": "faster-whisper-small",
        "size": "495616",
        "downloadLink": "https://huggingface.co/Systran/faster-whisper-small",
        "modelScopeLink": "pengzhendong/faster-whisper-small",
    },
    {
        "label": "Medium",
        "value": "faster-whisper-medium",
        "size": "1572864",
        "downloadLink": "https://huggingface.co/Systran/faster-whisper-medium",
        "modelScopeLink": "pengzhendong/faster-whisper-medium",
    },
    {
        "label": "Large-v1",
        "value": "faster-whisper-large-v1",
        "size": "3145728",
        "downloadLink": "https://huggingface.co/Systran/faster-whisper-large-v1",
        "modelScopeLink": "pengzhendong/faster-whisper-large-v1",
    },
    {
        "label": "Large-v2",
        "value": "faster-whisper-large-v2",
        "size": "3145728",
        "downloadLink": "https://huggingface.co/Systran/faster-whisper-large-v2",
        "modelScopeLink": "pengzhendong/faster-whisper-large-v2",
    },
    {
        "label": "Large-v3",
        "value": "faster-whisper-large-v3",
        "size": "3145728",
        "downloadLink": "https://huggingface.co/Systran/faster-whisper-large-v3",
        "modelScopeLink": "pengzhendong/faster-whisper-large-v3",
    },
    {
        "label": "Large-v3-turbo",
        "value": "faster-whisper-large-v3-turbo",
        "size": "1720320",
        "downloadLink": "https://huggingface.co/Systran/faster-whisper-large-v3-turbo",
        "modelScopeLink": "pengzhendong/faster-whisper-large-v3-turbo",
    },
]


# 在类外添加这个工具函数
# GPU 版解压后必须存在的关键文件；缺任何一个都说明没解压完整，
# 这时让用户去跑转录只会得到一个莫名其妙的失败。
XXL_REQUIRED_FILES = (
    "faster-whisper-xxl.exe",
    "_xxl_data/python310.dll",
    "_xxl_data/base_library.zip",
    "_xxl_data/ctranslate2/ctranslate2.dll",
    "_xxl_data/ctranslate2/_ext.cp310-win_amd64.pyd",
)

# 解压暂存目录：解压全部成功后才整体改名到正式位置
XXL_NAME = "Faster-Whisper-XXL"


def _xxl_dir() -> Path:
    return Path(BIN_PATH) / XXL_NAME


def _staging_dir() -> Path:
    """解压暂存目录（相对 BIN_PATH）。"""
    return Path(BIN_PATH) / ".faster-whisper-installing"


def check_xxl_install_complete() -> tuple[bool, str]:
    """检查 GPU 版程序是否完整可用。

    Returns:
        (是否完整, 缺失说明)
    """
    root = _xxl_dir()
    if not root.is_dir():
        return False, "程序目录不存在"

    missing = [
        rel for rel in XXL_REQUIRED_FILES if not (root / rel.replace("/", os.sep)).is_file()
    ]
    if missing:
        return False, "缺少文件: " + "、".join(missing)
    return True, ""


def check_faster_whisper_exists() -> tuple[bool, list[str]]:
    """检查 faster-whisper 程序是否存在且完整

    检查以下两种情况:
    1. bin目录下是否有 faster-whisper.exe
    2. bin目录下是否有 Faster-Whisper-XXL/faster-whisper-xxl.exe

    注意：只判断文件存不存在会把"解压了一半"的目录也当成已安装，
    之后转录时才会失败。这里对 GPU 版额外核对关键文件。

    Returns:
        tuple[bool, list[str]]: (是否存在程序, 已安装的版本列表)
    """
    bin_path = Path(BIN_PATH)
    installed_versions = []

    # 检查 faster-whisper.exe(CPU版本)
    cpu_exe = bin_path / "faster-whisper.exe"
    if cpu_exe.is_file() and cpu_exe.stat().st_size > 1024 * 1024:
        installed_versions.append("CPU")

    # 检查 Faster-Whisper-XXL/faster-whisper-xxl.exe(GPU版本)
    complete, reason = check_xxl_install_complete()
    if complete:
        installed_versions.extend(["GPU", "CPU"])
    elif (bin_path / XXL_NAME).exists():
        logger.warning("%s 目录不完整（%s），不视为已安装", XXL_NAME, reason)
    installed_versions = sorted(set(installed_versions))

    return bool(installed_versions), installed_versions


def _find_7z_executable() -> str | None:
    """查找可用的 7z 命令行程序，找不到返回 None。"""
    for name in ("7z", "7za", "7zr"):
        path = shutil.which(name)
        if path:
            return path
    for candidate in (
        r"C:\Program Files\7-Zip\7z.exe",
        r"C:\Program Files (x86)\7-Zip\7z.exe",
    ):
        if os.path.isfile(candidate):
            return candidate
    return None


def verify_7z_archive(archive_path: str) -> tuple[bool, str]:
    """解压前先确认压缩包本身是完整的。

    下载中断留下的半截 7z 往往还能被打开、甚至会解出一部分文件，
    直到中途报错才失败——这就是"装好了但转录失败"的来源。
    这里先做一次快速校验，不完整就明确告诉用户要重新下载。

    Returns:
        (是否完整, 说明)
    """
    try:
        import py7zr
    except ImportError:
        # 没有 py7zr 时退到外部工具校验；不可用就跳过这步
        return True, ""

    try:
        with py7zr.SevenZipFile(archive_path, mode="r") as archive:
            names = archive.getnames()
            if not names:
                return False, "压缩包是空的"
            # 读一下头部信息，损坏会被这里发现
            archive.archiveinfo()
            if archive.needs_password():
                return False, "压缩包已加密，无法自动解压"
        return True, ""
    except Exception as e:
        return False, f"压缩包已损坏或不完整（{e}）"


def _run_external_extractor(cmd: list[str], cancel_check) -> tuple[bool, str]:
    """运行外部解压命令，支持随时取消。

    用 Popen + 轮询而不是 subprocess.run，这样关闭窗口时能立刻杀掉解压进程，
    不会留一个占着 CPU/磁盘的后台任务。
    """
    process = subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    while process.poll() is None:
        if cancel_check and cancel_check():
            process.kill()
            process.wait(timeout=10)
            return False, "已取消"
        time.sleep(0.2)

    stderr = ""
    try:
        stderr = process.stderr.read() or ""
    except Exception:
        pass
    if process.returncode == 0:
        return True, ""
    detail = [ln for ln in stderr.strip().splitlines() if ln.strip()]
    return False, (detail[-1][:200] if detail else f"返回码 {process.returncode}")


def extract_7z_archive(archive_path: str, extract_path: str, cancel_check=None) -> None:
    """解压 7z 压缩包。

    优先用 Windows 自带的 tar.exe（libarchive，外部进程、速度快、不占 GIL），
    其次退回内置 py7zr，最后才是 7z 命令行。三种方式都失败才抛错。
    """
    os.makedirs(extract_path, exist_ok=True)
    errors = []

    if os.name == "nt" and shutil.which("tar.exe"):
        ok, detail = _run_external_extractor(
            ["tar.exe", "-xf", archive_path, "-C", extract_path], cancel_check
        )
        if ok:
            return
        if detail == "已取消":
            raise RuntimeError("已取消")
        errors.append(f"tar: {detail}")

    exe = _find_7z_executable()
    if exe:
        ok, detail = _run_external_extractor(
            [exe, "x", archive_path, f"-o{extract_path}", "-y"], cancel_check
        )
        if ok:
            return
        if detail == "已取消":
            raise RuntimeError("已取消")
        errors.append(f"7z: {detail}")

    try:
        import py7zr

        with py7zr.SevenZipFile(archive_path, mode="r") as archive:
            archive.extractall(path=extract_path)
        return
    except ImportError:
        errors.append("未安装 py7zr")
    except Exception as e:  # 加密、损坏、多卷等
        errors.append(f"py7zr: {e}")

    raise RuntimeError("无法解压压缩包：" + "；".join(errors))


class UnzipThread(QThread):
    """7z 解压线程。

    流程（顺序很重要）：
        1. 校验压缩包完整 —— 不完整直接报错，不动现有安装
        2. 解压到临时目录 —— 失败不影响现有目录
        3. 核对关键文件 —— 解压不完整则报错
        4. 原子替换 —— 用新目录顶替旧目录，最后才删压缩包

    以前是"边下边解 + 直接解到正式目录"，网络中断留下的残包会把
    原有安装覆盖成半成品，程序却仍显示"已安装"。
    """

    progress = pyqtSignal(str)  # 阶段说明
    finished = pyqtSignal()  # 解压完成信号
    error = pyqtSignal(str)  # 解压错误信号

    def __init__(self, zip_file, extract_path=None):
        super().__init__()
        self.zip_file = zip_file
        self.extract_path = Path(extract_path or BIN_PATH)
        self._cancelled = False

    def run(self):
        staging = self.extract_path / ".faster-whisper-installing"
        try:
            # 1. 校验压缩包
            self.progress.emit(self.tr("正在校验下载的文件..."))
            ok, reason = verify_7z_archive(self.zip_file)
            if not ok:
                self.error.emit(
                    f"{reason}。请删除该文件后重新下载：\n{self.zip_file}"
                )
                return

            # 2. 解压到临时目录
            self.progress.emit(self.tr("正在解压文件（约 1~3 分钟，请勿关闭窗口）..."))
            if staging.exists():
                shutil.rmtree(staging, ignore_errors=True)
            staging.mkdir(parents=True, exist_ok=True)
            extract_7z_archive(
                self.zip_file, str(staging), cancel_check=lambda: self._cancelled
            )

            if self._cancelled:
                shutil.rmtree(staging, ignore_errors=True)
                self.error.emit(self.tr("已取消"))
                return

            # 3. 核对解压结果
            self.progress.emit(self.tr("正在检查文件完整性..."))
            src_root = staging / XXL_NAME if (staging / XXL_NAME).is_dir() else staging
            missing = [
                rel
                for rel in XXL_REQUIRED_FILES
                if not (src_root / rel.replace("/", os.sep)).is_file()
            ]
            if missing:
                shutil.rmtree(staging, ignore_errors=True)
                self.error.emit(
                    "解压后的程序不完整（缺少 "
                    + "、".join(missing)
                    + "）。请删除压缩包后重新下载。"
                )
                return

            # 4. 原子替换正式目录
            final_dir = self.extract_path / XXL_NAME
            old_dir = self.extract_path / (XXL_NAME + ".old")
            if old_dir.exists():
                shutil.rmtree(old_dir, ignore_errors=True)
            if final_dir.exists():
                final_dir.rename(old_dir)
            src_root.rename(final_dir)
            shutil.rmtree(old_dir, ignore_errors=True)
            # 临时目录里除 XXL 之外的零散文件（如 license.txt）一并挪过去
            for extra in staging.iterdir():
                target = self.extract_path / extra.name
                if target.exists() and target.is_dir():
                    shutil.rmtree(target, ignore_errors=True)
                elif target.exists():
                    target.unlink(missing_ok=True)
                extra.rename(target)
            shutil.rmtree(staging, ignore_errors=True)

            # 5. 全部成功后才删压缩包
            try:
                os.remove(self.zip_file)
            except OSError:
                pass

            self.finished.emit()

        except Exception as e:
            logger.exception("解压失败")
            shutil.rmtree(staging, ignore_errors=True)
            self.error.emit(str(e))

    def cancel(self):
        self._cancelled = True


class FasterWhisperDownloadDialog(MessageBoxBase):
    """Faster Whisper 下载对话框"""

    # 添加类变量跟踪下载状态
    is_downloading = False

    def __init__(self, parent=None, setting_widget=None):
        super().__init__(parent)
        self.widget.setMinimumWidth(600)
        self.program_download_thread = None
        self.model_download_thread = None
        self._setup_ui()
        self._connect_signals()
        self.setting_widget = setting_widget
        self._lightweight_compositing = False

    def _enter_lightweight_mode(self) -> None:
        """下载期间降低窗口合成开销。

        这个对话框是覆盖整个主窗口的半透明遮罩 + 60px 投影。下载时进度条
        持续重绘，窗口一旦失去焦点，系统合成这类图层的代价明显更高
        （部分显卡/远程桌面环境下会卡到界面无响应）。
        下载期间去掉投影即可保持可交互，结束后恢复。
        """
        if self._lightweight_compositing:
            return
        self._lightweight_compositing = True
        try:
            self.widget.setGraphicsEffect(None)
            self.windowMask.setStyleSheet("background: rgba(0, 0, 0, 120)")
        except Exception:
            pass

    def _leave_lightweight_mode(self) -> None:
        """下载结束，恢复正常视觉效果。"""
        if not self._lightweight_compositing:
            return
        self._lightweight_compositing = False
        try:
            self.setShadowEffect(60, (0, 10), QColor(0, 0, 0, 50))
            self.setMaskColor(QColor(0, 0, 0, 76))
        except Exception:
            pass

    def _setup_ui(self):
        """设置UI"""
        layout = QVBoxLayout()
        self._setup_program_section(layout)
        layout.addSpacing(20)
        self._setup_model_section(layout)
        self._setup_progress_section(layout)

        self.viewLayout.addLayout(layout)
        self.cancelButton.setText(self.tr("关闭"))
        self.yesButton.hide()

    def _setup_program_section(self, layout):
        """设置程序下载部分UI"""
        # 标题和按钮的水平布局
        title_layout = QHBoxLayout()

        # 标题
        faster_whisper_title = SubtitleLabel(self.tr("Faster Whisper 下载"), self)
        title_layout.addWidget(faster_whisper_title)

        # 添加打开文件夹按钮
        open_folder_btn = HyperlinkButton("", self.tr("打开程序文件夹"), parent=self)
        open_folder_btn.setIcon(FIF.FOLDER)
        open_folder_btn.clicked.connect(self._open_program_folder)
        title_layout.addStretch()
        title_layout.addWidget(open_folder_btn)

        layout.addLayout(title_layout)
        layout.addSpacing(8)

        # 检查已安装的版本
        has_program, installed_versions = check_faster_whisper_exists()

        if has_program:
            # 显示已安装版本
            versions_text = " + ".join(installed_versions)
            program_status = BodyLabel(self.tr(f"已安装版本: {versions_text}"), self)
            program_status.setStyleSheet("color: green")
            layout.addWidget(program_status)

            # 添加说明标签
            if len(installed_versions) == 1:
                desc_label = BodyLabel(self.tr("您可以继续下载其他版本:"), self)
                layout.addWidget(desc_label)
        else:
            desc_label = BodyLabel(self.tr("未下载Faster Whisper 程序"), self)
            layout.addWidget(desc_label)

        # 下载控件
        program_layout = QHBoxLayout()
        self.program_combo = ComboBox(self)
        self.program_combo.setFixedWidth(300)
        self.program_combo.hide()

        # 只显示未安装的版本
        for program in FASTER_WHISPER_PROGRAMS:
            version_type = program["type"]
            if version_type not in installed_versions:
                self.program_combo.addItem(f"{program['label']} ({program['size']})")

        # 如果还有可下载的版本，显示下载控件
        if self.program_combo.count() > 0:
            self.program_combo.show()
            self.program_download_btn = PushButton(self.tr("下载程序"), self)
            self.program_download_btn.clicked.connect(self._start_download)
            program_layout.addWidget(self.program_combo)
            program_layout.addWidget(self.program_download_btn)
            program_layout.addStretch()
            layout.addLayout(program_layout)

    def _setup_model_section(self, layout):
        """设置模型下载部分UI"""
        # 标题和按钮的水平布局
        title_layout = QHBoxLayout()

        # 标题
        model_title = SubtitleLabel(self.tr("模型下载"), self)
        title_layout.addWidget(model_title)

        # 添加打开文件夹按钮
        open_folder_btn = HyperlinkButton("", self.tr("打开模型文件夹"), parent=self)
        open_folder_btn.setIcon(FIF.FOLDER)
        open_folder_btn.clicked.connect(self._open_model_folder)
        title_layout.addStretch()
        title_layout.addWidget(open_folder_btn)

        layout.addLayout(title_layout)
        layout.addSpacing(8)

        # 模型表格
        self.model_table = self._create_model_table()
        self._populate_model_table()
        layout.addWidget(self.model_table)

    def _create_model_table(self):
        """创建模型表格"""
        table = TableWidget(self)
        table.setEditTriggers(TableWidget.NoEditTriggers)
        table.setSelectionMode(TableWidget.NoSelection)
        table.setColumnCount(4)
        table.setHorizontalHeaderLabels(
            [self.tr("模型名称"), self.tr("大小"), self.tr("状态"), self.tr("操作")]
        )

        # 设置表格样式
        table.setBorderVisible(True)
        table.setBorderRadius(8)
        table.setItemDelegate(TableItemDelegate(table))

        # 设置列宽
        header = table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.Stretch)
        header.setSectionResizeMode(1, QHeaderView.Fixed)
        header.setSectionResizeMode(2, QHeaderView.Fixed)
        header.setSectionResizeMode(3, QHeaderView.Fixed)

        table.setColumnWidth(1, 100)
        table.setColumnWidth(2, 80)
        table.setColumnWidth(3, 150)

        # 设置行高
        row_height = 45
        table.verticalHeader().setDefaultSectionSize(row_height)

        # 设置表格高度
        header_height = 20
        max_visible_rows = 6
        table_height = row_height * max_visible_rows + header_height + 15
        table.setFixedHeight(table_height)

        return table

    def _setup_progress_section(self, layout):
        """设置进度显示部分UI"""
        self.progress_bar = ProgressBar(self)
        self.progress_label = BodyLabel("", self)
        self.progress_bar.hide()
        self.progress_label.hide()

        layout.addWidget(self.progress_bar)
        layout.addWidget(self.progress_label)

    def _populate_model_table(self):
        """填充模型表格数据"""
        self.model_table.setRowCount(len(FASTER_WHISPER_MODELS))
        for i, model in enumerate(FASTER_WHISPER_MODELS):
            self._add_model_row(i, model)

    def _add_model_row(self, row, model):
        """添加模型表格行"""
        # 模型名称
        name_item = QTableWidgetItem(model["label"])
        name_item.setTextAlignment(Qt.AlignCenter)  # type: ignore
        self.model_table.setItem(row, 0, name_item)

        # 大小
        size_item = QTableWidgetItem(f"{int(model['size']) / 1024:.1f} MB")
        size_item.setTextAlignment(Qt.AlignCenter)  # type: ignore
        self.model_table.setItem(row, 1, size_item)

        # 状态 - 检查model.bin文件是否存在
        model_path = os.path.join(MODEL_PATH, model["value"])
        model_bin_path = os.path.join(model_path, "model.bin")
        is_downloaded = os.path.exists(model_bin_path)

        status_item = QTableWidgetItem(
            self.tr("已下载") if is_downloaded else self.tr("未下载")
        )
        if is_downloaded:
            status_item.setForeground(Qt.green)  # type: ignore
        status_item.setTextAlignment(Qt.AlignCenter)  # type: ignore
        self.model_table.setItem(row, 2, status_item)

        # 下载按钮
        button_container = QWidget()
        button_layout = QHBoxLayout(button_container)
        button_layout.setContentsMargins(4, 4, 4, 4)

        download_btn = HyperlinkButton(
            "",
            self.tr("重新下载") if is_downloaded else self.tr("下载"),
            parent=self,
        )
        download_btn.setIcon(FIF.DOWNLOAD)
        download_btn.clicked.connect(lambda checked, r=row: self._download_model(r))

        button_layout.addStretch()
        button_layout.addWidget(download_btn)
        button_layout.addStretch()
        self.model_table.setCellWidget(row, 3, button_container)

    def _connect_signals(self):
        """连接信号"""
        self.rejected.connect(self._on_dialog_reject)

    def _start_download(self):
        """开始下载"""
        if FasterWhisperDownloadDialog.is_downloading:
            InfoBar.warning(
                self.tr("下载进行中"),
                self.tr("请等待当前下载任务完成"),
                duration=3000,
                parent=self,
            )
            return

        FasterWhisperDownloadDialog.is_downloading = True
        # 禁用所有下载按钮
        self._set_all_download_buttons_enabled(False)

        # 获取选中的文本
        selected_text = self.program_combo.currentText()

        # 从显示文本中提取程序标签
        selected_label = selected_text.split(" (")[0]

        # 根据标签找到对应的程序配置
        program = next(
            (p for p in FASTER_WHISPER_PROGRAMS if p["label"] == selected_label), None
        )

        if not program:
            InfoBar.error(
                self.tr("下载错误"),
                self.tr("未找到对应的程序配置"),
                duration=3000,
                parent=self,
            )
            FasterWhisperDownloadDialog.is_downloading = False
            self._set_all_download_buttons_enabled(True)
            return

        # 确保 BIN_PATH 目录存在
        os.makedirs(BIN_PATH, exist_ok=True)

        # GPU 版：如果上次已经下好了压缩包只是解压失败，直接复用，不重新下载 1.4GB
        if program["value"].endswith(".7z"):
            existing = Path(BIN_PATH) / program["value"]
            if existing.is_file() and existing.stat().st_size > 10 * 1024 * 1024:
                ok, reason = verify_7z_archive(str(existing))
                if ok:
                    FasterWhisperDownloadDialog.is_downloading = False
                    self._set_all_download_buttons_enabled(True)
                    self._install_from_existing_archive()
                    return
                # 不完整的包：提示续传而不是整包重下
                if self._offer_resume_or_redownload(existing, reason):
                    return

            # 上次下载中途被打断留下的 .part：直接续传，不再问
            part = existing.with_name(existing.name + ".part")
            if part.is_file() and part.stat().st_size > 1024 * 1024:
                logger.info("发现未完成的下载，继续: %s", part)
                self._resume_existing_download(existing, part)
                return

        self.progress_bar.show()
        self.progress_label.show()
        self.program_download_btn.setEnabled(False)
        self.program_combo.setEnabled(False)
        self._enter_lightweight_mode()

        # 直接下载到bin目录
        save_path = os.path.join(BIN_PATH, program["value"])

        self.program_download_thread = FileDownloadThread(
            program["downloadLink"], save_path
        )
        self.program_download_thread.progress.connect(
            self._on_program_download_progress
        )
        self.program_download_thread.finished.connect(
            lambda: self._on_program_download_finished(save_path)
        )
        self.program_download_thread.error.connect(self._on_program_download_error)
        self.program_download_thread.start()

    def _on_program_download_progress(self, value, status_msg):
        """更新程序下载进度"""
        self.progress_bar.setValue(int(value))
        self.progress_label.setText(status_msg)

    def _on_program_download_finished(self, save_path):
        """程序下载完成处理（此时文件已通过完整性校验）"""
        try:
            # 检查是否是 CPU 版本的直接下载
            if save_path.endswith(".exe"):
                target = os.path.join(BIN_PATH, "faster-whisper.exe")
                os.replace(save_path, target)
                self._finish_program_installation()
            else:
                # GPU 版本需要解压：进度条切到不确定状态，避免停在 100% 让人以为结束了
                self.progress_bar.setValue(0)
                self.progress_label.setText(self.tr("正在解压文件..."))

                # 创建并启动解压线程
                self.unzip_thread = UnzipThread(save_path, BIN_PATH)
                self.unzip_thread.progress.connect(self.progress_label.setText)
                self.unzip_thread.finished.connect(self._finish_program_installation)
                self.unzip_thread.error.connect(self._on_unzip_error)
                self.unzip_thread.start()
                return  # 提前返回,等待解压完成

        except Exception as e:
            logger.exception("安装失败")
            InfoBar.error(self.tr("安装失败"), str(e), duration=5000, parent=self)
            self._cleanup_installation()

    def _on_program_download_error(self, error):
        """程序下载错误处理"""
        InfoBar.error(self.tr("下载失败"), error, duration=8000, parent=self)
        # 下载失败时清掉半截文件，避免下次被当成"已下载"
        self._discard_partial_download()
        FasterWhisperDownloadDialog.is_downloading = False
        self._set_all_download_buttons_enabled(True)
        if hasattr(self, "program_download_btn"):
            self.program_download_btn.setEnabled(True)
        self.program_combo.setEnabled(True)
        self.progress_bar.hide()
        self.progress_label.hide()
        self._leave_lightweight_mode()

    def _discard_partial_download(self):
        """清理本次下载可能留下的 .part 文件。"""
        if not hasattr(self, "program_combo"):
            return
        selected_text = self.program_combo.currentText()
        selected_label = selected_text.split(" (")[0]
        program = next(
            (p for p in FASTER_WHISPER_PROGRAMS if p["label"] == selected_label), None
        )
        if not program:
            return
        partial = Path(BIN_PATH) / (program["value"] + ".part")
        try:
            partial.unlink(missing_ok=True)
        except OSError:
            pass

    def _offer_resume_or_redownload(self, existing: Path, reason: str) -> bool:
        """已有压缩包不完整时，询问用户是否续传。

        Returns:
            True 表示用户选择了续传（调用方直接返回），False 表示要整包重下。
        """
        size_mb = existing.stat().st_size / (1024 * 1024)
        box = MessageBox(
            self.tr("上次的下载不完整"),
            self.tr(
                f"发现上次下载的压缩包只有 {size_mb:.0f} MB，校验未通过（{reason}）。\n\n"
                "「继续下载」会从中断处接着下，不用从头开始；\n"
                "「重新下载」会删掉这个文件，从头下载。"
            ),
            self,
        )
        box.yesButton.setText(self.tr("继续下载"))
        box.cancelButton.setText(self.tr("重新下载"))
        resume = bool(box.exec())

        if resume:
            self._resume_existing_download(existing)
        else:
            try:
                existing.unlink()
            except OSError as e:
                logger.warning("删除残缺压缩包失败: %s", e)
        return resume

    def _resume_existing_download(self, existing: Path, part: Path | None = None):
        """把已有的半截文件当作 .part，从断点继续下载。"""
        if part is None:
            part = existing.with_name(existing.name + ".part")
            try:
                if part.exists():
                    part.unlink()
                existing.rename(part)
            except OSError as e:
                InfoBar.error(self.tr("无法续传"), str(e), duration=6000, parent=self)
                return

        program = next(
            (p for p in FASTER_WHISPER_PROGRAMS if p["value"] == existing.name), None
        )
        if not program:
            return

        self.progress_bar.show()
        self.progress_label.show()
        self.progress_bar.setValue(0)
        done_mb = part.stat().st_size / (1024 * 1024) if part.exists() else 0
        self.progress_label.setText(
            self.tr(f"已下载 {done_mb:.0f} MB，正在从断点继续…")
        )
        self._enter_lightweight_mode()

        save_path = str(existing)
        self.program_download_thread = FileDownloadThread(
            program["downloadLink"], save_path
        )
        self.program_download_thread.progress.connect(self._on_program_download_progress)
        self.program_download_thread.finished.connect(
            lambda: self._on_program_download_finished(save_path)
        )
        self.program_download_thread.error.connect(self._on_program_download_error)
        self.program_download_thread.start()

    def _install_from_existing_archive(self):
        """发现已下载好的压缩包时，直接解压，不用重新下载。"""
        archive = Path(BIN_PATH) / "faster-whisper-gpu.7z"
        if not archive.is_file():
            self._start_download()
            return

        answer = MessageBox(
            self.tr("发现已下载的程序包"),
            self.tr(
                "程序文件夹里已有下载好的 faster-whisper-gpu.7z。\n"
                "要直接用这个文件解压安装吗？（不需要重新下载）"
            ),
            self,
        )
        if not answer.exec():
            self._start_download()
            return

        FasterWhisperDownloadDialog.is_downloading = True
        self._set_all_download_buttons_enabled(False)
        self.progress_bar.show()
        self.progress_label.show()
        self.progress_bar.setValue(0)
        self.progress_label.setText(self.tr("正在校验下载的文件..."))
        self._enter_lightweight_mode()

        self.unzip_thread = UnzipThread(str(archive), BIN_PATH)
        self.unzip_thread.progress.connect(self.progress_label.setText)
        self.unzip_thread.finished.connect(self._finish_program_installation)
        self.unzip_thread.error.connect(self._on_unzip_error)
        self.unzip_thread.start()

    def _on_dialog_reject(self):
        """对话框关闭处理。

        注意：这个槽是由 rejected 信号触发的。旧实现在这里又调了一次
        self.reject()，而 reject() 最终还会再发 rejected —— 于是关闭按钮
        会进入"反复关闭动画"的死循环，界面看起来就是卡死。
        这里只负责停止后台任务，真正的关闭交给 Qt 自己完成。
        """
        self._stop_background_tasks()
        FasterWhisperDownloadDialog.is_downloading = False

    def _stop_background_tasks(self):
        """停止所有后台线程。

        关键是 wait()：QThread 还在运行时对象就被销毁会让 Qt 直接 abort，
        表现出来就是"点关闭整个程序没了"。这里必须等线程真正退出。
        """
        for name in ("program_download_thread", "model_download_thread", "unzip_thread"):
            thread = getattr(self, name, None)
            if thread is None:
                continue
            try:
                if not thread.isRunning():
                    continue
                # 优先走各线程自己的"优雅停止"
                for method in ("stop", "cancel"):
                    if hasattr(thread, method):
                        getattr(thread, method)()
                        break
                if thread.wait(2000):
                    continue
                logger.warning("%s 未在 2 秒内退出，强制终止", name)
                thread.terminate()
                thread.wait(3000)
            except Exception as e:
                logger.warning("停止 %s 失败: %s", name, e)

    def closeEvent(self, event):
        """窗口关闭事件处理"""
        self._stop_background_tasks()
        FasterWhisperDownloadDialog.is_downloading = False
        super().closeEvent(event)

    def _download_model(self, row):
        """下载选中的模型"""
        if FasterWhisperDownloadDialog.is_downloading:
            InfoBar.warning(
                self.tr("下载进行中"),
                self.tr("请等待当前下载任务完成"),
                duration=3000,
                parent=self,
            )
            return

        FasterWhisperDownloadDialog.is_downloading = True
        self._set_all_download_buttons_enabled(False)

        model = FASTER_WHISPER_MODELS[row]
        self.progress_bar.show()
        self.progress_label.show()
        self.progress_label.setText(self.tr(f"正在下载 {model['label']} 模型..."))
        self._enter_lightweight_mode()

        # 禁用当前行的下载按钮
        button_container = self.model_table.cellWidget(row, 3)
        download_btn = button_container.findChild(HyperlinkButton)
        if download_btn:
            download_btn.setEnabled(False)

        # 创建并启动下载线程，保存到类属性
        self.model_download_thread = ModelscopeDownloadThread(
            model["modelScopeLink"], os.path.join(MODEL_PATH, model["value"])
        )

        def _on_model_download_progress(value, msg):
            self.progress_bar.setValue(value)
            self.progress_label.setText(msg)

        def _on_model_download_finished():
            FasterWhisperDownloadDialog.is_downloading = False
            self._set_all_download_buttons_enabled(True)
            # 更新状态
            status_item = QTableWidgetItem(self.tr("已下载"))
            status_item.setForeground(Qt.green)  # type: ignore
            status_item.setTextAlignment(Qt.AlignCenter)  # type: ignore
            self.model_table.setItem(row, 2, status_item)

            # 更新下载按钮文本
            if download_btn:
                download_btn.setText(self.tr("重新下载"))
                download_btn.setEnabled(True)

            model = FASTER_WHISPER_MODELS[row]

            # 更新主设置对话框的模型选择
            if self.setting_widget:
                # 保存当前值并清空
                current_value = cfg.faster_whisper_model.value
                combo = self.setting_widget.model_card.comboBox
                combo.clear()

                # 找出已下载的模型
                available = []
                model_map = {
                    m["label"].lower(): m["value"] for m in FASTER_WHISPER_MODELS
                }
                for enum_val in FasterWhisperModelEnum:
                    if enum_val.value in model_map:
                        if (MODEL_PATH / model_map[enum_val.value]).exists():
                            available.append(enum_val)

                # 重建下拉框
                self.setting_widget.model_card.optionToText = {
                    e: e.value for e in available
                }
                for enum_val in available:
                    combo.addItem(enum_val.value, userData=enum_val)

                # 恢复选择
                if current_value in available:
                    combo.setCurrentText(current_value.value)
                elif combo.count() > 0:
                    combo.setCurrentIndex(0)

            InfoBar.success(
                self.tr("下载成功"),
                self.tr(f"{model['label']} 模型已下载完成"),
                duration=3000,
                parent=self,
            )
            self.progress_bar.hide()
            self.progress_label.hide()

        def _on_model_download_error(error):
            FasterWhisperDownloadDialog.is_downloading = False
            self._set_all_download_buttons_enabled(True)
            if download_btn:
                download_btn.setEnabled(True)

            InfoBar.error(self.tr("下载失败"), str(error), duration=3000, parent=self)
            self.progress_bar.hide()
            self.progress_label.hide()
            self._leave_lightweight_mode()

        self.model_download_thread.progress.connect(_on_model_download_progress)
        self.model_download_thread.finished.connect(_on_model_download_finished)
        self.model_download_thread.error.connect(_on_model_download_error)
        self.model_download_thread.start()

    def _set_all_download_buttons_enabled(self, enabled: bool):
        """设置所有下载按钮的启用状态"""
        # 设置程序下载按钮
        if hasattr(self, "program_download_btn"):
            self.program_download_btn.setEnabled(enabled)
            self.program_combo.setEnabled(enabled)

        # 设置所有模型下载按钮
        for row in range(self.model_table.rowCount()):
            button_container = self.model_table.cellWidget(row, 3)
            if button_container:
                download_btn = button_container.findChild(HyperlinkButton)
                if download_btn:
                    download_btn.setEnabled(enabled)

    def _open_model_folder(self):
        """打开模型文件夹"""
        if os.path.exists(MODEL_PATH):
            # 根据操作系统打开文件夹
            open_folder(str(MODEL_PATH))

    def _open_program_folder(self):
        """打开程序文件夹"""
        if os.path.exists(BIN_PATH):
            # 根据操作系统打开文件夹
            open_folder(str(BIN_PATH))

    def _finish_program_installation(self):
        """完成程序安装"""
        InfoBar.success(
            self.tr("安装完成"),
            self.tr("Faster Whisper 程序已安装成功"),
            duration=3000,
            parent=self,
        )
        self.accept()
        self._cleanup_installation()

    def _on_unzip_error(self, error_msg):
        """处理解压错误"""
        InfoBar.error(self.tr("安装失败"), error_msg, duration=10000, parent=self)
        self._cleanup_installation()
        self._refresh_install_state()

    def _refresh_install_state(self):
        """按实际安装情况刷新界面，避免显示与实际不符的"已安装"。"""
        try:
            has_program, versions = check_faster_whisper_exists()
            logger.info("Faster Whisper 安装状态：%s %s", has_program, versions)
        except Exception:
            pass

    def _cleanup_installation(self):
        """清理安装状态"""
        FasterWhisperDownloadDialog.is_downloading = False
        self._set_all_download_buttons_enabled(True)
        self.progress_bar.hide()
        self.progress_label.hide()
        self._leave_lightweight_mode()


class FasterWhisperSettingWidget(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setup_ui()
        self._connect_signals()

    def showEvent(self, a0: QShowEvent) -> None:
        super().showEvent(a0)
        # 检查Faster Whisper模型是否存在
        is_faster_whisper_exists, _ = check_faster_whisper_exists()
        if not is_faster_whisper_exists:
            self.show_error_info(self.tr("Faster Whisper程序不存在，请先下载程序"))
            self._show_model_manager()
        return

    def setup_ui(self):
        self.main_layout = QVBoxLayout(self)

        # 创建单向滚动区域和容器
        self.scrollArea = SingleDirectionScrollArea(orient=Qt.Vertical, parent=self)  # type: ignore
        self.scrollArea.setStyleSheet(
            "QScrollArea{background: transparent; border: none}"
        )

        self.container = QWidget(self)
        self.container.setStyleSheet("QWidget{background: transparent}")
        self.containerLayout = QVBoxLayout(self.container)

        self.setting_group = SettingCardGroup(
            self.tr("Faster Whisper 设置"), self
        )

        # 模型选择
        self.model_card = ComboBoxSettingCard(
            cfg.faster_whisper_model,
            FIF.ROBOT,
            self.tr("模型"),
            self.tr("选择 Faster Whisper 模型"),
            [model.value for model in FasterWhisperModelEnum],
            self.setting_group,
        )

        # 检查未下载的模型并从下拉框中移除
        for i in range(self.model_card.comboBox.count() - 1, -1, -1):
            model_text = self.model_card.comboBox.itemText(i).lower()
            model_config = next(
                (
                    model
                    for model in FASTER_WHISPER_MODELS
                    if model["label"].lower() == model_text
                ),
                None,
            )
            if model_config:
                model_path = Path(MODEL_PATH) / model_config["value"]
                model_bin_path = model_path / "model.bin"
                if model_bin_path.exists():
                    continue
            self.model_card.comboBox.removeItem(i)

        # 创建管理模型卡片
        self.manage_model_card = HyperlinkCard(
            "",  # 无链接
            self.tr("管理模型"),
            FIF.DOWNLOAD,  # 使用下载图标
            self.tr("模型管理"),
            self.tr("下载或更新 Faster Whisper 模型"),
            self.setting_group,  # 添加到设置组
        )

        # 语言选择
        self.language_card = ComboBoxSettingCard(
            cfg.transcribe_language,
            FIF.LANGUAGE,
            self.tr("源语言"),
            self.tr("音视频中说话的语言，默认根据前30秒自动识别"),
            [lang.value for lang in TranscribeLanguageEnum],
            self.setting_group,
        )
        self.language_card.comboBox.setMaxVisibleItems(6)

        # 设备选择
        self.device_card = ComboBoxSettingCard(
            cfg.faster_whisper_device,
            FIF.IOT,
            self.tr("运行设备"),
            self.tr("模型运行设备"),
            ["cuda", "cpu"],
            self.setting_group,
        )
        # _, available_devices = check_faster_whisper_exists()
        # if "GPU" not in available_devices:
        #     self.device_card.comboBox.removeItem(0)

        # VAD设置组
        self.vad_group = SettingCardGroup(self.tr("VAD设置"), self)

        # VAD过滤开关
        self.vad_filter_card = SwitchSettingCard(
            FIF.CHECKBOX,
            self.tr("VAD过滤"),
            self.tr("过滤无人声语音片断，减少幻觉"),
            cfg.faster_whisper_vad_filter,
            self.vad_group,
        )

        # VAD阈值
        self.vad_threshold_card = DoubleSpinBoxSettingCard(
            cfg.faster_whisper_vad_threshold,
            FIF.VOLUME,  # type: ignore
            self.tr("VAD阈值"),
            self.tr("语音概率阈值，高于此值视为语音"),
            minimum=0.00,
            maximum=1.00,
            decimals=2,
            step=0.05,
        )

        # VAD方法
        self.vad_method_card = ComboBoxSettingCard(
            cfg.faster_whisper_vad_method,
            FIF.MUSIC,
            self.tr("VAD方法"),
            self.tr("选择VAD检测方法"),
            [method.value for method in VadMethodEnum],
            self.vad_group,
        )

        # 其他设置组
        self.other_group = SettingCardGroup(self.tr("其他设置"), self)

        # 音频降噪
        self.ff_mdx_kim2_card = SwitchSettingCard(
            FIF.MUSIC,
            self.tr("人声分离"),
            self.tr("处理前使用MDX-Net降噪，分离人声和背景音乐"),
            cfg.faster_whisper_ff_mdx_kim2,
            self.other_group,
        )

        # 单词时间戳
        self.one_word_card = SwitchSettingCard(
            FIF.UNIT,
            self.tr("单字时间戳"),
            self.tr("开启生成单字级时间戳；关闭后使用原始分段断句"),
            cfg.faster_whisper_one_word,
            self.other_group,
        )

        # 提示词
        self.prompt_card = LineEditSettingCard(
            cfg.faster_whisper_prompt,
            FIF.CHAT,
            self.tr("提示词"),
            self.tr("可选的提示词,默认空"),
            "",
            self.other_group,
        )

        # 添加模型设置组的卡片
        self.setting_group.addSettingCard(self.model_card)
        self.setting_group.addSettingCard(self.manage_model_card)
        self.setting_group.addSettingCard(self.device_card)
        self.setting_group.addSettingCard(self.language_card)

        # 添加VAD设置组的卡片
        self.vad_group.addSettingCard(self.vad_filter_card)
        self.vad_group.addSettingCard(self.vad_threshold_card)
        self.vad_group.addSettingCard(self.vad_method_card)

        # 添加其他设置的卡片
        self.other_group.addSettingCard(self.ff_mdx_kim2_card)
        self.other_group.addSettingCard(self.one_word_card)
        self.other_group.addSettingCard(self.prompt_card)

        # 将所有设置组添加到容器布局
        self.containerLayout.addWidget(self.setting_group)
        self.containerLayout.addWidget(self.vad_group)
        self.containerLayout.addWidget(self.other_group)
        self.containerLayout.addStretch(1)

        # 设置组件最小宽度
        self.model_card.comboBox.setMinimumWidth(200)
        self.device_card.comboBox.setMinimumWidth(200)
        self.language_card.comboBox.setMinimumWidth(200)
        self.vad_method_card.comboBox.setMinimumWidth(200)
        self.prompt_card.lineEdit.setMinimumWidth(200)

        # 设置滚动区域
        self.scrollArea.setWidget(self.container)
        self.scrollArea.setWidgetResizable(True)

        # 将滚动区域添加到主布局
        self.main_layout.addWidget(self.scrollArea)

    def _connect_signals(self):
        """连接信号"""
        self.manage_model_card.linkButton.clicked.connect(self._show_model_manager)
        self.vad_filter_card.checkedChanged.connect(self._on_vad_filter_changed)

    def _on_vad_filter_changed(self, checked: bool):
        """VAD过滤开关状态改变时的处理"""
        self.vad_threshold_card.setEnabled(checked)
        self.vad_method_card.setEnabled(checked)

    def _show_model_manager(self):
        """显示模型管理对话框"""
        dialog = FasterWhisperDownloadDialog(self.window(), self)
        dialog.exec_()

    def show_error_info(self, error_msg):
        """显示错误信息"""
        InfoBar.error(
            title=self.tr("错误"),
            content=error_msg,
            parent=self.window(),
            duration=5000,
            position=InfoBarPosition.BOTTOM,
        )

    def check_faster_whisper_model(self):
        """检查选定的Faster Whisper模型是否存在

        Returns:
            bool: 如果模型存在且配置正确返回True，否则返回False
        """
        # 检查程序是否存在
        has_program, _ = check_faster_whisper_exists()
        if not has_program:
            self.show_error_info(self.tr("Faster Whisper程序不存在，请先下载程序"))
            return False

        model_value = cfg.faster_whisper_model.value.value
        # 检查模型配置是否存在
        model_config = next(
            (
                m
                for m in FASTER_WHISPER_MODELS
                if m["label"].lower() == model_value.lower()
            ),
            None,
        )
        if not model_config:
            self.show_error_info(self.tr("模型配置不存在"))
            return False

        model_path = MODEL_PATH / model_config["value"]
        model_files = model_path / "model.bin"
        # 检查模型文件是否存在
        if not model_path.exists() and not model_files.exists():
            self.show_error_info(self.tr("模型文件不存在: ") + model_value)
            return False
        return True
