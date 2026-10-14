"""ffmpeg discovery, encoder probing/selection, command construction and execution.

Command construction is pure (no subprocess) so it can be unit-tested; execution parses
``-progress pipe:1`` for live progress and honours cancellation.
"""

from __future__ import annotations

import glob
import json
import os
import re
import shutil
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from slidecast.core import proc
from slidecast.core.config import ExportSettings
from slidecast.core.errors import Cancelled, EncoderUnavailable, FFmpegError, FFmpegNotFound
from slidecast.core.logging_setup import get_logger

log = get_logger("ffmpeg")

ENCODERS: dict[str, dict[str, str]] = {
    "av1": {"nvenc": "av1_nvenc", "software": "libsvtav1"},
    "hevc": {"nvenc": "hevc_nvenc", "software": "libx265"},
}
DEFAULT_PRESETS: dict[str, str] = {
    "av1_nvenc": "p5",
    "hevc_nvenc": "p5",
    "libsvtav1": "6",
    "libx265": "medium",
}


def find_ffmpeg(explicit: Path | None = None) -> Path:
    searched: list[str] = []
    if explicit:
        searched.append(str(explicit))
        if Path(explicit).is_file():
            return Path(explicit)
    env = os.environ.get("SLIDECAST_FFMPEG")
    if env:
        searched.append(f"$SLIDECAST_FFMPEG={env}")
        if Path(env).is_file():
            return Path(env)
    found = shutil.which("ffmpeg")
    searched.append("PATH:ffmpeg")
    if found:
        return Path(found)
    if sys.platform == "win32":
        patterns = [
            os.path.expandvars(
                r"%LOCALAPPDATA%\Microsoft\WinGet\Packages\Gyan.FFmpeg*\*\bin\ffmpeg.exe"
            ),
            r"C:\ffmpeg\bin\ffmpeg.exe",
            r"C:\Program Files\ffmpeg\bin\ffmpeg.exe",
        ]
        for pat in patterns:
            searched.append(pat)
            hits = sorted(glob.glob(pat))
            if hits:
                return Path(hits[-1])
    raise FFmpegNotFound(searched)


def ffmpeg_version(ffmpeg: Path) -> str:
    try:
        out = proc.run([str(ffmpeg), "-version"], timeout=15)
    except OSError as exc:
        return f"unknown ({exc})"
    first = out.stdout.splitlines()[0] if out.stdout else "?"
    m = re.match(r"ffmpeg version (\S+)", first)
    return m.group(1) if m else first


_ENCODER_LINE = re.compile(r"^\s*[VAS][.F][.S][.X][.B][.D]\s+(\S+)\s")


def parse_encoders(output: str) -> set[str]:
    names: set[str] = set()
    for line in output.splitlines():
        m = _ENCODER_LINE.match(line)
        if m:
            names.add(m.group(1))
    return names


def probe_encoders(ffmpeg: Path) -> set[str]:
    try:
        out = proc.run([str(ffmpeg), "-hide_banner", "-encoders"], timeout=60)
    except OSError as exc:
        raise FFmpegError(f"Could not run ffmpeg -encoders: {exc}") from exc
    if out.timed_out:
        raise FFmpegError("ffmpeg -encoders did not answer within 60 seconds")
    return parse_encoders(out.stdout)


def nvenc_smoke_test(ffmpeg: Path, encoder: str) -> bool:
    """Encode three black frames to /dev/null; False if the hardware encoder cannot initialise."""
    cmd = [
        str(ffmpeg),
        "-hide_banner",
        "-v",
        "error",
        "-nostdin",
        "-f",
        "lavfi",
        "-i",
        "color=c=black:s=256x256:r=30:d=0.2",
        "-frames:v",
        "3",
        "-c:v",
        encoder,
        "-f",
        "null",
        "-",
    ]
    try:
        result = proc.run(cmd, timeout=60)
    except OSError as exc:
        log.warning("%s smoke test failed to run: %s", encoder, exc)
        return False
    if result.timed_out:
        log.info("%s not usable: smoke test timed out", encoder)
        return False
    if result.returncode != 0:
        log.info("%s not usable: hardware encoder failed to start on this GPU", encoder)
        log.debug("%s smoke test stderr: %s", encoder, result.stderr.strip()[-300:])
        return False
    return True


@dataclass(frozen=True)
class EncoderChoice:
    name: str
    hardware: bool
    reason: str


