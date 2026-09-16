import subprocess
import tempfile
from pathlib import Path

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
        except Exception:
            destination.unlink(missing_ok=True)
            raise

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

    def download_youtube(self, url: str, output_dir: Path) -> Path:
        output_dir.mkdir(parents=True, exist_ok=True)
        try:
            import yt_dlp
        except ImportError as exc:
            raise MediaProcessingError("yt-dlp is not installed") from exc
        options = {
            "format": "bestvideo*+bestaudio/best",
            "outtmpl": str(output_dir / "source.%(ext)s"),
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
            "socket_timeout": settings.subprocess_timeout_seconds,
            "max_filesize": settings.max_upload_bytes,
        }
        try:
            with yt_dlp.YoutubeDL(options) as downloader:
                downloader.download([url])
        except Exception as exc:
            raise MediaProcessingError(f"YouTube download failed: {exc}") from exc
        files = [path for path in output_dir.glob("source.*") if path.is_file()]
        if not files:
            raise MediaProcessingError("YouTube download produced no media file")
        return files[0]

    @classmethod
    def validate_filename(cls, filename: str | None) -> str:
        suffix = Path(filename or "").suffix.lower()
        if suffix not in cls.supported_extensions:
            raise MediaProcessingError("Unsupported media format")
        return suffix


def temporary_directory() -> tempfile.TemporaryDirectory[str]:
    return tempfile.TemporaryDirectory(prefix="videomind-")