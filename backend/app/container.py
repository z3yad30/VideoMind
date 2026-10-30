from dataclasses import dataclass
from functools import lru_cache

from backend.app.adapters import (
    ChromaVectorStore,
    FasterWhisperTranscriber,
    GroqLLMClient,
    SqlJobQueue,
    LocalBlobStore,
    Pyttsx3TTS,
    SentenceTransformerEmbedder,
)
from backend.app.core.config import settings
from backend.app.db.database import get_database
from backend.app.services.llm import GroqLLMService, VideoAIService
from backend.app.services.media import MediaService
from backend.app.services.rag import TranscriptRAGService
from backend.app.services.video_processing import VideoProcessingService
from backend.app.services.voice import VoiceQuestionService


@dataclass(frozen=True)
class ApplicationContainer:
    job_queue: SqlJobQueue
    blob_store: LocalBlobStore
    transcriber: FasterWhisperTranscriber
    embedder: SentenceTransformerEmbedder
    vector_store: ChromaVectorStore
    llm_client: GroqLLMClient
    tts: Pyttsx3TTS
    media: MediaService
    rag: TranscriptRAGService
    ai: VideoAIService
    video_processing: VideoProcessingService
    voice: VoiceQuestionService


@lru_cache(maxsize=1)
def get_container() -> ApplicationContainer:
    job_queue = SqlJobQueue(get_database().session_factory)
    blob_store = LocalBlobStore(settings.project_root / "data")
    transcriber = FasterWhisperTranscriber()
    embedder = SentenceTransformerEmbedder()
    vector_store = ChromaVectorStore(settings.project_root / "data" / "chroma")
    llm_client = GroqLLMClient()
    tts = Pyttsx3TTS()
    media = MediaService()
    rag = TranscriptRAGService(blob_store, embedder, vector_store)
    ai = VideoAIService(GroqLLMService(llm_client), rag, blob_store, tts)
    video_processing = VideoProcessingService(media, transcriber, ai, blob_store, job_queue)
    voice = VoiceQuestionService(ai, media, transcriber, tts, blob_store)
    return ApplicationContainer(
        job_queue=job_queue,
        blob_store=blob_store,
        transcriber=transcriber,
        embedder=embedder,
        vector_store=vector_store,
        llm_client=llm_client,
        tts=tts,
        media=media,
        rag=rag,
        ai=ai,
        video_processing=video_processing,
        voice=voice,
    )


def get_blob_store() -> LocalBlobStore:
    return get_container().blob_store


def get_ai_service() -> VideoAIService:
    return get_container().ai


def get_video_processing_service() -> VideoProcessingService:
    return get_container().video_processing


def get_voice_service() -> VoiceQuestionService:
    return get_container().voice


def get_media_service() -> MediaService:
    return get_container().media
