#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
视频批量命名工具 v3.5.6 by:ooouuuiii
=====================
基于本地 Ollama 多模态模型（如 Gemma3 / Qwen2.5-VL），智能分析视频并批量重命名。

流程：选择视频 -> 智能抽帧 -> 模型分析帧图片 -> 生成标准命名 -> 复制到输出目录
命名格式：【序号】亮／暗＿景别＿事件描述
示例：【1】亮／内＿中景＿打电话
"""

import os
import sys
import json
import time
import shutil
import logging
import subprocess
import tempfile
import threading
import traceback
import re
import base64
from pathlib import Path
from datetime import datetime
from typing import Optional

import requests
import gradio as gr

# ═══════════════════════════════════════════════════════════════
# 全局配置
# ═══════════════════════════════════════════════════════════════

OLLAMA_URL = "http://127.0.0.1:11434"
DEFAULT_MODEL = "gemma3:4b"
FRAME_EVERY_N = 5      # 每5帧抽1帧
MAX_FRAMES_TO_MODEL = 8
REQUEST_TIMEOUT = 300
CONNECT_TIMEOUT = 10
SUPPORTED_EXTS = {".mp4", ".avi", ".mkv", ".mov", ".wmv", ".flv", ".webm", ".m4v", ".mts", ".m2ts"}
_stop_event = threading.Event()


# ═══════════════════════════════════════════════════════════════
# 日志
# ═══════════════════════════════════════════════════════════════

def setup_logger():
    logger = logging.getLogger("VNT")
    logger.setLevel(logging.DEBUG)
    if logger.handlers:
        return logger
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
    # 控制台输出 DEBUG 级别（让 CMD 窗口看到所有细节）
    fh = logging.StreamHandler(sys.stdout)
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)
    logger.addHandler(fh)
    # 文件输出 DEBUG 级别
    fh2 = logging.FileHandler(Path(__file__).parent / "video_naming_tool.log", encoding="utf-8")
    fh2.setLevel(logging.DEBUG)
    fh2.setFormatter(fmt)
    logger.addHandler(fh2)
    return logger

logger = setup_logger()


# ═══════════════════════════════════════════════════════════════
# 智能抽帧
# ═══════════════════════════════════════════════════════════════

def extract_frames(video_path: str, output_dir: str, every_n: int = FRAME_EVERY_N) -> list[str]:
    """
    使用 FFmpeg 从视频中每隔 N 帧抽1帧（默认每5帧抽1帧）。
    FFmpeg 支持几乎所有视频格式（MKV/H.265/AV1 等），远优于 OpenCV。
    """
    os.makedirs(output_dir, exist_ok=True)
    name = os.path.basename(video_path)

    # 检查 FFmpeg 是否可用
    ffmpeg_path = "ffmpeg"
    try:
        subprocess.run([ffmpeg_path, "-version"], capture_output=True, timeout=5)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        logger.error("未找到 FFmpeg，请先安装：https://ffmpeg.org/download.html")
        return []

    # 构建命令：ffmpeg -i input -vf fps=1 -q:v 2 output_dir/frame_%06d.jpg
    cmd = [
        ffmpeg_path,
        "-y",                              # 覆盖输出
        "-i", video_path,                   # 输入视频
        "-vf", f"select='not(mod(n\\,{every_n}))'",  # 每隔 N 帧抽1帧
        "-q:v", "5",                        # JPEG 质量（中等，节省空间）
        os.path.join(output_dir, "frame_%06d.jpg"),
    ]

    logger.info(f"FFmpeg 抽帧: {name}")
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=300,
        )
        if result.returncode != 0:
            logger.error(f"FFmpeg 抽帧失败 ({name}): {result.stderr[-500:]}")
            return []
    except subprocess.TimeoutExpired:
        logger.error(f"FFmpeg 抽帧超时 ({name})")
        return []
    except Exception as e:
        logger.error(f"FFmpeg 抽帧异常 ({name}): {e}")
        return []

    # 收集生成的帧
    paths = sorted([
        os.path.join(output_dir, f)
        for f in os.listdir(output_dir)
        if f.lower().endswith((".jpg", ".jpeg", ".png"))
    ])
    logger.info(f"抽帧完成: {name} -> {len(paths)} 帧")
    return paths


def pick_representative(frames: list[str], max_n: int = MAX_FRAMES_TO_MODEL) -> list[str]:
    """从所有帧中均匀选取代表帧（始终包含首尾帧）。"""
    if not frames:
        return []
    if len(frames) <= max_n:
        return list(frames)
    selected = [frames[0], frames[-1]]
    middle = frames[1:-1]
    slots = max_n - 2
    if middle and slots > 0:
        step = len(middle) / (slots + 1)
        for i in range(slots):
            selected.append(middle[min(int((i + 1) * step), len(middle) - 1)])
    selected.sort()
    return selected


def cleanup_dir(d: str):
    """删除目录中的所有文件和目录本身。"""
    if not os.path.exists(d):
        return
    try:
        n = 0
        for f in os.listdir(d):
            p = os.path.join(d, f)
            if os.path.isfile(p):
                os.remove(p)
                n += 1
        os.rmdir(d)
        logger.info(f"清理: 删除 {n} 个临时文件")
    except Exception as e:
        logger.warning(f"清理异常: {e}")


# ═══════════════════════════════════════════════════════════════
# Prompt 与 JSON 解析
# ═══════════════════════════════════════════════════════════════

PROMPT = """你是一位专业影视分析师。请观察以下从同一视频中按时间顺序提取的截图，分析视频内容。