def encoder_candidates(codec: str, mode: str) -> list[str]:
    table = ENCODERS[codec]
    if mode == "nvenc":
        return [table["nvenc"]]
    if mode == "software":
        return [table["software"]]
    return [table["nvenc"], table["software"]]


def select_encoder(
    codec: str,
    mode: str,
    available: set[str],
    smoke_test: Callable[[str], bool] | None = None,
) -> EncoderChoice:
    """Pick the first candidate that exists (and passes the smoke test for NVENC)."""
    tried: list[str] = []
    for name in encoder_candidates(codec, mode):
        hw = name.endswith("_nvenc")
        if name not in available:
            tried.append(f"{name} (not in this ffmpeg build)")
            continue
        if hw and smoke_test is not None and not smoke_test(name):
            tried.append(f"{name} (failed to initialise on this GPU)")
            continue
        reason = "hardware encoder" if hw else "software encoder"
        if tried:
            reason += "; fell back after: " + ", ".join(tried)
        return EncoderChoice(name=name, hardware=hw, reason=reason)
    raise EncoderUnavailable(
        f"No usable {codec.upper()} encoder for mode '{mode}'. Tried: " + ", ".join(tried)
    )


def video_encoder_args(encoder: str, cq: int, preset: str, gop: int) -> list[str]:
    preset = preset or DEFAULT_PRESETS.get(encoder, "")
    if encoder in ("av1_nvenc", "hevc_nvenc"):
        args = [
            "-c:v",
            encoder,
            "-preset",
            preset,
            "-tune",
            "hq",
            "-rc",
            "vbr",
            "-cq",
            str(cq),
            "-b:v",
            "0",
            "-g",
            str(gop),
        ]
    elif encoder == "libsvtav1":
        args = [
            "-c:v",
            "libsvtav1",
            "-crf",
            str(cq),
            "-preset",
            preset,
            "-g",
            str(gop),
            "-svtav1-params",
            "tune=0",
        ]
    elif encoder == "libx265":
        args = [
            "-c:v",
            "libx265",
            "-crf",
            str(cq),
            "-preset",
            preset,
            "-g",
            str(gop),
            "-x265-params",
            "log-level=error",
        ]
    else:
        raise EncoderUnavailable(f"Unsupported encoder {encoder}")
    if encoder in ("hevc_nvenc", "libx265"):
        args += ["-tag:v", "hvc1"]
    return args


def audio_encoder_args(codec: str, bitrate_kbps: int, channels: int, container: str) -> list[str]:
    if codec == "opus":
        args = [
            "-c:a",
            "libopus",
            "-b:a",
            f"{bitrate_kbps}k",
            "-ac",
            str(channels),
            "-application",
            "audio",
        ]
        if container == "mp4":
            args += ["-strict", "-2"]
        return args
    if codec == "aac":
        return ["-c:a", "aac", "-b:a", f"{bitrate_kbps}k", "-ac", str(channels)]
    raise EncoderUnavailable(f"Unsupported audio codec {codec}")


def build_mux_command(
    ffmpeg: Path,
    concat_list: Path,
    audio: Path,
    output: Path,
    settings: ExportSettings,
    encoder: str,
    width: int,
    height: int,
    total_frames: int | None = None,
) -> list[str]:
    """Full ffmpeg command: concat-demuxed stills + wav -> encoded video with progress output."""
    gop = max(round(settings.keyint_seconds * settings.fps), 1)
    cmd = [
        str(ffmpeg),
        "-y",
        "-hide_banner",
        "-nostdin",
        "-nostats",
        "-loglevel",
        "error",
        "-progress",
        "pipe:1",
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        str(concat_list),
        "-i",
        str(audio),
        "-map",
        "0:v:0",
        "-map",
        "1:a:0",
        "-vf",
        f"scale={width}:{height}:flags=lanczos,format=yuv420p",
        "-r",
        str(settings.fps),
        "-fps_mode",
        "cfr",
        "-pix_fmt",
        "yuv420p",
        *video_encoder_args(encoder, settings.cq, settings.preset, gop),
        *audio_encoder_args(
            settings.resolved_audio_codec(),
            settings.audio_bitrate_kbps,
            settings.audio_channels,
            settings.container,
        ),
    ]
    if total_frames is not None:
        cmd += ["-frames:v", str(total_frames)]
    if settings.container == "mp4":
        cmd += ["-movflags", "+faststart"]
    # Explicit muxer: the pipeline encodes to a .part file and publishes by rename.
    cmd += ["-f", "mp4" if settings.container == "mp4" else "matroska", str(output)]
    return cmd


