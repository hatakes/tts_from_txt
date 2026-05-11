#!/usr/bin/env python3

"""
Author: Sean Zhang
Date: 2026-03-15
Description: TTS batch generation script (MLX-based)
"""

import argparse
import audioop
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
import wave
from pathlib import Path

import chardet

from monitor_logger import *

# ===== 路径与配置 =====

BASE_DIR = Path(__file__).resolve().parent
CONFIG_FILE = BASE_DIR / "config" / "default_config.json"


def load_app_config():
    if not CONFIG_FILE.exists():
        return {}

    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as exc:
        logging.warning(f"⚠️ 读取配置失败，将使用脚本内默认值: {exc}")
        return {}


APP_CONFIG = load_app_config()


def resolve_path(path_value):
    # 配置里允许写相对路径，统一相对脚本根目录解析，避免从不同 cwd 启动时漂移。
    path = Path(path_value)
    if path.is_absolute():
        return path
    return BASE_DIR / path

# ===== 初始化日志 =====

init_logger(str(BASE_DIR / "tts_run.log"))

start_monitor(interval=10)

# ===== 默认参数 =====

DEFAULT_MODEL = APP_CONFIG.get("model", "")
DEFAULT_VOICE = APP_CONFIG.get("voice", "serena")
DEFAULT_LANG = APP_CONFIG.get("lang", "zh")
DEFAULT_CFG_SCALE = str(APP_CONFIG.get("cfg_scale", "1.2"))
DEFAULT_ENABLE_SFX = bool(APP_CONFIG.get("enable_sfx", True))

CHUNK_SIZE = 180
TARGET_CHANNELS = 1
TARGET_SAMPLE_WIDTH = 2
TARGET_SAMPLE_RATE = 24000
MIN_SPLIT_SEARCH = 60
MAX_SFX_DURATION_SEC = 2.2
SFX_FADE_OUT_SEC = 0.12
SFX_GAIN = 0.35

# 本地音效素材目录
SFX_LIBRARY_DIR = str(resolve_path(APP_CONFIG.get("sfx_dir", "sfx_library")))
SFX_CONFIG_FILE = resolve_path(APP_CONFIG.get("sfx_config_file", "config/sfx_config.json"))

# 续传状态文件
RESUME_STATE_FILE = str(resolve_path(APP_CONFIG.get("resume_state_file", "tts_resume_state.json")))


# ===============================
# 续传状态管理
# ===============================

def load_resume_state():
    """加载续传状态"""
    if os.path.exists(RESUME_STATE_FILE):
        try:
            with open(RESUME_STATE_FILE, "r", encoding="utf-8") as f:
                state = json.load(f)
                logging.info(f"📂 加载续传状态: {state}")
                return state
        except Exception as exc:
            logging.warning(f"⚠️ 加载续传状态失败: {exc}")
    return {}


