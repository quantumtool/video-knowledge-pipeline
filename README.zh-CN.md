# 视频知识提取：可审计流程与案例

这是一个用于教学视频的可复现预处理流程，包含视频元数据读取、按源时间戳解码帧、可选近重复筛选、字幕对齐、音轨导出，以及可选的本地 Ollama 视觉语言模型分析。

本项目不声称“逐帧截图就能无损学习”，也不声称所有通用大模型采用相同的视频处理方式。保存全部解码帧为 PNG，只能表示保存了该解码器输出的像素；采样、近重复筛选和模型回答是否保留了某项知识，必须按具体任务评测。

## 研究范围

随附案例来自用户提供的 Illustrator 回形纹教程筛帧记录：50 个候选采样时点中人工保留了 17 个阶段帧，即候选集保留率 34%、减少率 66%。这些数字只描述这份人工筛选记录。项目没有原视频文件、全部候选图像、音频转写或模型评测，因此不能据此判断筛选有没有丢失知识，也不能证明算法有效。

相关研究已经覆盖视频知识评测、高帧率处理和关键帧选择。论文把贡献限定为可审计的数据流程、评测协议和单例演示，不把这些已存在方向说成新发现。

## 安装

需要 Python 3.10+、FFmpeg 和 FFprobe。视觉语言模型分析还需要本地 Ollama 服务；音频转写需要单独选择 ASR 工具。

~~~bash
python -m pip install -e .
vkx --help
~~~

## 基本用法

先检查帧率、码率、时长和流信息：

~~~bash
vkx probe lesson.mp4
~~~

解码所有源帧、保留 PNG，并记录时间戳：

~~~bash
vkx extract lesson.mp4 --out runs/lesson
~~~

需要与稀疏采样对照时，可指定每秒采样数；默认模式会解码源视频的每一帧：

~~~bash
vkx extract lesson.mp4 --out runs/lesson-1fps --sample-fps 1
~~~

可选启用感知哈希去重，但原始解码帧仍保留：

~~~bash
vkx extract lesson.mp4 --out runs/lesson-dedup --dedupe-hamming 4
~~~

如果已有 SRT 或 WebVTT 字幕，可按时间戳对齐：

~~~bash
vkx extract lesson.mp4 --out runs/lesson-asr --transcript lesson.vtt
~~~

## 主要产物

- manifest.json：帧时间戳、采样方式、文件哈希和筛选结果
- frames/：完整解码帧的 PNG 文件
- transcript_alignment.csv：每帧对应的重叠或邻近字幕片段
- knowledge.json：可选本地模型的回答；需要人工核对证据

## 论文

英文 LaTeX 稿件在 paper/main.tex。稿件报告了筛帧记录中的描述性统计，并提出后续可执行的对照实验方案；它没有伪造模型准确率或知识留存结果。

示例目录不包含 B 站视频或视频截图，只保留用户提供的时间戳与筛选状态记录。原视频及其视觉内容的权利仍归原作者或相应权利人所有。
