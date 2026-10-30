import subprocess
import json
import math
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

from backend.app.core.config import settings


class MediaProcessingError(RuntimeError):
    pass


class MediaService:
    supported_extensions = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".mp3", ".wav", ".m4a", ".flac", ".ogg"}

    def save_upload(self, source, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        total = 0
        try:
            with destination.open("wb") as output:
                while chunk := source.file.read(1024 * 1024):
                    total += len(chunk)
                    if total > settings.max_upload_bytes:
                        raise MediaProcessingError("Uploaded file exceeds the maximum allowed size")
                    output.write(chunk)
            self.validate_magic(destination)
        except Exception:
            destination.unlink(missing_ok=True)
            raise

    def validate_magic(self, media_path: Path) -> None:
        import filetype

        kind = filetype.guess(str(media_path))
        if kind is None:
            raise MediaProcessingError("Uploaded file content is not a supported media format")
        suffix = media_path.suffix.lower()
        expected = {
            ".mp4": {"mp4"},
            ".mov": {"mov", "mp4"},
            ".mkv": {"mkv"},
            ".webm": {"webm"},
            ".avi": {"avi"},
            ".mp3": {"mp3"},
            ".wav": {"wav"},
            ".m4a": {"m4a", "mp4"},
            ".flac": {"flac"},
            ".ogg": {"ogg"},
        }
        if kind.extension not in expected.get(suffix, set()):
            raise MediaProcessingError("Uploaded file content does not match its media extension")

    def get_duration(self, media_path: Path) -> float:
        command = [
            settings.ffprobe_binary,
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(media_path),
        ]
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                check=False,
                timeout=settings.subprocess_timeout_seconds,
            )
        except OSError as exc:
            raise MediaProcessingError("FFprobe is not installed or cannot be started") from exc
        except subprocess.TimeoutExpired as exc:
            raise MediaProcessingError("Media duration probe timed out") from exc
        try:
            duration = float(result.stdout.strip())
        except ValueError as exc:
            raise MediaProcessingError("Unable to determine media duration") from exc
        if result.returncode != 0 or not math.isfinite(duration) or duration < 0:
            detail = result.stderr.strip().splitlines()[-1] if result.stderr.strip() else "unknown FFprobe error"
            raise MediaProcessingError(f"Media duration probe failed: {detail}")
        if duration > settings.max_media_duration_seconds:
            raise MediaProcessingError("Media exceeds the maximum allowed duration")
        return duration

    @staticmethod
    def validate_youtube_url(url: str) -> None:
        parsed = urlsplit(url)
        if (
            parsed.scheme not in {"http", "https"}
            or parsed.hostname not in {"youtube.com", "www.youtube.com", "youtu.be"}
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port not in {None, 80 if parsed.scheme == "http" else 443}
        ):
            raise MediaProcessingError("Only YouTube URLs are supported")

    def get_youtube_duration(self, url: str) -> float:
        self.validate_youtube_url(url)
        command = [
            sys.executable,
            "-m",
            "yt_dlp",
            "--dump-single-json",
            "--skip-download",
            "--no-playlist",
            "--no-warnings",
            "--no-call-home",
            "--allowed-extractors",
            "youtube",
            url,
        ]
        result = self._run_ytdlp(command)
        try:
            duration = float(json.loads(result.stdout)["duration"])
        except (ValueError, KeyError, TypeError) as exc:
            raise MediaProcessingError("Unable to determine YouTube video duration") from exc
        if not math.isfinite(duration) or duration < 0 or duration > settings.max_media_duration_seconds:
            raise MediaProcessingError("Media exceeds the maximum allowed duration")
        return duration

    @staticmethod
    def _run_ytdlp(command: list[str]) -> subprocess.CompletedProcess[str]:
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                check=False,
                timeout=settings.subprocess_timeout_seconds,
            )
        except OSError as exc:
            raise MediaProcessingError("yt-dlp is not installed or cannot be started") from exc
        except subprocess.TimeoutExpired as exc:
            raise MediaProcessingError("YouTube operation timed out") from exc
        if result.returncode != 0:
            detail = result.stderr.strip().splitlines()[-1] if result.stderr.strip() else "unknown yt-dlp error"
            raise MediaProcessingError(f"YouTube operation failed: {detail}")
        return result

    def extract_audio(self, media_path: Path, output_dir: Path) -> Path:
        output_dir.mkdir(parents=True, exist_ok=True)
        audio_path = output_dir / "audio.wav"
        command = [settings.ffmpeg_binary, "-y", "-i", str(media_path), "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(audio_path)]
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                check=False,
                timeout=settings.subprocess_timeout_seconds,
            )
        except OSError as exc:
            raise MediaProcessingError("FFmpeg is not installed or cannot be started") from exc
        except subprocess.TimeoutExpired as exc:
            raise MediaProcessingError("Audio extraction timed out") from exc
        if result.returncode != 0 or not audio_path.exists() or audio_path.stat().st_size == 0:
            detail = result.stderr.strip().splitlines()[-1] if result.stderr.strip() else "unknown FFmpeg error"
            raise MediaProcessingError(f"Audio extraction failed: {detail}")
        return audio_path

    def download_youtube(self, url: str, output_dir: Path, video_id: str | None = None) -> Path:
        output_dir.mkdir(parents=True, exist_ok=True)
        basename = video_id or "source"
        self.validate_youtube_url(url)
        self._run_ytdlp([
            sys.executable, "-m", "yt_dlp", "--no-playlist", "--no-warnings",
            "--allowed-extractors", "youtube",
            "--no-call-home", "--max-filesize", str(settings.max_upload_bytes),
            "--format", "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/bestvideo+bestaudio/best",
            "--output", str(output_dir / f"{basename}.%(ext)s"), url,
        ])
        files = [path for path in output_dir.glob(f"{basename}.*") if path.is_file()]
        if not files:
            raise MediaProcessingError("YouTube download produced no media file")
        source = sorted(files)[0]
        self.validate_magic(source)
        self.get_duration(source)
        return source

    @classmethod
    def validate_filename(cls, filename: str | None) -> str:
        suffix = Path(filename or "").suffix.lower()
        if suffix not in cls.supported_extensions:
            raise MediaProcessingError("Unsupported media format")
        return suffix


def temporary_directory() -> tempfile.TemporaryDirectory[str]:
    return tempfile.TemporaryDirectory(prefix="videomind-")