def save_resume_state(state):
    """保存续传状态"""
    try:
        with open(RESUME_STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except Exception as exc:
        logging.error(f"❌ 保存续传状态失败: {exc}")


def make_resume_key(rel_path):
    """为续传状态生成稳定且唯一的键，避免同名 txt 冲突。"""
    normalized = os.path.normpath(rel_path)
    return normalized.replace("\\", "/")


def compute_text_hash(text):
    """对清理后的文本计算稳定 hash，用于判断 txt 内容是否变化。"""
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def normalize_resume_entry(entry):
    """兼容旧版纯 segment 格式，并统一为结构化状态。"""
    if isinstance(entry, int):
        return {"segment": entry}
    if isinstance(entry, dict):
        normalized = {}
        segment = entry.get("segment")
        if isinstance(segment, int):
            normalized["segment"] = segment
        text_hash = entry.get("text_hash")
        if isinstance(text_hash, str) and text_hash:
            normalized["text_hash"] = text_hash
        return normalized
    return {}


def get_resume_entry(state, state_key, legacy_state_key=None):
    """读取续传条目，并在需要时迁移旧 key / 旧格式。"""
    if state is None:
        return None

    source_key = None
    if state_key in state:
        source_key = state_key
    elif legacy_state_key and legacy_state_key in state:
        source_key = legacy_state_key

    if source_key is None:
        return None

    normalized_entry = normalize_resume_entry(state[source_key])
    changed = state[source_key] != normalized_entry

    # 兼容旧版只用 base_name 做键的状态文件；读到后立即迁移，后续就只维护新键。
    if source_key != state_key:
        state[state_key] = normalized_entry
        del state[source_key]
        changed = True
        logging.info(f"🔁 已迁移旧续传键: {source_key} -> {state_key}")
    elif changed:
        state[state_key] = normalized_entry

    if changed:
        save_resume_state(state)

    return normalized_entry


def set_resume_entry(state, state_key, current_segment, text_hash=None):
    """写入结构化续传状态。"""
    if state is None:
        return

    entry = {"segment": current_segment}
    if text_hash:
        entry["text_hash"] = text_hash
    state[state_key] = entry
    save_resume_state(state)


# ===============================
# 拟声词 → 本地音效文件映射
# ===============================


def load_sfx_config():
    if not SFX_CONFIG_FILE.exists():
        raise FileNotFoundError(f"SFX 配置文件不存在: {SFX_CONFIG_FILE}")

    with open(SFX_CONFIG_FILE, "r", encoding="utf-8") as f:
        raw_config = json.load(f)

    sfx_to_file = raw_config.get("sfx_to_file", {})
    non_speech_keys = raw_config.get("non_speech_sfx_keys", [])
    raw_patterns = raw_config.get("sfx_patterns", [])

    if not isinstance(sfx_to_file, dict) or not sfx_to_file:
        raise ValueError("sfx_to_file 必须是非空对象")
    if not isinstance(non_speech_keys, list):
        raise ValueError("non_speech_sfx_keys 必须是数组")
    if not isinstance(raw_patterns, list):
        raise ValueError("sfx_patterns 必须是数组")

    patterns = []
    for item in raw_patterns:
        if not isinstance(item, dict):
            raise ValueError("sfx_patterns 的每个项目都必须是对象")

        pattern = item.get("pattern")
        sfx_key = item.get("sfx_key")
        if not isinstance(pattern, str) or not pattern:
            raise ValueError("sfx_patterns[].pattern 必须是非空字符串")
        if sfx_key is not None and not isinstance(sfx_key, str):
            raise ValueError("sfx_patterns[].sfx_key 必须是字符串或 null")

        # 运行时只消费 (pattern, key) 元组，配置文件结构化仅用于便于扩展和手工维护。
        patterns.append((pattern, sfx_key))

    return sfx_to_file, set(non_speech_keys), patterns


try:
    SFX_TO_FILE, NON_SPEECH_SFX_KEYS, SFX_PATTERNS = load_sfx_config()
except Exception as exc:
    raise RuntimeError(f"加载 SFX 配置失败: {exc}") from exc


# ===============================
# wav 工具
# ===============================

def get_wav_duration(path):
    if not os.path.isfile(path):
        logging.error(f"❌ 不是文件: {path}")
        return 0

    with wave.open(path, "rb") as wav_file:
        frames = wav_file.getnframes()
        rate = wav_file.getframerate()
        return frames / float(rate)


def read_wav_file(path):
    with wave.open(path, "rb") as wav_file:
        params = {
            "channels": wav_file.getnchannels(),
            "sample_width": wav_file.getsampwidth(),
            "sample_rate": wav_file.getframerate(),
        }
        frames = wav_file.readframes(wav_file.getnframes())
    return params, frames


def apply_fade_out(frames, sample_width, channels, fade_frames):
    if fade_frames <= 0:
        return frames

    frame_size = sample_width * channels
    total_frames = len(frames) // frame_size
    fade_frames = min(fade_frames, total_frames)
    if fade_frames <= 0:
        return frames

    keep_bytes = (total_frames - fade_frames) * frame_size
    prefix = frames[:keep_bytes]
    fade_part = frames[keep_bytes:]
    faded = bytearray()

    # 只对尾部做线性淡出，避免硬截断时产生明显爆音。
    for i in range(fade_frames):
        start = i * frame_size
        end = start + frame_size
        factor = max(0.0, 1.0 - ((i + 1) / fade_frames))
        faded.extend(audioop.mul(fade_part[start:end], sample_width, factor))

    return prefix + bytes(faded)


def convert_wav_frames(frames, src_params, target_channels, target_sample_width, target_sample_rate):
    channels = src_params["channels"]
    sample_width = src_params["sample_width"]
    sample_rate = src_params["sample_rate"]

    if sample_width != target_sample_width:
        frames = audioop.lin2lin(frames, sample_width, target_sample_width)
        sample_width = target_sample_width

    if channels != target_channels:
        if channels == 2 and target_channels == 1:
            frames = audioop.tomono(frames, sample_width, 0.5, 0.5)
        elif channels == 1 and target_channels == 2:
            frames = audioop.tostereo(frames, sample_width, 1.0, 1.0)
        else:
            raise ValueError(f"不支持的声道转换: {channels} -> {target_channels}")
        channels = target_channels

    if sample_rate != target_sample_rate:
        frames, _ = audioop.ratecv(
            frames,
            sample_width,
            channels,
            sample_rate,
            target_sample_rate,
            None,
        )

    return frames


def trim_wav_frames(frames, channels, sample_width, sample_rate,
                    max_duration_sec=MAX_SFX_DURATION_SEC,
                    fade_out_sec=SFX_FADE_OUT_SEC):
    frame_size = channels * sample_width
    total_frames = len(frames) // frame_size
    max_frames = int(sample_rate * max_duration_sec)

    if total_frames <= max_frames:
        return frames

    trimmed = frames[:max_frames * frame_size]
    fade_frames = int(sample_rate * fade_out_sec)
    trimmed = apply_fade_out(trimmed, sample_width, channels, fade_frames)
    return trimmed


def apply_gain(frames, sample_width, gain):
    if gain == 1.0:
        return frames
    return audioop.mul(frames, sample_width, gain)


def normalize_wav_file(path, target_channels=TARGET_CHANNELS,
                       target_sample_width=TARGET_SAMPLE_WIDTH,
                       target_sample_rate=TARGET_SAMPLE_RATE):
    src_params, frames = read_wav_file(path)
    if (
        src_params["channels"] == target_channels
        and src_params["sample_width"] == target_sample_width
        and src_params["sample_rate"] == target_sample_rate
    ):
        # 已经满足最终合并格式时直接复用，减少重复转码。
        return path

    frames = convert_wav_frames(
        frames,
        src_params,
        target_channels=target_channels,
        target_sample_width=target_sample_width,
        target_sample_rate=target_sample_rate,
    )

    normalized_path = os.path.splitext(path)[0] + ".normalized.wav"
    with wave.open(normalized_path, "wb") as wav_file:
        wav_file.setnchannels(target_channels)
        wav_file.setsampwidth(target_sample_width)
        wav_file.setframerate(target_sample_rate)
        wav_file.writeframes(frames)

    logging.info(
        "🔧 已重采样: %s (%sHz/%sch -> %sHz/%sch)",
        os.path.basename(path),
        src_params["sample_rate"],
        src_params["channels"],
        target_sample_rate,
        target_channels,
    )
    return normalized_path


def prepare_sfx_wav(path, target_channels=TARGET_CHANNELS,
                    target_sample_width=TARGET_SAMPLE_WIDTH,
                    target_sample_rate=TARGET_SAMPLE_RATE,
                    max_duration_sec=MAX_SFX_DURATION_SEC):
    src_params, frames = read_wav_file(path)
    frames = convert_wav_frames(
        frames,
        src_params,
        target_channels=target_channels,
        target_sample_width=target_sample_width,
        target_sample_rate=target_sample_rate,
    )
    frames = trim_wav_frames(
        frames,
        channels=target_channels,
        sample_width=target_sample_width,
        sample_rate=target_sample_rate,
        max_duration_sec=max_duration_sec,
    )
    # 本地音效默认压低一点，避免覆盖 TTS 主人声。
    frames = apply_gain(frames, target_sample_width, SFX_GAIN)

    prepared_path = os.path.splitext(path)[0] + ".prepared.wav"
    with wave.open(prepared_path, "wb") as wav_file:
        wav_file.setnchannels(target_channels)
        wav_file.setsampwidth(target_sample_width)
        wav_file.setframerate(target_sample_rate)
        wav_file.writeframes(frames)

    duration = len(frames) / float(target_channels * target_sample_width * target_sample_rate)
    logging.info(
        "🔧 音效已处理: %s -> %.2fs @ %sHz, gain=%.2f",
        os.path.basename(path),
        duration,
        target_sample_rate,
        SFX_GAIN,
    )
    return prepared_path


def find_existing_part_wav(part_dir):
    # 兼容历史目录布局：优先找标准输出，其次兜底找残留的分段 wav。
    candidates = [
        os.path.join(part_dir, "audio_000.wav"),
        os.path.join(part_dir, "seg_000", "audio_000.wav"),
    ]

    for candidate in candidates:
        if os.path.exists(candidate):
            return candidate

    for name in sorted(os.listdir(part_dir)) if os.path.isdir(part_dir) else []:
        candidate = os.path.join(part_dir, name)
        if os.path.isdir(candidate):
            nested_wav = os.path.join(candidate, "audio_000.wav")
            if os.path.exists(nested_wav):
                return nested_wav

    wav_names = sorted(
        name for name in os.listdir(part_dir)
        if os.path.isfile(os.path.join(part_dir, name)) and name.endswith(".wav")
    ) if os.path.isdir(part_dir) else []

    for preferred_name in wav_names:
        if preferred_name.endswith(".normalized.wav"):
            return os.path.join(part_dir, preferred_name)

    for preferred_name in wav_names:
        if preferred_name != "audio_000.wav":
            return os.path.join(part_dir, preferred_name)

    return None


def merge_wav_files(wav_files, output_file,
                    target_channels=TARGET_CHANNELS,
                    target_sample_width=TARGET_SAMPLE_WIDTH,
                    target_sample_rate=TARGET_SAMPLE_RATE):
    if not wav_files:
        raise ValueError("没有可合并的 wav 文件")

    logging.info(f"🔗 合并 -> {output_file}")

    normalized_files = []
    for wav_path in wav_files:
        normalized_files.append(
            normalize_wav_file(
                wav_path,
                target_channels=target_channels,
                target_sample_width=target_sample_width,
                target_sample_rate=target_sample_rate,
            )
        )

    with wave.open(output_file, "wb") as out:
        out.setnchannels(target_channels)
        out.setsampwidth(target_sample_width)
        out.setframerate(target_sample_rate)

        for wav_path in normalized_files:
            with wave.open(wav_path, "rb") as wav_file:
                out.writeframes(wav_file.readframes(wav_file.getnframes()))


# ===============================
# TTS 调用
# ===============================

def clean_text(text):
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\r\n", "\n", text)
    text = re.sub(r"[<>《》【】\[\]{}]", "", text)
    return text.strip()


def normalize_tts_text(text):
    text = clean_text(text)
    text = re.sub(r"[“”]", "\"", text)
    text = re.sub(r"[‘’]", "'", text)
    text = re.sub(r"[~～]{2,}", "。", text)
    text = re.sub(r"[…]{2,}", "，", text)
    text = re.sub(r"[—－]{2,}", "，", text)
    text = re.sub(r"([，。！？；,!?])\1+", r"\1", text)
    # 连续语气词保留少量重复，既减少模型卡顿，也尽量不完全抹掉语气。
    text = re.sub(r"([啊呀哦喔哈呵哇呜嗯欸诶哎])\1{2,}", r"\1\1", text)
    text = re.sub(r"\s*\n\s*", "。", text)
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"([,，])(?=[,，。！？!?])", "", text)
    return text.strip(" ，。")