def write_concat_list(path: Path, frames: list[tuple[Path, float]]) -> Path:
    """concat demuxer list. The final entry is repeated without duration (demuxer quirk)."""
    lines = ["ffconcat version 1.0"]
    for img, dur in frames:
        lines.append(f"file '{_concat_escape(img)}'")
        lines.append(f"duration {dur:.6f}")
    if frames:
        lines.append(f"file '{_concat_escape(frames[-1][0])}'")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _concat_escape(p: Path) -> str:
    return str(Path(p).resolve()).replace("\\", "/").replace("'", r"'\''")


_OUT_TIME = re.compile(r"^out_time_us=(\d+)")


def run_ffmpeg(
    cmd: list[str],
    total_seconds: float,
    progress: Callable[[float], None] | None = None,
    cancel: Callable[[], bool] | None = None,
) -> None:
    log.debug("ffmpeg command: %s", " ".join(cmd))

    def _on_line(line: str) -> None:
        m = _OUT_TIME.match(line.strip())
        if m and progress and total_seconds > 0:
            progress(min(int(m.group(1)) / 1e6 / total_seconds, 1.0))

    try:
        result = proc.run(cmd, cancel=cancel, on_stdout=_on_line)
    except OSError as exc:
        raise FFmpegError(f"Could not start ffmpeg: {exc}") from exc
    if result.cancelled:
        raise Cancelled("Export cancelled")
    if result.returncode != 0:
        raise FFmpegError(
            f"ffmpeg exited with code {result.returncode}:\n{result.stderr.strip()[-3000:]}"
        )
    if progress:
        progress(1.0)


def estimate_output_bytes(
    duration_s: float, settings: ExportSettings, width: int, height: int, transitions: int = 0
) -> int:
    """Rough size estimate for slide-show content (mostly static frames)."""
    pixel_factor = (width * height) / (1920 * 1080)
    base_kbps = {"av1": 180.0, "hevc": 260.0}[settings.video_codec]
    quality_factor = 2.0 ** ((30 - settings.cq) / 6.0)
    video_kbps = base_kbps * pixel_factor * quality_factor
    # Each dissolve is a burst of moving pixels; charge ~1.5 s of "real video" per transition.
    burst_bits = transitions * 1.5 * 2500.0 * pixel_factor * quality_factor * 1000
    video_bits = video_kbps * 1000 * duration_s + burst_bits
    audio_bits = settings.audio_bitrate_kbps * 1000 * duration_s
    return int((video_bits + audio_bits) / 8)


# ---------------------------------------------------------------------------------------------
# Video-clip composition (PowerPoint slide source: animations, transitions, embedded media)
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class MediaInfo:
    duration: float
    has_audio: bool


def probe_media(ffmpeg: Path, path: Path) -> MediaInfo:
    """Duration and audio presence of a clip, via the ffprobe next to ``ffmpeg``."""
    ffprobe = ffmpeg.with_name(ffmpeg.name.replace("ffmpeg", "ffprobe"))
    if not ffprobe.is_file():
        ffprobe = Path(shutil.which("ffprobe") or "ffprobe")
    cmd = [
        str(ffprobe),
        "-v",
        "error",
        "-show_entries",
        "format=duration:stream=codec_type",
        "-of",
        "json",
        str(path),
    ]
    try:
        result = proc.run(cmd, timeout=60)
        data = json.loads(result.stdout or "{}")
    except (OSError, json.JSONDecodeError) as exc:
        raise FFmpegError(f"Could not probe {path.name}: {exc}") from exc
    if result.timed_out:
        raise FFmpegError(f"Could not probe {path.name}: ffprobe timed out")
    try:
        duration = float(data["format"]["duration"])
    except (KeyError, TypeError, ValueError) as exc:
        raise FFmpegError(f"{path.name} has no readable duration") from exc
    has_audio = any(s.get("codec_type") == "audio" for s in data.get("streams", []))
    return MediaInfo(duration=duration, has_audio=has_audio)


