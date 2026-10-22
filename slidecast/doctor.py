"""Setup check: ``python -m slidecast.doctor`` (``pixi run doctor``).

Run by the installer after the environment is in place. It downloads the voice model up front,
so the first narration does not stall on a 3.8 GB download, and reports whether the GPU, ffmpeg
and LibreOffice are usable - in plain words, since the person reading it is not a developer.
Exit code 1 means something Slidecast cannot work without is missing.
"""

from __future__ import annotations

import logging
import os
import sys
import warnings

from slidecast.core.logging_setup import configure_logging

OK, WARN, FAIL = "ok", "warn", "fail"
_MARK = {OK: "  [ok]  ", WARN: "  [!]   ", FAIL: "  [X]   "}


def report(state: str, text: str) -> str:
    print(_MARK[state] + text, flush=True)
    return state


def check_gpu() -> str:
    try:
        import torch
    except Exception as exc:
        return report(FAIL, f"PyTorch could not be loaded: {exc}")
    if not torch.cuda.is_available():
        return report(
            WARN,
            "No usable NVIDIA graphics card. Slidecast will run on the processor, which is much "
            "slower - set Processor to 'cpu' in the Voice step. If you do have an NVIDIA card, "
            "update its driver from nvidia.com and restart the PC.",
        )
    name = torch.cuda.get_device_name(0)
    memory = torch.cuda.get_device_properties(0).total_memory / 2**30
    return report(OK, f"Graphics card: {name} ({memory:.0f} GB)")


def check_ffmpeg() -> str:
    from slidecast.core import ffmpeg as F
    from slidecast.core.errors import SlidecastError

    try:
        path = F.find_ffmpeg()
    except SlidecastError:
        return report(FAIL, "ffmpeg was not found - videos cannot be made without it.")
    missing = [e for e in ("libsvtav1", "libx265") if e not in F.probe_encoders(path)]
    if missing:
        return report(WARN, f"ffmpeg {path} lacks {', '.join(missing)}; some formats will fail.")
    return report(OK, f"ffmpeg: {path}")


def check_libreoffice() -> str:
    from slidecast.core import render as R
    from slidecast.core.errors import SlidecastError

    try:
        path = R.find_soffice()
    except SlidecastError:
        return report(FAIL, "LibreOffice was not found - slides cannot be turned into images.")
    return report(OK, f"LibreOffice: {path}")


def check_powerpoint() -> str:
    if sys.platform != "win32":
        return OK
    import winreg

    try:
        winreg.CloseKey(winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, "PowerPoint.Application"))
    except OSError:
        return report(
            WARN,
            "Microsoft PowerPoint is not installed: slides become still images, so animations "
            "and embedded videos are not kept. Everything else works.",
        )
    return report(OK, "Microsoft PowerPoint: animations and videos are kept")


def fetch_model() -> str:
    from slidecast.core.config import TTSSettings
    from slidecast.core.errors import SlidecastError
    from slidecast.core.tts import ChatterboxTurboBackend

    backend = ChatterboxTurboBackend(TTSSettings())
    last = [-1]

    def status(message: str, fraction: float | None) -> None:
        if fraction is None:
            return
        pct = int(fraction * 100)
        if pct >= last[0] + 5:  # a line per 5%, not a flood
            last[0] = pct
            # The console code page cannot always show an em dash.
            print("        " + message.replace("—", "-"), flush=True)

    backend.on_status = status
    try:
        backend.resolve_model_id()
    except SlidecastError as exc:
        return report(FAIL, f"The voice model could not be downloaded: {exc}")
    return report(OK, "Voice model downloaded")


def quiet_hugging_face() -> None:
    """Hide Hugging Face's advice for developers (symlinks, access tokens): it does not apply to
    a one-off download, and to someone installing an app it reads like an error."""
    os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
    warnings.filterwarnings("ignore", module="huggingface_hub")
    logging.getLogger("huggingface_hub").setLevel(logging.ERROR)


def main() -> int:
    configure_logging(None, console=False)
    quiet_hugging_face()
    print("Checking what Slidecast needs...", flush=True)
    results = [check_gpu(), check_ffmpeg(), check_libreoffice(), check_powerpoint()]
    print("Downloading the voice model (3.8 GB, only the first time)...", flush=True)
    results.append(fetch_model())
    return 1 if FAIL in results else 0


if __name__ == "__main__":
    sys.exit(main())