def sanitize_tts_text(text):
    cleaned_text = normalize_tts_text(text)
    cleaned_text = re.sub(
        r"[^\u4e00-\u9fff\u3000-\u303f\uff00-\uffef\w\s.,;:!?\"'\-()，。！？；：、“”‘’]",
        "",
        cleaned_text,
    )
    cleaned_text = re.sub(r"-{2,}", "", cleaned_text)
    return cleaned_text.strip()


def resolve_voice(requested_voice):
    if requested_voice and requested_voice != DEFAULT_VOICE:
        logging.info("🎙️ 已忽略传入音色 %s，统一使用默认音色 %s", requested_voice, DEFAULT_VOICE)
    return DEFAULT_VOICE


def generate_tts(text, model, voice, lang_code, output_path, cfg_scale=DEFAULT_CFG_SCALE):
    cleaned_text = sanitize_tts_text(text)

    if not cleaned_text:
        raise ValueError("清理后文本为空，已跳过本段")

    resolved_voice = resolve_voice(voice)

    cmd = [
        sys.executable,
        "-m",
        "mlx_audio.tts.generate",
        "--model", model,
        "--text", cleaned_text,
        "--voice", resolved_voice,
        "--lang_code", lang_code,
        "--output_path", output_path,
        "--cfg_scale", str(cfg_scale),
    ]

    logging.info(f"🎤 Generating: {output_path}")

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        logging.error(f"❌ TTS 命令失败，返回码: {result.returncode}")
        logging.error(f"stderr: {result.stderr}")
        logging.error(f"stdout: {result.stdout}")
        raise subprocess.CalledProcessError(result.returncode, cmd, result.stdout, result.stderr)

    wav_path = os.path.join(output_path, "audio_000.wav")
    if not os.path.exists(wav_path):
        raise FileNotFoundError(f"TTS 输出不存在: {wav_path}")

    return normalize_wav_file(wav_path)


