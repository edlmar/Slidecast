"""Pydantic settings models shared by the CLI, the GUI and the pipeline."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, field_validator

VideoCodec = Literal["av1", "hevc"]
# "stills": LibreOffice -> PDF -> PNG (fast, flattens animations/embedded video).
# "video":  PowerPoint CreateVideo per slide (keeps animations, transitions, media).
SlideSource = Literal["auto", "stills", "video"]
EncoderMode = Literal["auto", "nvenc", "software"]
Container = Literal["mp4", "mkv"]
AudioCodec = Literal["auto", "opus", "aac"]

# Default abbreviation expansions. Keys are matched on word boundaries, case-sensitively.
# Medicine + computer science flavoured; users can extend/override per project.
DEFAULT_ABBREVIATIONS: dict[str, str] = {
    "e.g.": "for example",
    "i.e.": "that is",
    "etc.": "et cetera",
    "vs.": "versus",
    "vs": "versus",
    "approx.": "approximately",
    "Fig.": "Figure",
    "Figs.": "Figures",
    "Eq.": "Equation",
    "Dr.": "Doctor",
    "Prof.": "Professor",
    "No.": "Number",
    "w/": "with",
    "w/o": "without",
    "b/c": "because",
    "min.": "minutes",
    "max.": "maximum",
    "mg": "milligrams",
    "mcg": "micrograms",
    "kg": "kilograms",
    "mL": "milliliters",
    "ml": "milliliters",
    "dL": "deciliters",
    "mmHg": "millimeters of mercury",
    "bpm": "beats per minute",
    "IV": "intravenous",
    "IM": "intramuscular",
    "PO": "by mouth",
    "BID": "twice daily",
    "TID": "three times daily",
    "QID": "four times daily",
    "PRN": "as needed",
    "Hx": "history",
    "Dx": "diagnosis",
    "Tx": "treatment",
    "Rx": "prescription",
    "Sx": "symptoms",
    "y/o": "year old",
    "GHz": "gigahertz",
    "MHz": "megahertz",
    "GB": "gigabytes",
    "MB": "megabytes",
    "KB": "kilobytes",
    "TB": "terabytes",
    "ms": "milliseconds",
    "OS": "operating system",
    "DB": "database",
    "ML": "machine learning",
    "NLP": "natural language processing",
    "SQL": "sequel",
    "API": "A P I",
    "CPU": "C P U",
    "GPU": "G P U",
    "HTTP": "H T T P",
    "URL": "U R L",
    "JSON": "jason",
    "CLI": "C L I",
    "GUI": "gooey",
    "I/O": "I O",
    "EHR": "electronic health record",
    "ICU": "I C U",
    "ECG": "E C G",
    "EKG": "E K G",
    "MRI": "M R I",
    "CT": "C T",
    "BP": "blood pressure",
    "HR": "heart rate",
    "RR": "respiratory rate",
    "CBC": "complete blood count",
    "WBC": "white blood cell",
    "RBC": "red blood cell",
    "DNA": "D N A",
    "RNA": "R N A",
    "AI": "A I",
}


class RenderSettings(BaseModel):
    """Slide rasterisation settings."""

    width: int = Field(1920, ge=320, le=7680)
    height: int = Field(1080, ge=240, le=4320)
    background: str = Field("#000000", description="Letterbox colour as #RRGGBB")
    soffice_path: Path | None = Field(None, description="Explicit path to soffice(.exe)")
    slide_source: SlideSource = Field(
        "auto",
        description="auto = video clips when the deck has animations/media and PowerPoint is "
        "available, else stills",
    )
    media_after_narration: bool = Field(
        True,
        description="A slide video that has sound plays after the narration instead of under it",
    )
    media_volume: float = Field(
        1.0, ge=0.0, le=2.0, description="Level of a slide's own soundtrack under the narration"
    )

    @field_validator("background")
    @classmethod
    def _check_hex(cls, v: str) -> str:
        if len(v) != 7 or not v.startswith("#"):
            raise ValueError("background must be #RRGGBB")
        int(v[1:], 16)
        return v


class TextSettings(BaseModel):
    """Notes-cleaning and chunking settings."""

    chunk_chars: int = Field(250, ge=60, le=600)
    expand_abbreviations: bool = True
    abbreviations: dict[str, str] = Field(default_factory=lambda: dict(DEFAULT_ABBREVIATIONS))
    speak_urls: bool = True
    words_per_minute: int = Field(150, ge=60, le=300, description="For duration estimates")


class TTSSettings(BaseModel):
    """Chatterbox-Turbo generation settings. Only knobs the Turbo model honours are here."""

    reference_clip: Path | None = None
    temperature: float = Field(0.8, ge=0.05, le=2.0)
    top_p: float = Field(0.95, ge=0.1, le=1.0)
    top_k: int = Field(1000, ge=1, le=5000)
    repetition_penalty: float = Field(1.2, ge=1.0, le=3.0)
    seed: int = Field(42, ge=0, description="Base seed; each chunk derives its own from it")
    inter_sentence_gap: float = Field(0.25, ge=0.0, le=3.0, description="Seconds between chunks")
    inter_slide_gap: float = Field(0.6, ge=0.0, le=5.0, description="Seconds between slides")
    empty_slide_hold: float = Field(3.0, ge=0.5, le=60.0, description="Seconds for empty notes")
    device: str = "cuda"
    unload_when_idle: bool = False

    def cache_params(self) -> dict[str, object]:
        """Parameters that affect the synthesized audio of a single slide."""
        return {
            "temperature": self.temperature,
            "top_p": self.top_p,
            "top_k": self.top_k,
            "repetition_penalty": self.repetition_penalty,
            "seed": self.seed,
            "inter_sentence_gap": self.inter_sentence_gap,
            "device": self.device,
        }


class AudioPostSettings(BaseModel):
    """Post-processing applied to chunk audio and to the final track."""

    trim_silence: bool = True
    trim_threshold_db: float = Field(-40.0, le=0.0)
    trim_pad_ms: int = Field(60, ge=0, le=1000)
    declick: bool = True
    declick_fade_ms: int = Field(5, ge=1, le=50)
    target_lufs: float = Field(-16.0, le=0.0)
    true_peak_db: float = Field(-1.0, le=0.0)

    def cache_params(self) -> dict[str, object]:
        """Chunk-level post settings that change the cached per-slide audio."""
        return {
            "trim_silence": self.trim_silence,
            "trim_threshold_db": self.trim_threshold_db,
            "trim_pad_ms": self.trim_pad_ms,
            "declick": self.declick,
            "declick_fade_ms": self.declick_fade_ms,
        }


class ExportSettings(BaseModel):
    """Video/audio encoding and muxing settings."""

    fps: int = Field(30, ge=1, le=120)
    video_codec: VideoCodec = "av1"
    encoder_mode: EncoderMode = "auto"
    cq: int = Field(30, ge=0, le=63, description="Constant quality (NVENC cq / SVT crf)")
    preset: str = Field("", description="Encoder preset; empty = sensible default")
    keyint_seconds: float = Field(2.0, ge=0.1, le=30.0)
    container: Container = "mp4"
    audio_codec: AudioCodec = "auto"
    audio_bitrate_kbps: int = Field(128, ge=32, le=512)
    audio_channels: Literal[1, 2] = 1
    transition_seconds: float = Field(0.4, ge=0.0, le=3.0)
    ffmpeg_path: Path | None = None
    output_dir: Path | None = Field(
        None, description="Where the video goes; None = <project>/output"
    )

    def resolved_audio_codec(self) -> Literal["opus", "aac"]:
        if self.audio_codec != "auto":
            return self.audio_codec
        return "aac" if self.container == "mp4" else "opus"


class ProjectSettings(BaseModel):
    """Everything a project needs to be reproducible."""

    render: RenderSettings = Field(default_factory=RenderSettings)
    text: TextSettings = Field(default_factory=TextSettings)
    tts: TTSSettings = Field(default_factory=TTSSettings)
    audio: AudioPostSettings = Field(default_factory=AudioPostSettings)
    export: ExportSettings = Field(default_factory=ExportSettings)
    skip_hidden_slides: bool = True
