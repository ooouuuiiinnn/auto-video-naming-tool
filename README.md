# 🎬 视频批量命名工具

基于本地 Ollama 多模态大模型，智能分析视频内容并批量生成标准化文件名。

## 命名格式

```
【序号】亮／暗＿景别＿事件描述
```

**示例：**

```
【1】亮／内＿中景＿打电话
【2】暗／外＿全景＿雨中撑伞前行
【3】亮／内＿近景＿翻阅桌上文件
```

| 字段 | 取值 | 说明 |
|------|------|------|
| 亮/暗 | 亮、暗 | 画面明暗程度 |
| 内/外 | 内、外 | 室内/室外场景 |
| 景别 | 远景、全景、中景、近景、特写 | 镜头景别 |
| 事件描述 | 自由文本（≤30字） | 帧中呈现的动作或事件，省略主语 |

## 工作原理

```
选择视频文件 → FFmpeg 每5帧抽取1帧 → 均匀选取最多8张代表帧 → Ollama 多模态模型分析 → 生成标准化命名 → 复制到输出目录
```

1. **抽帧**：使用 FFmpeg 从视频中每隔 5 帧抽取 1 帧（支持 MKV/H.265/AV1 等所有格式）
2. **选帧**：从抽取的帧中均匀选取最多 8 张代表帧（始终包含首尾帧）
3. **分析**：将代表帧发送给本地 Ollama 多模态模型进行内容识别
4. **命名**：根据分析结果生成标准命名，复制原视频到输出目录并重命名
5. **清理**：自动删除临时抽帧文件

## 环境要求

- **Python** >= 3.10
- **Ollama** >= 0.1（本地运行，[安装指南](https://ollama.ai)）
- **FFmpeg**（系统工具，需在 PATH 中）
- **操作系统**：Windows / macOS / Linux

### 支持的视频格式

`.mp4` `.avi` `.mkv` `.mov` `.wmv` `.flv` `.webm` `.m4v` `.mts` `.m2ts`

### 推荐模型

- `gemma3:4b`（默认，显存需求低）
- `qwen2.5vl:3b`
- 其他支持视觉输入的 Ollama 模型

## 快速开始

### 1. 安装 Ollama 并拉取模型

```bash
# 安装 Ollama（参考 https://ollama.ai）
# 拉取多模态模型
ollama pull gemma3:4b
```

### 2. 安装 FFmpeg

```bash
# Windows: 下载 https://ffmpeg.org/download.html 并添加到 PATH
# macOS:
brew install ffmpeg
# Ubuntu:
sudo apt install ffmpeg
```

### 3. 安装 Python 依赖

```bash
pip install -r requirements.txt
```

### 4. 启动

```bash
# 方式一：直接运行
python main.py

# 方式二（Windows）：双击 启动工具.bat，可查看实时日志
```

启动后浏览器会自动打开 `http://localhost:7860`。

## 使用说明

1. **选择视频**：点击文件选择区域上传多个视频文件
2. **配置参数**：
   - `Ollama 模型`：填入已拉取的模型名称（默认 `gemma3:4b`）
   - `起始序号`：命名从第几个开始编号（默认 1）
   - `抽帧临时目录`：留空则使用系统临时目录
   - `结果保存目录`：必填，重命名后的视频会复制到此目录
3. **开始处理**：点击"开始处理"，实时查看进度和日志
4. **停止处理**：随时可点击"停止处理"中断任务

## 项目结构

```
video-naming-tool/
├── main.py              # 主程序（Gradio WebUI + 核心逻辑）
├── requirements.txt     # Python 依赖（gradio、requests）
├── 启动工具.bat          # Windows 一键启动（含日志窗口）
├── video_naming_tool.log # 运行日志（自动生成）
└── README.md
```

## 配置参数

可在 `main.py` 顶部修改全局配置：

```python
OLLAMA_URL = "http://127.0.0.1:11434"  # Ollama 服务地址
DEFAULT_MODEL = "gemma3:4b"            # 默认模型
FRAME_EVERY_N = 5                      # 每 N 帧抽取 1 帧
MAX_FRAMES_TO_MODEL = 8                # 最多发送给模型的帧数
REQUEST_TIMEOUT = 300                  # 模型推理超时（秒）
```

## 注意事项

- Ollama 需要在后台运行（`ollama serve`）
- 视频越大、帧数越多，处理时间越长
- 模型需要支持视觉输入（multimodal），纯文本模型不可用
- 原始视频不会被修改，工具以**复制**方式输出到目标目录
- 文件名中的斜杠使用全角字符 `／`（U+FF0F），避免 Windows 路径解析问题
- 处理过程中产生的临时帧文件会在每个视频处理完成后自动清理

## License

MIT