# ===============================
# 拟声词检测与分段
# ===============================

def detect_sound_effects(text):
    results = []
    occupied = set()

    for pattern, sfx_key in SFX_PATTERNS:
        for match in re.finditer(pattern, text):
            start, end = match.start(), match.end()
            # 已命中的字符区间不再重复匹配，避免一个片段被多个规则拆碎。
            if any(pos in occupied for pos in range(start, end)):
                continue

            if sfx_key:
                key = sfx_key
            else:
                matched = match.group()
                if matched.endswith("声"):
                    matched = matched[:-1]

                key = ""
                # 优先匹配更长的关键词，避免“轰隆”被“轰”提前截走。
                for candidate in sorted(SFX_TO_FILE.keys(), key=len, reverse=True):
                    if candidate in matched:
                        key = candidate
                        break

            if not key:
                continue

            # 双重过滤：正则负责找候选，最终是否允许插入仍以非语音白名单为准。
            if key not in NON_SPEECH_SFX_KEYS:
                continue

            results.append((start, end, key))
            occupied.update(range(start, end))

    results.sort(key=lambda item: item[0])
    return results


def split_chunk_into_segments(chunk):
    effects = detect_sound_effects(chunk)
    if not effects:
        return [("text", chunk.strip())] if chunk.strip() else []

    segments = []
    cursor = 0

    for start, end, sfx_key in effects:
        before_text = chunk[cursor:start].strip()
        if before_text:
            segments.append(("text", before_text))
        segments.append(("sfx", sfx_key))
        cursor = end

    remaining_text = chunk[cursor:].strip()
    if remaining_text:
        segments.append(("text", remaining_text))

    return segments