@dataclass(frozen=True)
class ClipSegment:
    """One stretch of the finished video.

    A slide contributes either a single ``play`` segment (its clip), or - when its video has a
    soundtrack - a still ``poster`` segment covering the narration followed by the clip, so the
    two are never talking over each other. The poster is a PNG of the slide after its opening
    transition, which is why the narration is not held over a black fade-in frame.
    """

    source: Path
    duration: float
    is_image: bool = False
    use_clip_audio: bool = False
    # Repeat the source to fill the slide. Only used for a silent video that is shorter than the
    # narration: PowerPoint plays embedded media exactly once and then holds the last frame,
    # whatever the deck's loop setting says, so the repeat has to happen here.
    loop: bool = False


def build_clip_mux_command(
    ffmpeg: Path,
    segments: list[ClipSegment],
    narration: Path,
    output: Path,
    settings: ExportSettings,
    encoder: str,
    width: int,
    height: int,
    media_volume: float = 1.0,
) -> list[str]:
    """Concatenate slide segments into the finished video.

    Still segments hold a poster image for the narration; clip segments play the slide, which
    PowerPoint has already rendered to the slide's own length. Narration is mixed over the top
    and is silent wherever a video plays, so nothing overlaps.
    """
    if not segments:
        raise FFmpegError("No slide clips to encode")
    gop = max(round(settings.keyint_seconds * settings.fps), 1)
    fps = settings.fps
    cmd: list[str] = [
        str(ffmpeg),
        "-y",
        "-hide_banner",
        "-nostdin",
        "-nostats",
        "-loglevel",
        "error",
        "-progress",
        "pipe:1",
    ]
    for seg in segments:
        if seg.is_image:
            cmd += ["-loop", "1", "-framerate", str(fps), "-t", f"{seg.duration:.3f}"]
        elif seg.loop:
            cmd += ["-stream_loop", "-1"]
        cmd += ["-i", str(seg.source)]
    narration_idx = len(segments)
    cmd += ["-i", str(narration)]

    parts: list[str] = []
    labels: list[str] = []
    for n, seg in enumerate(segments):
        d = f"{seg.duration:.3f}"
        base = (
            f"[{n}:v]scale={width}:{height}:force_original_aspect_ratio=decrease,"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={fps}"
        )
        if seg.is_image or seg.loop:
            parts.append(f"{base},trim=duration={d},setpts=PTS-STARTPTS[v{n}]")
        else:
            # A clip is rendered to fit its slide, so the trim is what normally applies. The pad
            # is the safety net for a clip that came back a few frames short.
            parts.append(
                f"{base},setpts=PTS-STARTPTS,tpad=stop_mode=clone:stop_duration={d},"
                f"trim=duration={d},setpts=PTS-STARTPTS[v{n}]"
            )
        if seg.use_clip_audio:
            parts.append(
                f"[{n}:a]aresample=48000,apad,atrim=duration={d},"
                f"asetpts=PTS-STARTPTS,volume={media_volume:.3f}[a{n}]"
            )
        else:
            parts.append(
                f"anullsrc=channel_layout=stereo:sample_rate=48000,"
                f"atrim=duration={d},asetpts=PTS-STARTPTS[a{n}]"
            )
        labels.append(f"[v{n}][a{n}]")

    parts.append(f"{''.join(labels)}concat=n={len(segments)}:v=1:a=1[vcat][acat]")
    parts.append(f"[{narration_idx}:a]aresample=48000[narr]")
    parts.append("[acat][narr]amix=inputs=2:duration=first:normalize=0[aout]")
    parts.append("[vcat]format=yuv420p[vout]")

    cmd += [
        "-filter_complex",
        ";".join(parts),
        "-map",
        "[vout]",
        "-map",
        "[aout]",
        "-r",
        str(fps),
        "-fps_mode",
        "cfr",
        "-pix_fmt",
        "yuv420p",
        *video_encoder_args(encoder, settings.cq, settings.preset, gop),
        *audio_encoder_args(
            settings.resolved_audio_codec(),
            settings.audio_bitrate_kbps,
            settings.audio_channels,
            settings.container,
        ),
    ]
    if settings.container == "mp4":
        cmd += ["-movflags", "+faststart"]
    cmd += ["-f", "mp4" if settings.container == "mp4" else "matroska", str(output)]
    return cmd


_BLACK_END = re.compile(r"black_start:\s*0(?:\.0+)?\s+black_end:\s*([0-9.]+)")


