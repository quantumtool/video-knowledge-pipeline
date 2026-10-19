"""Auditable video-to-knowledge preprocessing with FFmpeg and optional Ollama."""

from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import json
import math
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from fractions import Fraction
from pathlib import Path
from typing import Any


HASH_WIDTH = 9
HASH_HEIGHT = 8
HASH_BYTES = HASH_WIDTH * HASH_HEIGHT
PTS_RE = re.compile(r"pts_time:\s*(-?(?:\d+(?:\.\d*)?|\.\d+))")


class PipelineError(RuntimeError):
    """An actionable failure from a media tool or malformed input."""


def require_tool(name: str) -> str:
    path = shutil.which(name)
    if not path:
        raise PipelineError(f"Required executable '{name}' was not found on PATH.")
    return path


def run_json(command: list[str]) -> dict[str, Any]:
    result = subprocess.run(command, text=True, capture_output=True, check=False)
    if result.returncode:
        detail = result.stderr.strip() or result.stdout.strip()
        raise PipelineError(detail or f"Command failed: {command[0]}")
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise PipelineError(f"Could not parse JSON from {command[0]}: {exc}") from exc


def ratio(value: str | None) -> float | None:
    if not value or value in {"N/A", "0/0"}:
        return None
    try:
        result = float(Fraction(value))
    except (ValueError, ZeroDivisionError):
        return None
    return result if math.isfinite(result) else None


def probe_media(video: Path) -> dict[str, Any]:
    ffprobe = require_tool("ffprobe")
    if not video.is_file():
        raise PipelineError(f"Video file does not exist: {video}")
    payload = run_json([
        ffprobe, "-v", "error", "-show_format", "-show_streams",
        "-of", "json", str(video),
    ])
    streams = payload.get("streams", [])
    video_stream = next((item for item in streams if item.get("codec_type") == "video"), None)
    audio_stream = next((item for item in streams if item.get("codec_type") == "audio"), None)
    if not video_stream:
        raise PipelineError("No video stream was found in the input.")
    fmt = payload.get("format", {})
    return {
        "filename": video.name,
        "container": fmt.get("format_name"),
        "duration_seconds": _float_or_none(fmt.get("duration")),
        "container_bitrate_bps": _int_or_none(fmt.get("bit_rate")),
        "video": {
            "codec": video_stream.get("codec_name"),
            "width": video_stream.get("width"),
            "height": video_stream.get("height"),
            "pixel_format": video_stream.get("pix_fmt"),
            "nominal_fps": ratio(video_stream.get("r_frame_rate")),
            "average_fps": ratio(video_stream.get("avg_frame_rate")),
            "duration_seconds": _float_or_none(video_stream.get("duration")),
            "bitrate_bps": _int_or_none(video_stream.get("bit_rate")),
            "time_base": video_stream.get("time_base"),
        },
        "audio": None if not audio_stream else {
            "codec": audio_stream.get("codec_name"),
            "sample_rate_hz": _int_or_none(audio_stream.get("sample_rate")),
            "channels": audio_stream.get("channels"),
            "bitrate_bps": _int_or_none(audio_stream.get("bit_rate")),
        },
    }