# ===============================
# 生成安全文件名
# ===============================

def generate_safe_filename(text, max_len=10):
    replace_map = {
        "，": "",
        "。": "",
        "！": "",
        "？": "",
        "：": "",
        "；": "",
        "、": "",
        "《": "",
        "》": "",
        "\"": "",
        "'": "",
        "（": "",
        "）": "",
        "—": "",
        "…": "",
    }

    for old, new in replace_map.items():
        text = text.replace(old, new)

    chinese_chars = re.findall(r"[\u4e00-\u9fff]", text)
    if chinese_chars:
        name = "".join(chinese_chars[:max_len])
    else:
        name = re.sub(r"[^A-Za-z0-9]", "", text)[:max_len]

    if not name:
        name = "tts"

    timestamp = time.strftime("%H%M%S")
    return f"{name}_{timestamp}"


# ===============================
# 读取 txt
# ===============================

def read_text_file(txt_path):
    with open(txt_path, "rb") as f:
        raw = f.read()

    if not raw:
        raise ValueError("❌ txt 文件为空")

    result = chardet.detect(raw)
    detected_encoding = result.get("encoding")
    confidence = result.get("confidence", 0)
    logging.info(f"📄 chardet 编码: {detected_encoding}, confidence={confidence}")

    encodings = ["utf-8-sig", "utf-8", "gb18030", "gbk"]
    if detected_encoding:
        normalized = detected_encoding.lower().replace("_", "-")
        if confidence >= 0.8 and normalized not in {enc.lower() for enc in encodings}:
            encodings.append(detected_encoding)

    last_error = None
    for encoding in encodings:
        try:
            text = raw.decode(encoding)
            logging.info(f"📄 使用编码: {encoding}")
            break
        except UnicodeDecodeError as exc:
            last_error = exc
    else:
        detail = f"{last_error}" if last_error else "unknown error"
        raise ValueError(f"无法识别文本编码: {txt_path} ({detail})")

    return clean_text(text)


# ===============================
# 文本切块
# ===============================