def first_visible_time(ffmpeg: Path, video: Path, scan: float = 4.0, margin: float = 0.05) -> float:
    """Seconds until the picture is no longer black.

    A PowerPoint clip opens on the slide's transition, which fades in from black, so frame 0 is
    useless as a preview. This finds the end of that opening fade.
    """
    cmd = [
        str(ffmpeg),
        "-hide_banner",
        "-nostdin",
        "-t",
        f"{scan:.3f}",
        "-i",
        str(video),
        "-vf",
        "blackdetect=d=0.02:pix_th=0.10",
        "-an",
        "-f",
        "null",
        "-",
    ]
    try:
        result = proc.run(cmd, timeout=120)
    except OSError as exc:
        log.debug("blackdetect failed for %s: %s", video.name, exc)
        return 0.0
    match = _BLACK_END.search(result.stderr)
    if not match:
        return 0.0
    return min(float(match.group(1)) + margin, scan)


def extract_segment(ffmpeg: Path, video: Path, out: Path, start: float, duration: float) -> Path:
    """Cut ``duration`` seconds starting at ``start`` into its own file (cached).

    Used to isolate the part of a slide clip worth repeating, so a repeat never replays the
    slide's opening transition and the fade from black that goes with it.
    """
    out = Path(out)
    if out.is_file():
        return out
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(f".{out.name}.tmp{out.suffix}")
    cmd = [
        str(ffmpeg), "-y", "-hide_banner", "-loglevel", "error", "-nostdin",
        "-ss", f"{max(start, 0.0):.3f}", "-t", f"{max(duration, 0.1):.3f}", "-i", str(video),
        "-an", "-c:v", "libx264", "-crf", "16", "-preset", "veryfast", "-pix_fmt", "yuv420p",
        str(tmp),
    ]  # fmt: skip
    try:
        result = proc.run(cmd, timeout=600)
    except OSError as exc:
        raise FFmpegError(f"Could not cut a loop from {video.name}: {exc}") from exc
    if result.returncode != 0 or result.timed_out or not tmp.is_file():
        tmp.unlink(missing_ok=True)
        detail = "timed out" if result.timed_out else result.stderr.strip()[-300:]
        raise FFmpegError(f"Could not cut a loop from {video.name}: {detail}")
    os.replace(tmp, out)
    return out


def extract_frame(
    ffmpeg: Path, video: Path, out_png: Path, at: float, width: int | None = None
) -> Path:
    """Write a single frame from ``video`` at ``at`` seconds. Existing files are reused."""
    out_png = Path(out_png)
    if out_png.is_file():
        return out_png
    out_png.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_png.with_name(f".{out_png.name}.tmp.png")
    cmd = [
        str(ffmpeg),
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-ss",
        f"{max(at, 0.0):.3f}",
        "-i",
        str(video),
        "-frames:v",
        "1",
    ]
    if width is not None:
        cmd += ["-vf", f"scale={width}:-2:flags=bilinear"]
    cmd += [str(tmp)]
    try:
        result = proc.run(cmd, timeout=120)
    except OSError as exc:
        raise FFmpegError(f"Could not read a frame from {video.name}: {exc}") from exc
    if result.returncode != 0 or result.timed_out or not tmp.is_file():
        tmp.unlink(missing_ok=True)
        detail = "timed out" if result.timed_out else result.stderr.strip()[-300:]
        raise FFmpegError(f"Could not read a frame from {video.name}: {detail}")
    os.replace(tmp, out_png)
    return out_png


def extract_poster(ffmpeg: Path, video: Path, out_png: Path) -> Path:
    """The slide once its opening transition has finished - the still that holds on screen while
    the narration plays, before the slide's own video starts."""
    return extract_frame(ffmpeg, video, out_png, first_visible_time(ffmpeg, video))


def extract_thumbnail(
    ffmpeg: Path,
    video: Path,
    out_png: Path,
    duration: float | None = None,
    position: float = 0.6,
    width: int = 256,
) -> Path:
    """Grab one representative frame from a slide clip.

    Sampled part-way in rather than at the start, so builds have played and a slide holding a
    video shows the video rather than its opening fade.
    """
    if duration is None:
        duration = probe_media(ffmpeg, video).duration
    at = max(min(duration * position, max(duration - 0.1, 0.0)), 0.0)
    return extract_frame(ffmpeg, video, out_png, at, width=width)