判断标准：
- time_of_day（亮/暗）：画面明亮为"亮"，画面偏暗为"暗"
- location（内/外）：室内环境为"内"，室外开阔场景为"外"
- shot_type：远景(人物很小)/全景(全身可见)/中景(膝盖以上)/近景(胸部以上)/特写(面部)
- scene_action：用最简短的词语描述连续帧中呈现的元素、人物运动或发生的事件。省略主语，直接描述动作或状态，不超过30个字。如"打电话""走廊里快步走""雨中撑伞前行""翻阅桌上文件""窗边凝视远方"

严格输出JSON，不要输出其他内容：
{"time_of_day":"亮","location":"内","shot_type":"中景","scene_action":"打电话"}"""


def parse_json(text: str) -> Optional[dict]:
    """从模型文本中提取JSON（多级容错）。"""
    if not text:
        return None
    text = text.strip()
    # 尝试直接解析
    try:
        r = json.loads(text)
        if isinstance(r, dict):
            return validate(r)
    except json.JSONDecodeError:
        pass
    # 提取 ```json ... ```
    for m in re.findall(r"```(?:json)?\s*\n?(.*?)\n?\s*```", text, re.DOTALL):
        try:
            r = json.loads(m.strip())
            if isinstance(r, dict):
                return validate(r)
        except json.JSONDecodeError:
            continue
    # 提取 { ... }
    for m in re.findall(r"\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}", text, re.DOTALL):
        try:
            r = json.loads(m)
            if isinstance(r, dict):
                return validate(r)
        except json.JSONDecodeError:
            continue
    # 最后尝试：找第一个 { 到最后一个 }
    i, j = text.find("{"), text.rfind("}")
    if i != -1 and j > i:
        try:
            r = json.loads(text[i:j+1])
            if isinstance(r, dict):
                return validate(r)
        except json.JSONDecodeError:
            pass
    logger.error(f"JSON解析失败: {text[:200]}")
    return None


def validate(r: dict) -> dict:
    """验证并规范化字段，缺失字段用默认值。"""
    mapping = {
        "time_of_day": (["time_of_day", "time", "时间"], {"亮", "暗"}, "亮"),
        "location": (["location", "场景"], {"内", "外"}, "内"),
        "shot_type": (["shot_type", "shot", "景别"], {"远景", "全景", "中景", "近景", "特写"}, "中景"),
        "scene_action": (["scene_action", "character_action", "action", "动作"], None, "未知"),
    }
    out = {}
    for key, (aliases, valid_set, default) in mapping.items():
        v = None
        for a in aliases:
            if a in r:
                v = str(r[a]).strip()
                break
        if v is None:
            out[key] = default
        elif valid_set and v not in valid_set:
            out[key] = default
        elif key == "scene_action":
            out[key] = v[:30]
        else:
            out[key] = v
    return out


# ═══════════════════════════════════════════════════════════════
# Ollama 调用
# ═══════════════════════════════════════════════════════════════

class OllamaClient:
    """Ollama API 客户端，封装连接检测和视觉模型调用。"""

    def __init__(self, base_url: str = OLLAMA_URL):
        self.url = base_url.rstrip("/")
        self.session = requests.Session()
        self._connect()
        self.model = ""
        self._models = []

    def _connect(self):
        """检测 Ollama 是否可用。"""
        try:
            resp = self.session.get(f"{self.url}/api/tags", timeout=CONNECT_TIMEOUT)
            resp.raise_for_status()
            self._models = [m.get("name", "") for m in resp.json().get("models", [])]
            if self._models:
                logger.info(f"Ollama 连接成功，可用模型: {self._models}")
            else:
                # 连接成功但模型列表为空（部分 Ollama 版本/Windows 会出现）
                # 不阻断，让后续实际调用时再判断模型是否存在
                logger.warning("Ollama 连接成功，但模型列表为空，将跳过模型预检查")
                logger.debug(f"原始响应: {resp.text[:500]}")
        except requests.exceptions.ConnectionError:
            raise ConnectionError(
                f"无法连接 Ollama ({self.url})。\n"
                "请确认 Ollama 正在运行，在终端执行:\n  ollama serve"
            )
        except requests.exceptions.MissingSchema:
            raise ConnectionError(
                f"URL 格式错误: {self.url}\n"
                "需要 http:// 或 https:// 前缀"
            )
        except requests.exceptions.Timeout:
            raise ConnectionError("连接 Ollama 超时。")
        except Exception as e:
            raise ConnectionError(f"Ollama 连接失败: {e}")

    def set_model(self, name: str):
        """选择要使用的模型。如果模型列表为空则跳过检查，直接使用。"""
        name = name.strip()
        self.model = name
        if self._models:
            found = any(name in m or m in name for m in self._models)
            if not found:
                raise RuntimeError(
                    f"模型 '{name}' 未找到。\n"
                    f"可用模型: {', '.join(self._models)}\n"
                    f"请执行: ollama pull {name}"
                )
        logger.info(f"使用模型: {self.model}（跳过预检查）")

    def analyze_frames(self, image_paths: list[str]) -> Optional[dict]:
        """将图片列表发送给模型，返回分析结果字典。"""
        # 编码图片
        b64_list = []
        for p in image_paths:
            with open(p, "rb") as f:
                b64_list.append(base64.b64encode(f.read()).decode())
            # 释放内存
            os.remove(p)

        logger.info(f"调用模型 {self.model}，{len(b64_list)} 张图片")

        payload = {
            "model": self.model,
            "messages": [{
                "role": "user",
                "content": PROMPT,
                "images": b64_list,
            }],
            "stream": False,
            "options": {"temperature": 0.1, "num_predict": 512},
        }

        try:
            resp = self.session.post(
                f"{self.url}/api/chat",
                json=payload,
                timeout=REQUEST_TIMEOUT,
            )
            resp.raise_for_status()
            content = resp.json().get("message", {}).get("content", "").strip()
            if not content:
                logger.error("模型返回空内容")
                return None
            logger.info(f"模型响应: {content[:120]}")
            return parse_json(content)
        except requests.exceptions.Timeout:
            logger.error("模型推理超时（可能GPU内存不足）")
            return None
        except Exception as e:
            logger.error(f"模型调用失败: {e}")
            return None


# ═══════════════════════════════════════════════════════════════
# 核心处理
# ═══════════════════════════════════════════════════════════════

def natural_sort_key(s: str) -> list:
    """自然排序键：让数字按数值排序，而非字符串排序。
    例如：file2.mp4 排在 file10.mp4 前面。
    """
    import re
    return [int(c) if c.isdigit() else c.lower() for c in re.split(r'(\d+)', os.path.basename(s))]


def sanitize(name: str) -> str:
    """清理文件名非法字符。"""
    name = re.sub(r'[<>:"/\\|?*\n\r\t\x00-\x1f]', '_', name)
    name = name.strip(" .")
    return name[:200] if name else "unnamed"


def make_filename(result: dict, seq: int) -> str:
    """生成格式化文件名。格式：【序号】亮／暗＿景别＿事件描述"""
    t = result.get("time_of_day", "亮")
    l = result.get("location", "内")
    s = result.get("shot_type", "中景")
    a = sanitize(result.get("scene_action", "未知"))
    return f"【{seq}】{t}／{l}＿{s}＿{a}"


def process_one_video(client: OllamaClient, video_path: str, frame_base_dir: str) -> Optional[dict]:
    """
    处理单个视频：抽帧 -> 选代表帧 -> 模型分析 -> 清理临时文件。
    返回分析结果字典，失败返回 None。
    """
    name = os.path.basename(video_path)
    # 为该视频创建专属临时目录
    tmp_dir = os.path.join(
        frame_base_dir,
        f"_vnt_{os.path.splitext(name)[0]}_{int(time.time())}"
    )

    try:
        os.makedirs(tmp_dir, exist_ok=True)

        # 第一步：抽帧
        frames = extract_frames(video_path, tmp_dir)
        if not frames:
            logger.error(f"抽帧失败: {name}")
            return None

        # 第二步：选取代表帧
        reps = pick_representative(frames)
        logger.info(f"选取 {len(reps)} 个代表帧")

        # 第三步：调用模型（analyze_frames 内部会删除已发送的图片）
        result = client.analyze_frames(reps)
        if result:
            logger.info(f"分析成功: {name} -> {result}")
        else:
            logger.error(f"分析失败: {name}")
        return result

    except Exception as e:
        logger.error(f"处理异常 {name}: {type(e).__name__}: {e}")
        logger.debug(traceback.format_exc())
        return None

    finally:
        # 无论如何清理该视频的所有临时文件
        cleanup_dir(tmp_dir)


# ═══════════════════════════════════════════════════════════════
# UI 日志收集器
# ═══════════════════════════════════════════════════════════════

class LogBuffer:
    def __init__(self):
        self._lines = []
        self._lock = threading.Lock()

    def add(self, msg):
        ts = datetime.now().strftime("%H:%M:%S")
        with self._lock:
            self._lines.append(f"[{ts}] {msg}")

    def text(self):
        with self._lock:
            return "\n".join(self._lines)

    def clear(self):
        with self._lock:
            self._lines.clear()

_log = LogBuffer()


# ═══════════════════════════════════════════════════════════════
# Gradio UI
# ═══════════════════════════════════════════════════════════════

CSS = """
/* 全屏自适应宽度 */
.gradio-container {
    max-width: 100% !important;
    padding: 20px !important;
}