def split_long_sentence(sentence, chunk_size):
    chunks = []
    remaining = sentence.strip()
    split_marks = "，,、；;：: "

    while len(remaining) > chunk_size:
        search_window = remaining[:chunk_size + 1]
        split_at = -1

        # 优先在靠近 chunk_size 的弱标点处切，实在找不到再硬切。
        for mark in split_marks:
            idx = search_window.rfind(mark)
            if idx > split_at:
                split_at = idx

        if split_at < MIN_SPLIT_SEARCH:
            split_at = chunk_size
        else:
            split_at += 1

        piece = remaining[:split_at].strip()
        if piece:
            chunks.append(piece)
        remaining = remaining[split_at:].strip()

    if remaining:
        chunks.append(remaining)

    return chunks


def split_text(text, chunk_size=CHUNK_SIZE):
    sentences = re.split(r"(?<=[。！？!?；;：:\n])", text)
    chunks = []
    current = ""

    for sentence in sentences:
        sentence = sentence.strip()
        if not sentence:
            continue

        pieces = [sentence]
        if len(sentence) > chunk_size:
            pieces = split_long_sentence(sentence, chunk_size)

        for piece in pieces:
            if not current:
                current = piece
            elif len(current) + len(piece) <= chunk_size:
                current += piece
            else:
                chunks.append(current.strip())
                current = piece

    if current:
        chunks.append(current.strip())

    logging.info(f"📦 分段数量: {len(chunks)}")
    return chunks


# ===============================
# 处理单个 txt / 文本
# ===============================

def process_text(text, output_root, model, voice, lang, base_name=None,
                 resume_state=None, resume_key=None, text_hash=None,
                 sfx_dir=SFX_LIBRARY_DIR,
                 allow_tts_fallback_for_sfx=False, enable_sfx=DEFAULT_ENABLE_SFX,
                 cfg_scale=DEFAULT_CFG_SCALE,
                 chunk_size=CHUNK_SIZE):
    text = clean_text(text)
    chunks = split_text(text, chunk_size=chunk_size)

    if not base_name:
        base_name = generate_safe_filename(text[:50], max_len=10)

    book_dir = os.path.join(output_root, base_name)
    os.makedirs(book_dir, exist_ok=True)

    all_part_wav_files = []

    start_index = 0
    state_key = resume_key or base_name
    legacy_state_key = base_name if state_key != base_name else None
    resume_entry = get_resume_entry(resume_state, state_key, legacy_state_key)
    if resume_entry:
        start_index = resume_entry.get("segment", 0)

    logging.info(f"📖 {base_name}: 从 segment {start_index + 1} 开始续传")

    for i, chunk in enumerate(chunks):
        if i < start_index:
            part_dir = os.path.join(book_dir, f"part_{i:03d}")
            wav_file = find_existing_part_wav(part_dir)
            if wav_file:
                logging.info(f"⏭️ 跳过已完成 segment {i + 1}")
                all_part_wav_files.append(wav_file)
                continue

            logging.warning(f"⚠️ Segment {i + 1} 状态记录存在但文件缺失，重新处理")
            start_index = i

        set_progress(base_name, i + 1, len(chunks), chunk)
        add_chars(len(chunk))
        log_segment_start(i + 1, chunk)

        part_dir = os.path.join(book_dir, f"part_{i:03d}")
        os.makedirs(part_dir, exist_ok=True)

        t0 = time.time()
        # 先把 chunk 拆成文本 / 音效片段，再分别走 TTS 或本地 wav。
        segment_parts = split_chunk_into_segments(chunk) if enable_sfx else [("text", chunk)]
        part_wav_files = []

        for seg_type, seg_content in segment_parts:
            seg_idx = len(part_wav_files)
            if seg_type == "text":
                sanitized_text = sanitize_tts_text(seg_content)
                if not sanitized_text:
                    logging.warning(f"⏭️ 跳过空白文本片段: segment={i + 1}, seg={seg_idx:03d}")
                    continue
                seg_dir = os.path.join(part_dir, f"seg_{seg_idx:03d}")
                actual_wav = generate_tts(sanitized_text, model, voice, lang, seg_dir, cfg_scale=cfg_scale)
                part_wav_files.append(actual_wav)
                continue

            sfx_file = SFX_TO_FILE.get(seg_content)
            if sfx_file:
                src_path = os.path.join(sfx_dir, sfx_file)
                if os.path.exists(src_path):
                    seg_path = os.path.join(part_dir, f"seg_{seg_idx:03d}.wav")
                    shutil.copy(src_path, seg_path)
                    seg_path = prepare_sfx_wav(seg_path)
                    logging.info(f"🔊 引用音效: {sfx_file}")
                    part_wav_files.append(seg_path)
                    continue

                logging.warning(f"⚠️ 音效文件不存在: {sfx_file}")

            if allow_tts_fallback_for_sfx:
                logging.info(f"🗣️ TTS 回退生成音效占位: {seg_content}")
                seg_dir = os.path.join(part_dir, f"seg_{seg_idx:03d}")
                actual_wav = generate_tts(seg_content, model, voice, lang, seg_dir, cfg_scale=cfg_scale)
                part_wav_files.append(actual_wav)
            else:
                logging.warning(f"⏭️ 跳过低质量风险音效: {seg_content}")

        if not part_wav_files:
            logging.warning(f"⚠️ chunk {i + 1} 无有效音频片段")
            continue

        if len(part_wav_files) == 1:
            wav_file = part_wav_files[0]
        else:
            wav_file = os.path.join(part_dir, "audio_000.wav")
            merge_wav_files(part_wav_files, wav_file)

        all_part_wav_files.append(wav_file)

        duration = time.time() - t0
        audio_sec = get_wav_duration(wav_file)
        log_segment_end(i + 1, duration, audio_sec)

        if resume_state is not None:
            # 这里写入的是“下一个待处理 segment 下标”，便于异常中断后继续跑。
            set_resume_entry(resume_state, state_key, i + 1, text_hash=text_hash)

    if not all_part_wav_files:
        raise RuntimeError(f"未生成任何有效音频: {base_name}")

    final_wav = os.path.join(output_root, f"{base_name}.wav")
    merge_wav_files(all_part_wav_files, final_wav)

    logging.info(f"🎧 完成文件: {final_wav}")

    if resume_state is not None and state_key in resume_state:
        del resume_state[state_key]
        if legacy_state_key and legacy_state_key in resume_state:
            del resume_state[legacy_state_key]
        save_resume_state(resume_state)

    return final_wav


