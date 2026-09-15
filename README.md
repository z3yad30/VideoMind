# AI Video Assistant

AI Video Assistant is a planned production-quality MVP for uploading media or processing an authorized YouTube URL, transcribing it, generating a grounded summary, and answering text or voice questions about that specific video.

This repository is being built incrementally. Phase 2 implements backend media ingestion and timestamped ASR. RAG, TTS, summaries, and the frontend remain out of scope.

## Architecture

The planned pipeline is:

1. Upload a video/audio file or submit a YouTube URL.
2. Download media when needed and extract audio with FFmpeg.
3. Transcribe audio with configurable `faster-whisper`.
4. Persist timestamped transcript segments.
5. Chunk transcript segments while preserving timestamps.
6. Embed chunks with configurable local Sentence Transformers models.
7. Store chunks in a ChromaDB collection isolated by `video_id`.
8. Generate a structured summary with Groq, using hierarchical summarization for long transcripts.
9. Generate summary and answer audio through a replaceable TTS service.
10. Answer questions by retrieving only from the selected video's collection, then passing retrieved context to Groq.

The backend will use FastAPI and Uvicorn. Route handlers will remain thin; processing, ASR, media, embedding, vector-store, LLM, and TTS responsibilities will live in service modules.

## Technologies

- Python 3.13 is currently available on the development machine.
- FastAPI and Uvicorn for the API.
- `faster-whisper` for configurable ASR, defaulting to `large-v3`.
- FFmpeg for audio extraction and conversion.
- `yt-dlp` for authorized YouTube retrieval.
- Sentence Transformers for local embeddings, defaulting to `BAAI/bge-m3`.
- ChromaDB for per-video vector collections.
- Groq for summarization and grounded question answering.
- `pyttsx3` as the initial local TTS adapter on Windows.

## Project Layout

```text
backend/app/       API, services, models, schemas, core, utilities, database
frontend/           Frontend application (next implementation phase)
data/               Local runtime media, transcript, summary, and Chroma storage
tests/              Unit and integration tests
.venv/              Project-root Python virtual environment
requirements.txt    Runtime and test dependencies
```

## Installation on Windows

From the project root in PowerShell:

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

The virtual environment must remain at the project root. Do not install project dependencies into the global Python installation or inside `backend`.

If PowerShell blocks activation for the current user, open PowerShell as the current user and run the appropriate policy command allowed by your organization, or invoke the interpreter directly:

```powershell
.\.venv\Scripts\python.exe --version
```

## FFmpeg

FFmpeg is required for media processing and is not currently available on this machine's `PATH`. One Windows installation option is:

```powershell
winget install Gyan.FFmpeg.Shared
ffmpeg -version
```

Restart the terminal after installation if `ffmpeg` is not immediately found. The application will report a useful configuration error when FFmpeg is unavailable.

## Environment Variables

Copy `.env.example` to `.env` only when setting up a new checkout. The checked-in local `.env` contains no real credential. Set the real key only in your local untracked `.env`:

```dotenv
GROQ_API_KEY=your-real-key
GROQ_MODEL=openai/gpt-oss-120b
```

Never hardcode or commit API keys. `.env` is ignored by Git. Tests must use mocked external services and must not require a Groq key.

The application will load environment variables with `python-dotenv`. ASR, embedding, chunking, retrieval count, storage paths, upload limits, and TTS settings will be configurable through the backend settings module as implementation proceeds.

## Running the Backend

Start the backend with:

```powershell
.\.venv\Scripts\Activate.ps1
uvicorn backend.app.main:app --reload
```

## Running the Frontend

The frontend will be added in a later phase. Its setup and development command will be documented here when its package manifest exists.

## API Plan

The planned REST surface is:

- `POST /videos/upload`
- `POST /videos/youtube`
- `GET /videos/{video_id}`
- `GET /videos/{video_id}/status`
- `GET /videos/{video_id}/transcript`
- `GET /videos/{video_id}/summary`
- `POST /videos/{video_id}/question`
- `POST /videos/{video_id}/voice-question`
- `GET /videos/{video_id}/summary/audio`
- `GET /videos/{video_id}/answers/{answer_id}/audio`

Long-running processing will run in the background, with explicit statuses including `uploaded`, `downloading`, `extracting_audio`, `transcribing`, `chunking`, `embedding`, `storing_vectors`, `generating_summary`, `generating_voice_summary`, `completed`, and `failed`.

## RAG and ChromaDB

Each video receives a generated `video_id` and its own ChromaDB collection named `video_<video_id>`. Every stored chunk includes its text, `video_id`, chunk ID, source, and start/end timestamps. Question retrieval receives the selected video ID and queries only that collection. Retrieved chunks are passed to a grounded Groq prompt that says not to invent unsupported information and to state when the answer is not present in the video context.

Responses will also return retrieved timestamp sources so the frontend can later implement jump-to-timestamp behavior. Voice-question transcriptions are query-only and are never inserted into the video's transcript collection.

## YouTube and Upload Processing

Uploads will be validated by size, extension, and MIME type where available. Filenames will be sanitized and never used as identifiers. YouTube URLs will be validated before `yt-dlp` is invoked. Temporary downloads and extracted audio will be cleaned up after processing unless retained as an explicit application artifact. Invalid URLs, unsupported media, missing FFmpeg, failed downloads, corrupted files, empty audio, and service failures will produce logged, user-facing errors without exposing secrets.

## Voice Question Pipeline

The planned microphone flow is:

```text
record audio -> upload voice question -> ASR -> text question -> video-scoped RAG -> text answer -> TTS -> audio answer
```

The same RAG service will handle typed and transcribed questions.

## Testing

The test suite will cover video ID generation, segment-aware chunking, timestamp preservation, collection isolation, retrieval, LLM prompts, request validation, missing environment variables, malformed inputs, and one end-to-end flow with mocked external services. Tests will run without a real Groq API key or network access.

The intended command is:

```powershell
.\.venv\Scripts\Activate.ps1
python -m pytest
```

## Troubleshooting

- **`ffmpeg` not found:** install FFmpeg and verify `ffmpeg -version` in a new terminal.
- **Groq configuration error:** set `GROQ_API_KEY` in the untracked local `.env`; never place it in source code.
- **Model download or memory errors:** choose smaller configurable ASR or embedding models and ensure sufficient disk/RAM.
- **YouTube download failure:** verify the URL is authorized and accessible to `yt-dlp`; network and platform restrictions can prevent retrieval.
- **Windows PowerShell activation failure:** use the project interpreter directly or apply your organization's approved execution-policy setting.

## Current Phase Validation

Completed in Phase 2:

- Added background upload and YouTube processing endpoints.
- Added FFmpeg audio extraction to mono 16 kHz WAV.
- Added a replaceable ASR service abstraction with a `faster-whisper` implementation.
- Added JSON transcript artifacts preserving segment text, start seconds, and end seconds.
- Added in-memory processing status tracking and failure reporting.
- Added temporary media cleanup and deterministic tests for the processing pipeline.

RAG, TTS, and LLM summaries are intentionally deferred to later phases.