/* 顶部标题 */
.main-title {
    text-align: center;
    margin-bottom: 4px !important;
}
.main-title h1 {
    font-size: 28px !important;
    margin-bottom: 2px !important;
}
.main-desc {
    text-align: center;
    color: #666;
    font-size: 14px;
    margin-bottom: 16px;
}

/* 配置卡片 */
.config-card {
    background: #fafbfc;
    border: 1px solid #e2e8f0;
    border-radius: 12px;
    padding: 20px;
    margin-bottom: 12px;
}
.config-card .svelte-19lrwbu {
    font-size: 14px;
    font-weight: 600;
    color: #334155;
    margin-bottom: 8px;
}

/* 文件上传区域固定高度+滚动条 */
.file-upload-scroll {
    max-height: 220px !important;
    overflow-y: auto !important;
}
.file-upload-scroll * {
    max-height: none !important;
}
.status-box {
    background: linear-gradient(135deg, #f0f4ff 0%, #e8f0fe 100%);
    border: 1px solid #c5d5f0;
    border-radius: 10px;
    padding: 14px 20px;
    font-family: 'Consolas', 'Monaco', monospace;
    font-size: 14px;
    letter-spacing: 0.3px;
}

/* 日志区 */
.log-box {
    font-family: 'Consolas', 'Monaco', 'Courier New', monospace;
    font-size: 12px;
    line-height: 1.6;
    max-height: 350px;
    min-height: 200px;
    overflow-y: auto;
    background: #1a1a2e;
    color: #a8b2d1;
    border-radius: 10px;
    padding: 16px;
    white-space: pre-wrap;
    word-break: break-all;
    border: 1px solid #2d2d5e;
}

/* 进度条 */
.progress-box .svelte-19lrwbu {
    font-size: 14px;
    font-weight: 600;
}

/* 按钮组 */
.btn-group {
    display: flex;
    gap: 12px;
    justify-content: center;
}

/* 结果表格 */
.result-table {
    font-size: 13px;
    border-radius: 8px;
    overflow: hidden;
}

/* 区域标题 */
.section-title {
    font-size: 16px;
    font-weight: 600;
    color: #334155;
    margin-top: 16px;
    margin-bottom: 8px;
    padding-left: 4px;
    border-left: 3px solid #3b82f6;
}

/* 分割线 */
.divider {
    border: none;
    border-top: 1px solid #e2e8f0;
    margin: 12px 0;
}
"""


def build_ui():
    with gr.Blocks(title="砍柴视频批量命名工具", theme=gr.themes.Soft(), css=CSS) as app:

        # ── 标题 ──
        gr.HTML("""
        <div class="main-title">
            <h1>🎬 砍柴视频批量命名工具 By:ooouuuiii</h1>
        </div>
        <div class="main-desc">
            基于 Ollama 多模态模型，智能分析视频内容并生成标准命名 &nbsp;|&nbsp;
            格式示例：<code>【1】亮／内＿中景＿打电话</code>
        </div>
        """)

        # ── 文件选择区 ──
        gr.HTML('<div class="section-title">📁 文件选择</div>')
        videos = gr.File(label="视频文件（支持多选，MP4/MKV/AVI/MOV 等）",
                        file_count="multiple", file_types=list(SUPPORTED_EXTS),
                        elem_classes=["file-upload-scroll"])

        # ── 参数配置区 ──
        gr.HTML('<div class="section-title">⚙️ 参数配置</div>')
        with gr.Group(elem_classes=["config-card"]):
            with gr.Row(equal_height=True):
                model_name = gr.Textbox(label="Ollama 模型", value=DEFAULT_MODEL,
                                       placeholder="gemma3:4b / qwen2.5vl:3b")
                start_num = gr.Number(label="起始序号", value=1, precision=0, minimum=1)
            with gr.Row(equal_height=True):
                frame_dir = gr.Textbox(label="抽帧临时目录（留空=系统临时目录）",
                                      placeholder="D:\\temp\\frames")
                output_dir = gr.Textbox(label="结果保存目录（必填）",
                                       placeholder="D:\\output")

        # ── 操作按钮 ──
        with gr.Row(elem_classes=["btn-group"]):
            btn_start = gr.Button("▶  开始处理", variant="primary", size="lg", min_width=180)
            btn_stop = gr.Button("⏹  停止处理", variant="stop", size="lg", min_width=180)

        # ── 进度与状态 ──
        gr.HTML('<div class="section-title">📊 处理进度</div>')
        status = gr.Textbox(value="等待开始...", interactive=False,
                            elem_classes=["status-box"], show_label=False, lines=2)
        progress = gr.Slider(label="进度", minimum=0, maximum=100, value=0, interactive=False,
                             elem_classes=["progress-box"])

        # ── 结果表格 ──
        gr.HTML('<div class="section-title">📋 处理结果</div>')
        table = gr.Dataframe(headers=["序号", "原文件名", "生成命名", "状态", "目标路径"],
                             interactive=False, value=[], elem_classes=["result-table"],
                             wrap=True, max_height=300)

        # ── 日志 ──
        gr.HTML('<div class="section-title">📝 实时日志</div>')
        log_box = gr.Textbox(value="", interactive=False,
                             elem_classes=["log-box"], show_label=False, lines=12)

        state = gr.State({"busy": False})

        # ── 生成器：每处理完一个视频 yield 一次 ──
        def run(video_files, start, model, fdir, odir, st):
            def ui(s, p, t):
                return s, p, t, _log.text(), st

            if st.get("busy"):
                yield ui("⚠ 正在处理中", 0, [])
                return

            # 验证输入
            if not video_files:
                _log.add("❌ 请选择视频文件")
                yield ui("❌ 请选择视频文件", 0, [])
                return
            if not odir or not odir.strip():
                _log.add("❌ 请填写结果保存目录")
                yield ui("❌ 请填写结果保存目录", 0, [])
                return

            odir = odir.strip()
            try:
                os.makedirs(odir, exist_ok=True)
            except Exception:
                _log.add(f"❌ 输出目录无效: {odir}")
                yield ui(f"❌ 输出目录无效: {odir}", 0, [])
                return

            # 获取有效视频路径
            valid = []
            for f in video_files:
                p = f.name if hasattr(f, "name") else str(f)
                if os.path.isfile(p) and os.path.splitext(p)[1].lower() in SUPPORTED_EXTS:
                    valid.append(p)
                else:
                    _log.add(f"⚠ 跳过: {os.path.basename(p)}")

            if not valid:
                _log.add("❌ 没有有效视频")
                yield ui("❌ 没有有效视频", 0, [])
                return

            # 按文件名自然排序（数字按数值排，如 2排在10前面）
            valid.sort(key=natural_sort_key)

            _log.clear()
            _stop_event.clear()
            st["busy"] = True
            _log.add(f"🚀 共 {len(valid)} 个视频，模型: {model.strip()}")
            _log.add(f"📂 文件顺序: {[os.path.basename(v) for v in valid]}")
            yield ui(f"正在连接 Ollama ({OLLAMA_URL})...", 0, [])

            # 连接 Ollama
            try:
                client = OllamaClient(OLLAMA_URL)
                client.set_model(model.strip())
            except (ConnectionError, RuntimeError) as e:
                _log.add(f"❌ {e}")
                st["busy"] = False
                yield ui(str(e), 0, [])
                return

            _log.add(f"✅ 模型就绪: {client.model}")
            yield ui(f"✅ 模型就绪，开始处理 {len(valid)} 个视频", 0, [])

            # 确定抽帧目录
            fdir_clean = fdir.strip() if fdir and fdir.strip() else tempfile.gettempdir()

            results = []
            ok = 0
            fail = 0
            total = len(valid)

            for i, vp in enumerate(valid):
                if _stop_event.is_set():
                    _log.add("⏹ 用户停止")
                    break

                seq = int(start) + i
                vname = os.path.basename(vp)

                _log.add(f"\n[{i+1}/{total}] {vname}")
                yield ui(f"⏳ ({i+1}/{total}) {vname} — 抽帧中...",
                         int(i / total * 100),
                         [[r[0],r[1],r[2],r[3],r[4]] for r in results])

                # 处理视频
                try:
                    analysis = process_one_video(client, vp, fdir_clean)

                    if analysis:
                        new_name = make_filename(analysis, seq)
                        ext = os.path.splitext(vp)[1]
                        target = os.path.join(odir, new_name + ext)
                        # 处理重名
                        c = 1
                        while os.path.exists(target):
                            target = os.path.join(odir, f"{new_name}({c}){ext}")
                            c += 1
                        shutil.copy2(vp, target)
                        ok += 1
                        r = [seq, vname, new_name + ext, "✅ 成功", target]
                        _log.add(f"   ✅ -> {new_name}{ext}")
                    else:
                        fail += 1
                        r = [seq, vname, "分析失败", "❌ 失败", ""]

                except Exception as e:
                    fail += 1
                    r = [seq, vname, "处理异常", "❌ 失败", ""]
                    _log.add(f"   ❌ {type(e).__name__}: {e}")

                results.append(r)

                pct = int((i + 1) / total * 100)
                stxt = f"进度 {i+1}/{total} | ✅ {ok} | ❌ {fail}"
                _log.add(stxt)
                yield ui(stxt, pct, list(results))

            st["busy"] = False
            if _stop_event.is_set():
                final = f"⏹ 已停止 | ✅{ok} ❌{fail}"
            else:
                final = f"✅ 全部完成 | ✅{ok} | ❌{fail} | 共{len(results)}"
            _log.add(final)
            yield ui(final, 100, list(results))

        def stop(st):
            _stop_event.set()
            st["busy"] = False
            return "⏹ 正在停止...", gr.update(), gr.update(), _log.text(), st

        btn_start.click(fn=run,
                        inputs=[videos, start_num, model_name, frame_dir, output_dir, state],
                        outputs=[status, progress, table, log_box, state])
        btn_stop.click(fn=stop,
                       inputs=[state],
                       outputs=[status, progress, table, log_box, state])

    return app


if __name__ == "__main__":
    logger.info(f"启动服务 Ollama={OLLAMA_URL} 模型={DEFAULT_MODEL}")
    app = build_ui()
    app.launch(server_name="0.0.0.0", server_port=7860, inbrowser=True)