# ===============================
# 处理单个 txt
# ===============================

def process_file(txt_path, input_root, output_root,
                 model, voice, lang, resume_state,
                 sfx_dir, allow_tts_fallback_for_sfx, enable_sfx, cfg_scale, chunk_size):
    logging.info(f"\n📂 处理: {txt_path}")

    set_progress(os.path.basename(txt_path), 0, 0, "reading...")

    text = read_text_file(txt_path)
    text_hash = compute_text_hash(text)
    txt_file_name = os.path.basename(txt_path)
    base_name = os.path.splitext(txt_file_name)[0]

    rel_path = os.path.relpath(txt_path, input_root)
    rel_dir = os.path.dirname(rel_path)
    resume_key = make_resume_key(rel_path)
    legacy_state_key = base_name if resume_key != base_name else None

    final_wav = os.path.join(output_root, rel_dir, f"{base_name}.wav")
    resume_entry = get_resume_entry(resume_state, resume_key, legacy_state_key)
    stored_hash = resume_entry.get("text_hash") if resume_entry else None

    # 已存在目标 wav 时，结合文本 hash 判断是直接跳过还是强制重建。
    if os.path.exists(final_wav) and resume_state is not None:
        if stored_hash == text_hash:
            logging.info(f"✅ 文件已完成且内容未变，跳过: {base_name}")
            if resume_key in resume_state:
                del resume_state[resume_key]
                if legacy_state_key and legacy_state_key in resume_state:
                    del resume_state[legacy_state_key]
                save_resume_state(resume_state)
            return final_wav

        if stored_hash:
            logging.info(f"♻️ 检测到内容变化，重新生成: {base_name}")
        else:
            logging.info(f"♻️ 已有输出但缺少历史 hash，重新生成以建立内容索引: {base_name}")

    target_output_root = os.path.join(output_root, rel_dir)
    os.makedirs(target_output_root, exist_ok=True)

    return process_text(
        text,
        target_output_root,
        model,
        voice,
        lang,
        base_name,
        resume_state,
        resume_key=resume_key,
        text_hash=text_hash,
        sfx_dir=sfx_dir,
        allow_tts_fallback_for_sfx=allow_tts_fallback_for_sfx,
        enable_sfx=enable_sfx,
        cfg_scale=cfg_scale,
        chunk_size=chunk_size,
    )


# ===============================
# 主入口
# ===============================

