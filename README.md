# VideoCaptioner 免安装增强版（非官方）

> **这不是官方版本。** 本项目是 [WEIFENG2333/VideoCaptioner](https://github.com/WEIFENG2333/VideoCaptioner)
> v1.4.2 的个人修改分支，针对便携使用、下载稳定性和中文字幕质量做了改进。
> 官方版本请访问原仓库。

卡卡字幕助手 | VideoCaptioner —— 基于 LLM 的智能字幕助手，支持视频字幕生成、断句、校正、翻译全流程。

---

## 下载使用

1. 到 [Releases](../../releases) 下载 `VideoCaptioner-1.4.2-Portable-Win64-Patched.zip`（约 328 MB）
2. 解压到任意位置（路径可含中文和空格）
3. 双击 `VideoCaptioner.exe`

**注意**：不要在压缩包内直接运行，必须先完整解压。

详细说明见压缩包内的 `使用说明.txt`。

### 快速上手（无需配置）

1. 把视频拖进「任务创建」
2. 语音识别选择「B 接口」（必剪，免费）
3. 需要翻译时，在设置里选「公益模型（免 API）」
4. 若视频有多条音轨，按弹窗提示选择

---

## 相对原版的改动

以下顺序与包内 `使用说明.txt` 一致，方便对照阅读。

### 一、开箱即用的免安装包

- 内置 Python 运行时，解压后双击 `VideoCaptioner.exe` 即可使用
- 不需要单独安装 Python / ffmpeg / 7-Zip
- 配置、缓存、日志、模型都保存在程序目录内，整个文件夹可随意搬移

### 二、修复 Faster Whisper 下载与安装

原版存在三个问题：

| 问题 | 原因 | 本版处理 |
|---|---|---|
| 提示"安装失败 `[WinError 2]`" | 依赖系统 `7z` 命令 | 改用 Windows 自带 `tar.exe`，回退 py7zr |
| 下载中断后卡死 | 逐块回调抢占 GIL | 改用系统 `curl.exe` 外部进程下载 |
| 装完仍提示未安装 / 转录失败 | 只判断 exe 是否存在 | 校验 5 个关键文件 |

同时增加：

- **断点续传**：网络中断后继续下载，不用重下 1.4GB
- **下载完成才解压**：先核对文件大小，不完整就报错
- **原子替换**：解压到临时目录，校验通过才替换正式目录
- **复用已下载的包**：压缩包完好但解压失败时，直接重试解压

### 三、字幕按停顿断句（一句话就是一句话）

原版只要不超过字数上限就会拼接，导致几句话挤在一条字幕里。

现在：

- 说话停顿 **≥ 1 秒**（含 1 秒）就另起一条字幕
- 不区分是同一个人换气还是两个人对话
- 字幕设置里可调「停顿断句阈值」（200~5000 毫秒）
- 新增「字幕分割类型」：句子分段 / 语义分段
- 「去除末尾标点符号」改为真正的开关

转录阶段同步收紧：faster-whisper 的 `--max_gap` 从默认 3 秒改为 1 秒。

### 四、字幕文件带语言后缀、统一 SRT

任务结束后的输出文件：

```
【字幕】视频名-中文.srt
【字幕】视频名-英语.srt
```

- 文件名直接标出语言
- 不再生成 `.ass`
- 重名自动加 `(2)`、`(3)`，不会覆盖

### 五、公益模型（免 API）

在「设置 → LLM 提供商」里选择「公益模型（免 API）」：

- 无需任何 API Key
- 翻译走微软翻译（国内可直连）或谷歌翻译
- 断句改用本地规则，不调用大模型

同时修复了原版失效的免费翻译接口：

- 微软：旧 `edge.microsoft.com/translate/auth` 已 404，改用 Bing 网页翻译器接口
- 谷歌：旧接口被限流，改用 `clients5` 接口

### 六、多音轨选择

拖入多音轨视频时弹窗选择用哪条音轨，音轨信息一路传到识别环节。

### 七、子进程静默运行（不再弹黑框）

ffmpeg / ffprobe 不再弹出黑色控制台窗口，包括第三方库自己启动的进程。

### 八、任务栏图标与固定到任务栏

写入 AppUserModelID + 创建开始菜单快捷方式，任务栏显示本程序图标，
固定后点击可正常启动。首次运行如仍是旧图标，取消固定后重新固定即可。

### 九、修复字幕时间轴未对齐音频

保存前统一处理：裁掉字幕重叠、补足零时长、截断跨越静音的长尾。

### 十、界面汉化补充

补齐遗漏的英文文案（设置导航项、请求日志字段、字幕提示词等）。

### 十一、缓解「切到后台后界面卡死」

- 下载进度信号节流（每 8KB 一次 → 每 100ms 一次）
- 修正多个下载并发时对全局 stdout 的相互覆盖
- 下载期间降低弹窗合成开销
- 主线程阻塞超过 1.5 秒写入日志，便于定位

### 顺带修复的隐藏 bug

- **CPU 识别报 float16 错误**：原逻辑只要检测到 50 系显卡就强制传
  `--compute_type float16`，用 CPU 识别时会直接失败。现在只在 cuda 设备下生效。
- **翻译失败结果被缓存 7 天**：现在只在全部译文非空时才写缓存。

---

## 源码与构建

本仓库基于上游 v1.4.2 源码，所有改动集中在一次提交中。

### 应用改动到上游源码

```bash
git clone https://github.com/WEIFENG2333/VideoCaptioner.git
cd VideoCaptioner
git checkout v1.4.2
git apply portable-changes.patch
```

`portable-changes.patch` 见 [Releases](../../releases) 附件。

### 改动统计

```
37 files changed, 2633 insertions(+), 312 deletions(-)
```

新增文件（5 个）：

```
videocaptioner/core/utils/app_identity.py                    任务栏图标标识
videocaptioner/core/utils/silent_process.py                  子进程静默
videocaptioner/core/utils/subtitle_naming.py                 字幕语言后缀
videocaptioner/core/utils/ui_watchdog.py                     主线程卡顿日志
videocaptioner/ui/components/AudioTrackSelectionDialog.py    多音轨选择弹窗
```

主要修改：

| 文件 | 说明 |
|---|---|
| `ui/thread/file_download_thread.py` | curl 下载器、断点续传、大小校验 |
| `ui/components/FasterWhisperSettingWidget.py` | 解压校验、原子替换、完整性检测 |
| `core/split/split.py` | 停顿断句（1 秒阈值） |
| `core/asr/faster_whisper.py` | `--max_gap` 收紧、float16 修复 |
| `ui/thread/subtitle_thread.py` | 语言后缀 SRT 输出 |
| `core/translate/bing_translator.py` | Bing 免费接口重写 |
| `core/translate/google_translator.py` | Google 免费接口重写 |
| `ui/common/config.py` | 新增分割类型、停顿阈值等配置项 |

完整清单见补丁文件。

---

## 许可

本项目遵循 **GPL-3.0**，与原项目一致。

- 原始代码版权归 [WEIFENG2333](https://github.com/WEIFENG2333) 所有
- 本分支的修改同样以 GPL-3.0 发布
- 完整源码见本仓库；二进制包见 Releases

原项目：https://github.com/WEIFENG2333/VideoCaptioner

---

## 免责声明

这是个人维护的非官方修改版，不提供任何担保。使用前请先备份重要素材。
如遇到与原版相关的功能问题，请优先向[原项目](https://github.com/WEIFENG2333/VideoCaptioner)反馈。
