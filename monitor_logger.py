import logging
import time
import os
import psutil
import subprocess
import threading

# ===== 全局统计 =====

start_time = time.time()

total_chars = 0
total_audio_seconds = 0
segment_count = 0

process = psutil.Process(os.getpid())

monitor_running = False

# ===== 当前处理进度 =====

current_file = ""
current_chunk = 0
total_chunks = 0
current_text = ""


# ===== 初始化日志 =====

def init_logger(log_file="tts_run.log"):

    logger = logging.getLogger()
    logger.setLevel(logging.INFO)

    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)s | %(message)s"
    )

    console = logging.StreamHandler()
    console.setFormatter(formatter)

    file_handler = logging.FileHandler(
        log_file,
        encoding="utf-8"
    )
    file_handler.setFormatter(formatter)

    logger.addHandler(console)
    logger.addHandler(file_handler)

    logging.info("🚀 Logger initialized")


# ===== 系统信息 =====

def get_system_stats():

    cpu = psutil.cpu_percent()

    mem = psutil.virtual_memory()

    disk = psutil.disk_usage("/")

    proc_mem = process.memory_info().rss / 1024 / 1024

    return {
        "cpu": cpu,
        "mem_percent": mem.percent,
        "mem_used": mem.used / 1024 / 1024 / 1024,
        "disk_percent": disk.percent,
        "proc_mem": proc_mem
    }


# ===== Mac GPU =====

def get_metal_gpu():

    try:

        cmd = [
            "powermetrics",
            "--samplers",
            "gpu_power",
            "-n",
            "1"
        ]

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True
        )

        output = result.stdout

        for line in output.split("\n"):

            if "GPU Busy" in line:

                val = line.split(":")[1]

                return val.strip()

    except Exception:

        pass

    return "N/A"


# ===== 后台监控 =====

def monitor_loop(interval=10):

    global monitor_running

    while monitor_running:

        stats = get_system_stats()

        gpu = get_metal_gpu()

        progress = ""
        if current_file:
            progress = f" | 📝 {current_file} [{current_chunk}/{total_chunks}]"
            if current_text:
                progress += f" `{current_text}...`"

        logging.info(
            "📊 SYS | "
            f"CPU={stats['cpu']}% | "
            f"MEM={stats['mem_percent']}% "
            f"({stats['mem_used']:.2f}GB) | "
            f"PROC_MEM={stats['proc_mem']:.1f}MB | "
            f"DISK={stats['disk_percent']}% | "
            f"GPU={gpu}"
            + progress
        )

        time.sleep(interval)


def start_monitor(interval=10):

    global monitor_running

    monitor_running = True

    t = threading.Thread(
        target=monitor_loop,
        args=(interval,),
        daemon=True
    )

    t.start()

    logging.info("🟢 System monitor started")


def stop_monitor():

    global monitor_running

    monitor_running = False

    logging.info("🔴 System monitor stopped")


# ===== TTS统计 =====

def add_chars(n):

    global total_chars

    total_chars += n


def set_progress(file_name, chunk, total, text=""):
    """更新当前处理进度"""
    global current_file, current_chunk, total_chunks, current_text
    current_file = file_name
    current_chunk = chunk
    total_chunks = total
    current_text = text[:30] if text else ""


def log_segment_start(index, text):

    global segment_count

    segment_count += 1

    logging.info(
        f"🎬 Segment {index} start | "
        f"chars={len(text)}"
    )


def log_segment_end(index, duration, audio_seconds):

    global total_audio_seconds

    total_audio_seconds += audio_seconds

    rtf = (
        audio_seconds / duration
        if duration > 0 else 0
    )

    logging.info(
        f"✅ Segment {index} done | "
        f"time={duration:.2f}s | "
        f"audio={audio_seconds:.2f}s | "
        f"RTF={rtf:.2f}"
    )


def log_summary():

    total_time = time.time() - start_time

    rtf = (
        total_audio_seconds / total_time
        if total_time > 0 else 0
    )

    char_speed = (
        total_chars / total_time
        if total_time > 0 else 0
    )

    logging.info("=================================")
    logging.info("📊 TTS SUMMARY")
    logging.info(f"Segments: {segment_count}")
    logging.info(f"Total chars: {total_chars}")
    logging.info(f"Total audio: {total_audio_seconds:.2f}s")
    logging.info(f"Total time: {total_time:.2f}s")
    logging.info(f"Chars/sec: {char_speed:.2f}")
    logging.info(f"RTF: {rtf:.2f}")
    logging.info("=================================")