def main():
    parser = argparse.ArgumentParser(
        description="Qwen3 TTS - 修正版，统一音频参数并优化分段"
    )

    parser.add_argument("--input", help="txt 输入目录或文件")
    parser.add_argument("--text", help="直接输入文本")
    parser.add_argument("--output", default="output_audio", help="wav 输出目录")
    parser.add_argument("--voice", default=DEFAULT_VOICE, help="voice 名称 (默认 serena)")
    parser.add_argument("--lang", default=DEFAULT_LANG, help="语言代码 (默认 zh)")
    parser.add_argument("--model", default=DEFAULT_MODEL, help="模型路径；可在 config/default_config.json 中设置")
    parser.add_argument("--sfx-dir", default=SFX_LIBRARY_DIR, help="本地音效素材目录")
    parser.add_argument(
        "--enable-sfx",
        action="store_true",
        default=DEFAULT_ENABLE_SFX,
        help="启用本地音效插入；默认开启，且仅在较明确的拟声词上触发",
    )
    parser.add_argument(
        "--disable-sfx",
        action="store_false",
        dest="enable_sfx",
        help="关闭本地音效插入",
    )
    parser.add_argument(
        "--tts-fallback-for-sfx",
        action="store_true",
        help="本地音效缺失时，用普通 TTS 念出拟声词作为回退",
    )
    parser.add_argument(
        "--cfg-scale",
        default=DEFAULT_CFG_SCALE,
        help="Qwen3 TTS cfg_scale，默认 1.2",
    )
    parser.add_argument(
        "--chunk-size",
        type=int,
        default=CHUNK_SIZE,
        help="文本分段长度，默认 180；此版本优先在逗号和句号处切分",
    )
    parser.add_argument(
        "--no-resume",
        action="store_true",
        help="不使用续传，从头开始",
    )

    args = parser.parse_args()

    if not args.input and not args.text:
        parser.error("必须提供 --input 或 --text")

    if args.input and args.text:
        parser.error("不能同时提供 --input 和 --text")

    if not args.model:
        parser.error("必须通过 --model 或 config/default_config.json 配置模型路径")

    output_root = args.output
    os.makedirs(output_root, exist_ok=True)

    try:
        if args.text:
            base_name = generate_safe_filename(args.text[:50], max_len=10)
            set_progress(base_name, 0, 0, "preparing...")
            final_wav = process_text(
                args.text,
                output_root,
                args.model,
                args.voice,
                args.lang,
                base_name,
                sfx_dir=args.sfx_dir,
                allow_tts_fallback_for_sfx=args.tts_fallback_for_sfx,
                enable_sfx=args.enable_sfx,
                cfg_scale=args.cfg_scale,
                chunk_size=args.chunk_size,
            )
            logging.info(f"\n🎉 完成: {final_wav}")
        else:
            input_root = args.input
            txt_files = []

            if os.path.isfile(input_root):
                if input_root.lower().endswith(".txt"):
                    txt_files = [input_root]
                else:
                    logging.warning(f"⚠️ 不是 txt 文件: {input_root}")
            else:
                for dirpath, _, filenames in os.walk(input_root):
                    for filename in filenames:
                        if filename.lower().endswith(".txt"):
                            txt_files.append(os.path.join(dirpath, filename))

            txt_files.sort()
            logging.info(f"📚 共发现 {len(txt_files)} 个 txt 文件")

            sfx_count = 0
            if os.path.exists(args.sfx_dir):
                for filename in os.listdir(args.sfx_dir):
                    if filename.endswith(".wav"):
                        sfx_count += 1
            if args.enable_sfx:
                logging.info(f"📊 音效素材: {sfx_count} 个")
            else:
                logging.info(f"🔇 音效插入已关闭，本地素材未参与处理（库存 {sfx_count} 个）")

            resume_state = load_resume_state() if not args.no_resume else None
            if args.no_resume:
                logging.info("🔄 不使用续传，从头开始")
                if os.path.exists(RESUME_STATE_FILE):
                    os.remove(RESUME_STATE_FILE)

            for txt_path in txt_files:
                process_file(
                    txt_path,
                    input_root,
                    output_root,
                    args.model,
                    args.voice,
                    args.lang,
                    resume_state,
                    args.sfx_dir,
                    args.tts_fallback_for_sfx,
                    args.enable_sfx,
                    args.cfg_scale,
                    args.chunk_size,
                )

            logging.info("\n🎉 全部完成")

    except Exception:
        logging.exception("❌ 运行异常")

    finally:
        stop_monitor()
        log_summary()


if __name__ == "__main__":
    main()
