import json
import locale
import math
import subprocess
from pathlib import Path

from .documents import DocumentReadError


ALLOWED_VIDEO_EXTENSIONS = {"mp4", "mov", "mkv", "webm", "avi", "m4v"}
DEFAULT_VIDEO_FRAME_MAX_COUNT = 16
VIDEO_FRAME_MIME_TYPE = "image/jpeg"


class VideoFrameExtractionError(DocumentReadError):
    pass


def allowed_video_file(filename: str) -> bool:
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_VIDEO_EXTENSIONS


def video_extension_of(filename: str) -> str:
    return filename.rsplit(".", 1)[1].lower()


def _decode_process_output(value) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, bytes):
        encodings = ["utf-8", locale.getpreferredencoding(False), "gb18030"]
        for encoding in dict.fromkeys(encodings):
            try:
                return value.decode(encoding)
            except UnicodeDecodeError:
                continue
        return value.decode("utf-8", errors="replace")
    return str(value)


def _process_message(completed) -> str:
    return (_decode_process_output(completed.stderr) or _decode_process_output(completed.stdout)).strip()


def extract_video_frames(
    video_path: Path,
    output_dir: Path,
    *,
    source_filename: str = "",
    max_frames: int = DEFAULT_VIDEO_FRAME_MAX_COUNT,
) -> tuple[list[dict], dict]:
    output_dir.mkdir(parents=True, exist_ok=True)
    source_name = Path(str(source_filename or video_path.name)).name
    duration = _probe_video_duration(video_path)
    timestamps = _sample_video_timestamps(duration, max_frames)
    if not timestamps:
        raise VideoFrameExtractionError("未能计算视频抽帧时间点。")

    frames = []
    for sequence, timestamp in enumerate(timestamps, start=1):
        filename = f"{sequence:04d}_t{int(timestamp * 1000):09d}.jpg"
        destination = output_dir / filename
        _extract_frame(video_path, destination, timestamp)
        if not destination.is_file() or destination.stat().st_size <= 0:
            raise VideoFrameExtractionError(f"视频第 {sequence} 帧抽取失败。")
        position = f"{_format_timestamp(timestamp)}"
        frames.append(
            {
                "id": f"frame-{sequence:04d}",
                "filename": filename,
                "stored_filename": filename,
                "relative_path": filename,
                "mime_type": VIDEO_FRAME_MIME_TYPE,
                "position": position,
                "source": source_name,
                "size_bytes": destination.stat().st_size,
                "kind": "video_frame",
                "timestamp_seconds": round(float(timestamp), 3),
            }
        )
    return frames, {
        "duration_seconds": round(float(duration), 3),
        "selected_timestamps": [round(float(value), 3) for value in timestamps],
        "max_frames": max(1, int(max_frames or DEFAULT_VIDEO_FRAME_MAX_COUNT)),
        "frame_count": len(frames),
        "strategy": "uniform-sampling",
    }


def format_video_document_text(filename: str, frames: list[dict], selection: dict | None = None) -> str:
    name = Path(str(filename or "")).name.strip() or "video"
    selection = selection or {}
    duration = float(selection.get("duration_seconds") or 0)
    lines = [
        f"file: {name}",
        "",
        "video_context:",
        f"- 视频时长：{_format_timestamp(duration)}（{duration:.1f} 秒）" if duration else "- 视频时长：未识别",
        f"- 抽取帧数：{len(frames)}",
        "- 抽帧策略：按时间轴均匀采样，模型只能看到这些采样帧，连续动作需结合前后帧判断。",
        "",
        "video_frames:",
    ]
    for frame in frames:
        lines.append(
            f"- {frame.get('filename')}: 时间点 {frame.get('position') or '-'} "
            f"({frame.get('mime_type') or '-'}, {int(frame.get('size_bytes') or 0) / 1024:.1f} KB)"
        )
    return "\n".join(lines).strip()


def _probe_video_duration(video_path: Path) -> float:
    command = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "json",
        str(video_path),
    ]
    try:
        completed = subprocess.run(command, capture_output=True, timeout=30, check=False)
    except FileNotFoundError as exc:
        raise VideoFrameExtractionError("视频抽帧依赖 ffmpeg/ffprobe，当前环境未安装或未加入 PATH。") from exc
    except subprocess.TimeoutExpired as exc:
        raise VideoFrameExtractionError("读取视频时长超时。") from exc
    if completed.returncode != 0:
        message = _process_message(completed)
        raise VideoFrameExtractionError(f"读取视频时长失败：{message or 'ffprobe 返回异常'}")
    try:
        payload = json.loads(_decode_process_output(completed.stdout) or "{}")
        duration = float(payload.get("format", {}).get("duration") or 0)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise VideoFrameExtractionError("无法识别视频时长。") from exc
    if duration <= 0:
        raise VideoFrameExtractionError("无法识别视频时长。")
    return duration


def _sample_video_timestamps(duration: float, max_frames: int) -> list[float]:
    duration = max(0.0, float(duration or 0))
    frame_limit = max(1, int(max_frames or DEFAULT_VIDEO_FRAME_MAX_COUNT))
    if duration <= 0:
        return []
    frame_count = min(frame_limit, max(1, math.ceil(duration / 2) + 1))
    if frame_count == 1:
        return [0.0]
    last_timestamp = max(0.0, duration - 0.1)
    step = last_timestamp / max(1, frame_count - 1)
    timestamps = []
    seen = set()
    for index in range(frame_count):
        timestamp = round(min(last_timestamp, index * step), 3)
        key = int(timestamp * 1000)
        if key in seen:
            continue
        seen.add(key)
        timestamps.append(timestamp)
    return timestamps


def _extract_frame(video_path: Path, destination: Path, timestamp: float) -> None:
    command = [
        "ffmpeg",
        "-y",
        "-ss",
        f"{float(timestamp):.3f}",
        "-i",
        str(video_path),
        "-frames:v",
        "1",
        "-q:v",
        "3",
        str(destination),
    ]
    try:
        completed = subprocess.run(command, capture_output=True, timeout=60, check=False)
    except FileNotFoundError as exc:
        raise VideoFrameExtractionError("视频抽帧依赖 ffmpeg/ffprobe，当前环境未安装或未加入 PATH。") from exc
    except subprocess.TimeoutExpired as exc:
        raise VideoFrameExtractionError(f"抽取 {_format_timestamp(timestamp)} 视频帧超时。") from exc
    if completed.returncode != 0:
        message = _process_message(completed)
        raise VideoFrameExtractionError(f"抽取 {_format_timestamp(timestamp)} 视频帧失败：{message or 'ffmpeg 返回异常'}")


def _format_timestamp(seconds: float) -> str:
    value = max(0.0, float(seconds or 0))
    total_seconds = int(value)
    millis = int(round((value - total_seconds) * 1000))
    if millis >= 1000:
        total_seconds += 1
        millis -= 1000
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds_value = divmod(remainder, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{seconds_value:02d}.{millis:03d}"
    return f"{minutes:02d}:{seconds_value:02d}.{millis:03d}"