def _float_or_none(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _parse_showinfo(stderr: str) -> list[float]:
    return [float(match.group(1)) for match in PTS_RE.finditer(stderr)]


def _dhash(raw: bytes) -> int:
    if len(raw) != HASH_BYTES:
        raise PipelineError(f"Expected {HASH_BYTES} grayscale bytes per frame, got {len(raw)}.")
    value = 0
    for row in range(HASH_HEIGHT):
        start = row * HASH_WIDTH
        for col in range(HASH_WIDTH - 1):
            value = (value << 1) | int(raw[start + col] > raw[start + col + 1])
    return value


def _distance(left: int, right: int) -> int:
    return (left ^ right).bit_count()


def extract_frames(
    video: Path,
    output: Path,
    sample_fps: float | None,
    dedupe_hamming: int,
    transcript: Path | None,
) -> dict[str, Any]:
    ffmpeg = require_tool("ffmpeg")
    info = probe_media(video)
    output.mkdir(parents=True, exist_ok=True)
    raw_dir = output / "frames"
    if (output / "manifest.json").exists() or (output / "transcript_alignment.csv").exists():
        raise PipelineError(f"Output already contains a manifest: {output}")
    raw_dir.mkdir(exist_ok=True)
    if any(raw_dir.iterdir()):
        raise PipelineError(f"Output already contains frame files: {raw_dir}")
    if dedupe_hamming < 0 or dedupe_hamming > 64:
        raise PipelineError("--dedupe-hamming must be between 0 and 64.")
    if sample_fps is not None:
        if sample_fps <= 0:
            raise PipelineError("--sample-fps must be greater than zero.")
        source_fps = info["video"]["average_fps"] or info["video"]["nominal_fps"]
        if source_fps and sample_fps > source_fps * 1.001:
            raise PipelineError(
                f"Requested {sample_fps:g} FPS exceeds estimated source rate "
                f"{source_fps:g} FPS. Omit --sample-fps to decode every source frame."
            )

    prefix = f"fps=fps={sample_fps:.8g}," if sample_fps is not None else ""
    filters = (
        f"[0:v:0]{prefix}split=2[full][thumb];"
        f"[thumb]scale={HASH_WIDTH}:{HASH_HEIGHT}:flags=area,format=gray,showinfo[gray]"
    )
    command = [
        ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "info", "-i", str(video),
        "-filter_complex", filters,
        "-map", "[full]", "-fps_mode", "passthrough", "-start_number", "0",
        str(raw_dir / "frame_%08d.png"),
        "-map", "[gray]", "-fps_mode", "passthrough", "-pix_fmt", "gray",
        "-f", "rawvideo", "pipe:1",
    ]
    result = subprocess.run(command, capture_output=True, check=False)
    if result.returncode:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise PipelineError(detail or "FFmpeg frame extraction failed.")

    frames = sorted(raw_dir.glob("frame_*.png"))
    times = _parse_showinfo(result.stderr.decode("utf-8", errors="replace"))
    if len(result.stdout) % HASH_BYTES:
        raise PipelineError("FFmpeg returned an incomplete perceptual-hash stream.")
    hash_count = len(result.stdout) // HASH_BYTES
    if not frames or len(frames) != hash_count or len(frames) != len(times):
        raise PipelineError(
            "Frame, hash, and timestamp counts do not match "
            f"({len(frames)}, {hash_count}, {len(times)}). No results were discarded."
        )

    records: list[dict[str, Any]] = []
    seen_sha: set[str] = set()
    previous_kept_hash: int | None = None
    previous_kept_time: float | None = None
    for index, frame in enumerate(frames):
        pixels = result.stdout[index * HASH_BYTES:(index + 1) * HASH_BYTES]
        visual_hash = _dhash(pixels)
        sha = hashlib.sha256(frame.read_bytes()).hexdigest()
        distance = None if previous_kept_hash is None else _distance(visual_hash, previous_kept_hash)
        seconds_from_previous_kept = (
            None if previous_kept_time is None else round(times[index] - previous_kept_time, 6)
        )
        exact_duplicate = sha in seen_sha
        seen_sha.add(sha)
        if exact_duplicate:
            keep, reason = False, "exact_png_duplicate"
        elif distance is not None and dedupe_hamming > 0 and distance <= dedupe_hamming:
            keep, reason = False, "perceptual_near_duplicate"
        else:
            keep, reason = True, "retained"
            previous_kept_hash = visual_hash
            previous_kept_time = times[index]
        records.append({
            "index": index,
            "timestamp_seconds": round(times[index], 6),
            "path": str(frame.relative_to(output)),
            "sha256": sha,
            "dhash_64": f"{visual_hash:016x}",
            "distance_from_previous_retained": distance,
            "seconds_from_previous_retained": seconds_from_previous_kept,
            "selected": keep,
            "selection_reason": reason,
        })

    alignment = None
    if transcript:
        if not transcript.is_file():
            raise PipelineError(f"Transcript file does not exist: {transcript}")
        cues = parse_transcript(transcript)
        alignment = align_transcript(records, cues)
        write_alignment(output / "transcript_alignment.csv", alignment)

    manifest = {
        "schema_version": 1,
        "source": info,
        "sampling": {
            "mode": "every_decoded_source_frame" if sample_fps is None else "uniform_fps",
            "requested_fps": sample_fps,
            "dedupe_hamming_threshold": dedupe_hamming,
            "pixel_frames_preserved": True,
            "note": (
                "All extracted PNG frames remain on disk. Selection is a separate manifest "
                "view; perceptual deduplication is a heuristic and is never described as lossless."
            ),
        },
        "candidate_frame_count": len(records),
        "selected_frame_count": sum(item["selected"] for item in records),
        "frames": records,
        "transcript_alignment": None if alignment is None else "transcript_alignment.csv",
    }
    (output / "manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return manifest


def parse_transcript(path: Path) -> list[dict[str, Any]]:
    text = path.read_text(encoding="utf-8-sig")
    blocks = re.split(r"\n\s*\n", text)
    cues = []
    timestamp_line = re.compile(r"^\s*(\S+)\s*-->\s*(\S+)")
    for block in blocks:
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        match_index = next((i for i, line in enumerate(lines) if "-->" in line), None)
        if match_index is None:
            continue
        match = timestamp_line.match(lines[match_index])
        if not match:
            continue
        start = _parse_timestamp(match.group(1))
        end = _parse_timestamp(match.group(2))
        cue_text = " ".join(lines[match_index + 1:])
        cue_text = re.sub(r"<[^>]*>", "", cue_text).strip()
        if end >= start and cue_text:
            cues.append({"start_seconds": start, "end_seconds": end, "text": cue_text})
    return sorted(cues, key=lambda item: item["start_seconds"])


def _parse_timestamp(value: str) -> float:
    parts = value.replace(",", ".").split(":")
    if len(parts) == 3:
        hour, minute, second = parts
        return int(hour) * 3600 + int(minute) * 60 + float(second)
    if len(parts) == 2:
        minute, second = parts
        return int(minute) * 60 + float(second)
    raise PipelineError(f"Unsupported subtitle timestamp: {value}")


def align_transcript(
    frames: list[dict[str, Any]],
    cues: list[dict[str, Any]],
    window_seconds: float = 1.5,
) -> list[dict[str, Any]]:
    aligned = []
    for frame in frames:
        timestamp = frame["timestamp_seconds"]
        active = next(
            (cue for cue in cues if cue["start_seconds"] <= timestamp <= cue["end_seconds"]),
            None,
        )
        offset = None
        if active is None and cues:
            nearest = min(
                cues,
                key=lambda cue: min(
                    abs(timestamp - cue["start_seconds"]),
                    abs(timestamp - cue["end_seconds"]),
                ),
            )
            boundary = min(
                (nearest["start_seconds"], nearest["end_seconds"]),
                key=lambda value: abs(timestamp - value),
            )
            if abs(timestamp - boundary) <= window_seconds:
                active, offset = nearest, round(timestamp - boundary, 3)
        aligned.append({
            "frame_index": frame["index"],
            "timestamp_seconds": timestamp,
            "selected": frame["selected"],
            "transcript_start_seconds": None if active is None else active["start_seconds"],
            "transcript_end_seconds": None if active is None else active["end_seconds"],
            "offset_from_nearest_boundary_seconds": offset,
            "transcript_text": "" if active is None else active["text"],
        })
    return aligned


def write_alignment(path: Path, records: list[dict[str, Any]]) -> None:
    if not records:
        return
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(records[0]))
        writer.writeheader()
        writer.writerows(records)


def extract_audio(video: Path, output: Path, sample_rate: int) -> None:
    ffmpeg = require_tool("ffmpeg")
    if not video.is_file():
        raise PipelineError(f"Video file does not exist: {video}")
    if sample_rate <= 0:
        raise PipelineError("--sample-rate must be greater than zero.")
    output.parent.mkdir(parents=True, exist_ok=True)
    command = [
        ffmpeg, "-hide_banner", "-nostdin", "-v", "error", "-n",
        "-i", str(video), "-map", "0:a:0", "-vn", "-ac", "1",
        "-ar", str(sample_rate), "-c:a", "pcm_s16le", str(output),
    ]
    result = subprocess.run(command, text=True, capture_output=True, check=False)
    if result.returncode:
        raise PipelineError(result.stderr.strip() or "FFmpeg audio extraction failed.")


def run_ollama(
    output: Path,
    model: str,
    endpoint: str,
    batch_size: int,
    transcript_alignment: Path | None,
) -> Path:
    manifest_path = output / "manifest.json"
    if not manifest_path.is_file():
        raise PipelineError(f"Missing extraction manifest: {manifest_path}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    selected = [item for item in manifest["frames"] if item["selected"]]
    if not selected:
        raise PipelineError("No selected frames are available for vision-language analysis.")
    alignments: dict[int, dict[str, Any]] = {}
    if transcript_alignment:
        with transcript_alignment.open(encoding="utf-8", newline="") as stream:
            alignments = {int(row["frame_index"]): row for row in csv.DictReader(stream)}

    results = []
    endpoint = endpoint.rstrip("/") + "/api/chat"
    for start in range(0, len(selected), batch_size):
        batch = selected[start:start + batch_size]
        frame_lines = []
        images = []
        for item in batch:
            timestamp = item["timestamp_seconds"]
            cue = alignments.get(item["index"], {}).get("transcript_text", "")
            frame_lines.append(
                f"Image {len(frame_lines) + 1}: timestamp={timestamp:.3f}s; "
                f"aligned transcript={cue or '[none]'!r}"
            )
            frame_path = output / item["path"]
            images.append(base64.b64encode(frame_path.read_bytes()).decode("ascii"))
        prompt = (
            "Extract observable procedural knowledge from these timestamped tutorial frames. "
            "Return concise JSON with atomic claims, evidence timestamps, whether each claim is "
            "directly visible or inferred, and unresolved ambiguities. Do not invent steps or "
            "claim that viewing the frames trained or permanently changed the model. Preserve "
            "the distinction between image evidence and transcript evidence.\n\n"
            + "\n".join(frame_lines)
        )
        payload = {
            "model": model,
            "stream": False,
            "messages": [{"role": "user", "content": prompt, "images": images}],
        }
        request = urllib.request.Request(
            endpoint,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=900) as response:
                model_response = json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise PipelineError(f"Ollama request failed: {exc}") from exc
        results.append({
            "frame_indices": [item["index"] for item in batch],
            "timestamps_seconds": [item["timestamp_seconds"] for item in batch],
            "response": model_response.get("message", {}).get("content", ""),
        })

    destination = output / "knowledge.json"
    destination.write_text(json.dumps({
        "model": model,
        "endpoint": endpoint,
        "source_manifest": "manifest.json",
        "transcript_alignment": None if transcript_alignment is None else str(transcript_alignment),
        "results": results,
        "warning": "Model output is an unvalidated hypothesis until checked against the source.",
    }, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return destination


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="vkx",
        description="Inspect videos, preserve decoded frames, align transcripts, and optionally query a local VLM.",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    inspect = commands.add_parser("probe", help="Report container, frame-rate, bitrate, and stream metadata.")
    inspect.add_argument("video", type=Path)

    extract = commands.add_parser("extract", help="Extract frames and write an auditable timestamp manifest.")
    extract.add_argument("video", type=Path)
    extract.add_argument("--out", type=Path, required=True)
    extract.add_argument("--sample-fps", type=float, help="Uniform sampling rate; omit to decode every source frame.")
    extract.add_argument(
        "--dedupe-hamming", type=int, default=0,
        help="Drop only exact decoded duplicates at 0; positive values enable lossy 64-bit perceptual deduplication.",
    )
    extract.add_argument("--transcript", type=Path, help="Optional time-coded SRT or WebVTT transcript.")

    audio = commands.add_parser("audio", help="Extract mono PCM audio for a separate ASR system.")
    audio.add_argument("video", type=Path)
    audio.add_argument("--out", type=Path, required=True)
    audio.add_argument("--sample-rate", type=int, default=16000)

    learn = commands.add_parser("learn", help="Ask a local Ollama vision-language model to inspect selected frames.")
    learn.add_argument("--run-dir", type=Path, required=True)
    learn.add_argument("--model", default="qwen2.5vl:3b")
    learn.add_argument("--endpoint", default="http://localhost:11434")
    learn.add_argument("--batch-size", type=int, default=8)
    learn.add_argument("--transcript-alignment", type=Path)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        if args.command == "probe":
            print(json.dumps(probe_media(args.video), indent=2, ensure_ascii=False))
        elif args.command == "extract":
            result = extract_frames(
                args.video, args.out, args.sample_fps, args.dedupe_hamming, args.transcript,
            )
            print(json.dumps({
                "output": str(args.out.resolve()),
                "candidate_frames": result["candidate_frame_count"],
                "selected_frames": result["selected_frame_count"],
                "manifest": str((args.out / "manifest.json").resolve()),
            }, indent=2, ensure_ascii=False))
        elif args.command == "audio":
            extract_audio(args.video, args.out, args.sample_rate)
            print(args.out.resolve())
        elif args.command == "learn":
            if args.batch_size < 1:
                raise PipelineError("--batch-size must be at least 1.")
            result = run_ollama(
                args.run_dir, args.model, args.endpoint, args.batch_size,
                args.transcript_alignment,
            )
            print(result.resolve())
        return 0
    except (PipelineError, OSError, ValueError) as exc:
        print(f"vkx: error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
