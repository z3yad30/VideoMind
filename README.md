# VideoMind

VideoMind is a local-first MVP for uploading media or processing an authorized YouTube URL, transcribing it, generating a grounded summary, and answering text or voice questions about that specific video.

The backend and Vite frontend are implemented. The current deployment model is a single Windows-hosted process with local filesystem artifacts.

## Architecture

The pipeline is:

1. Upload a video/audio file or submit a YouTube URL.
2. Persist the source under `data/videos/{video_id}{extension}` for both uploads and YouTube downloads.
3. Extract audio with FFmpeg in a temporary working directory.
4. Transcribe audio with configurable `faster-whisper`.
5. Persist timestamped transcript segments.
6. Chunk transcript segments while preserving timestamps.
7. Embed chunks with configurable local Sentence Transformers models.
8. Store chunks in a ChromaDB collection isolated by `video_id`.
9. Generate a structured summary with Groq, using hierarchical summarization for long transcripts.
10. Generate summary and answer audio through a replaceable TTS service.
11. Answer questions using a bounded video-scoped retrieval flow: top 5 candidates, dynamic similarity filtering, and fallback to question-focused raw transcript excerpts when transcript evidence is too weak.

The important behavior change is that YouTube downloads are no longer temporary: the saved source remains in `data/videos/` after processing completes and is served through the backend media route for playback.

The backend uses FastAPI and Uvicorn. Route handlers remain thin; processing, ASR, media, embedding, vector-store, LLM, and TTS responsibilities live in service modules.

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

The application loads environment variables with `python-dotenv`. ASR, upload limits, subprocess timeouts, question length, and storage paths are configurable through the backend settings module.

## Running the Backend

Start the backend with:

```powershell
.\.venv\Scripts\Activate.ps1
uvicorn backend.app.main:app --reload
```

## Running the Frontend

From the project root in PowerShell:

```powershell
Set-Location frontend
npm install
npm run dev
```

The Vite development server runs at `http://localhost:5173` and proxies `/api` requests to the backend at `http://127.0.0.1:8000`. Start the backend separately before using upload, YouTube processing, transcript, summary, or question workflows.

The Python virtual environment installs backend dependencies from `requirements.txt`. Frontend dependencies cannot be installed into that environment because React and Vite are Node/npm packages. Install them once with `npm install` inside `frontend`; validate the production bundle with `npm run build`.

## Runtime Data Reset: Vanish

Use the standalone cleanup utility to remove only VideoMind’s generated runtime data while leaving the repository and source code intact.

### What Vanish clears

`python scripts/vanish.py`

This command targets the project’s local runtime storage under `data/` and removes the generated contents from:

- `data/videos/`
- `data/audio/`
- `data/transcripts/`
- `data/summaries/`
- `data/chroma/`

It preserves the `data/` directory itself and keeps the expected runtime folders in place so the project can regenerate data cleanly.

### What Vanish preserves

Vanish does not delete:

- project source code
- backend or frontend dependencies
- the `.venv` environment
- `.git` metadata
- README and documentation
- `.env` and config files
- any cache outside the runtime data scope
- anything outside the repository’s `data/` directory

### Cache protection

Project caches remain untouched. The script explicitly clears only the allowlisted runtime directories under `data/` and never targets repository caches such as `.pytest_cache`, build output, or other directories outside that boundary.

### Server independence

Vanish is a standalone command-line utility. It does not require the FastAPI server, Uvicorn, frontend, or API routes to be running. It can be invoked directly from the project root with:

```powershell
python scripts/vanish.py
```

## API Plan

The planned REST surface is:

- `POST /videos/upload`
- `POST /videos/youtube`
- `GET /videos/{video_id}`
- `GET /videos/{video_id}/status`
- `GET /videos/{video_id}/events` (Server-Sent Events for live processing activity)
- `GET /videos/{video_id}/media` (serves the persisted source video after processing completes)
- `GET /videos/{video_id}/transcript`
- `GET /videos/{video_id}/summary`
- `POST /videos/{video_id}/question`
- `POST /videos/{video_id}/voice-question`
- `GET /videos/{video_id}/summary/audio`
- `GET /videos/{video_id}/answers/{answer_id}/audio`

Long-running processing runs in the background with stage snapshots and replayable events. Stages include `validating`, `downloading`, `extracting_audio`, `detecting_language`, `transcribing`, `building_transcript`, `chunking`, `embedding`, `indexing`, `summarizing`, `generating_summary_audio`, `completed`, and `failed`. The event stream emits `stage_started`, `stage_progress`, `stage_completed`, `stage_failed`, and `processing_completed`.

## RAG and ChromaDB

Each video receives a generated `video_id` and its own ChromaDB collection named `video_<video_id>`. Every stored chunk includes its text, `video_id`, chunk ID, source, and start/end timestamps. Question retrieval receives the selected video ID and queries only that collection.

The implemented retrieval policy is:

- `top_k = 5`
- `MIN_SIMILARITY = 0.70`
- `RELATIVE_MARGIN = 0.10`
- Keep only chunks with similarity >= `max(MIN_SIMILARITY, top_similarity - RELATIVE_MARGIN)` and similarity >= `MIN_SIMILARITY`

This is a dynamic filter: weak results are discarded before the LLM sees them, but retrieval failure does not stop the question from being processed. If no transcript chunks pass the threshold, the system selects question-focused raw transcript excerpts from `data/transcripts/{video_id}.json`, preserving timestamps and limiting the context to 12,000 characters before asking the LLM whether the question is answerable. The structured summary remains available for display and audio, but is not used as the no-match question context.

The LLM must not invent unsupported facts. If the question is unrelated to the video or the raw transcript has insufficient evidence, the answer states that the available video context does not provide enough information. Voice-question transcriptions are query-only and are never inserted into the video's transcript collection.

## YouTube and Upload Processing

Uploads are validated by size, extension, and MIME type where available. Filenames are sanitized and never used as identifiers. YouTube URLs are validated before `yt-dlp` is invoked.

The actual storage model is persistent: every successfully downloaded YouTube source is saved to `data/videos/{video_id}{extension}` and remains available after the job completes. Uploaded files also use the same source-storage pattern in `data/videos/`; only the extracted WAV/intermediate processing files live in temporary working directories and are cleaned up as part of the processing job. Playback is intentionally blocked until the job reaches `completed`, at which point the frontend loads the saved source from the backend media route instead of showing a broken object URL.

Invalid URLs, unsupported media, missing FFmpeg, failed downloads, corrupted files, empty audio, and service failures still produce logged, user-facing errors without exposing secrets.

## Voice Question Pipeline

The implemented microphone flow is:

```text
record audio -> upload voice question -> ASR -> text question -> video-scoped RAG -> raw transcript fallback if needed -> text answer -> TTS -> audio answer
```

Typed and spoken questions share the same video-scoped retrieval, raw transcript fallback, and per-video conversation memory.

## Testing

The test suite will cover video ID generation, segment-aware chunking, timestamp preservation, collection isolation, retrieval, LLM prompts, request validation, missing environment variables, malformed inputs, and one end-to-end flow with mocked external services. Tests will run without a real Groq API key or network access.

The implemented command is:

```powershell
.\.venv\Scripts\Activate.ps1
python -m pytest
```

The current suite verifies retrieval thresholds, raw transcript fallback, voice flow, history truncation, and per-video isolation.

## Current limitations

- Job status is in memory and `BackgroundTasks` is process-local. A restart loses status, and multiple workers do not share jobs.
- There is no authentication, authorization, tenant isolation, rate limiting, or durable job queue. This is not ready for an internet-facing multi-user deployment.
- ChromaDB, transcripts, summaries, and audio use local filesystem storage. Use a database/object store and isolated vector namespaces for multi-instance deployment.
- ASR, embeddings, Groq, FFmpeg, and Windows SAPI TTS are blocking and resource-intensive. Production deployment needs bounded worker pools, retries, quotas, and retention cleanup.

## Troubleshooting

- **`ffmpeg` not found:** install FFmpeg and verify `ffmpeg -version` in a new terminal.
- **Groq configuration error:** set `GROQ_API_KEY` in the untracked local `.env`; never place it in source code.
- **Model download or memory errors:** choose smaller configurable ASR or embedding models and ensure sufficient disk/RAM.
- **YouTube download failure:** verify the URL is authorized and accessible to `yt-dlp`; network and platform restrictions can prevent retrieval.
- **Windows PowerShell activation failure:** use the project interpreter directly or apply your organization's approved execution-policy setting.

## Current Phase Validation

Current implementation status:

- Background upload and YouTube processing endpoints are active.
- `yt-dlp` downloads now persist to `data/videos/{video_id}{extension}` instead of a temporary processing directory.
- FFmpeg audio extraction still occurs in a temporary working directory to create mono 16 kHz WAV for ASR.
- JSON transcript artifacts preserve segment text, start seconds, and end seconds.
- In-memory processing status tracking and failure reporting remain in place.
- Temporary extracted WAV files are cleaned up without deleting the persisted source artifact.
- Persistent source playback is served by `GET /videos/{video_id}/media` only after processing reaches `completed`.

Completed in Phase 6:

- Added backend-owned processing stage snapshots and replayable SSE events.
- Added truthful chunk-count progress for transcript chunking, embedding, and indexing.
- Added frontend SSE subscription, refresh recovery, activity details, and a responsive processing workspace.

Completed in Phase 3:

- Added segment-aware transcript chunking with preserved start/end timestamps.
- Added local Sentence Transformers embedding support with injectable test doubles.
- Added persistent ChromaDB storage with one `video_<video_id>` collection per video.
- Added video-scoped similarity retrieval and timestamped RAG context construction.
- Added tests for collection isolation, metadata persistence, relevance, and empty collections.

Completed in Phase 4:

- Added lazy Groq client initialization using `GROQ_API_KEY` and `GROQ_MODEL`, defaulting to `openai/gpt-oss-120b`.
- Added automatic transcript indexing and structured hierarchical summaries under `data/summaries/`.
- Added `GET /videos/{video_id}/summary` and grounded `POST /videos/{video_id}/question` with timestamp sources.
- Added mocked LLM tests that run without a real Groq API key or network access.

Completed in Phase 5:

- Added a replaceable TTS abstraction with a local `pyttsx3` adapter using the Windows SAPI voice installed on the host.
- Added WAV audio generation for automatic summaries and grounded answers.
- Added `POST /videos/{video_id}/voice-question` with multipart microphone audio, faster-whisper transcription, existing video-scoped RAG, and a voice answer location.
- Added summary and answer audio download routes.
- Voice question audio and transcripts are temporary/query-only artifacts and are never indexed into ChromaDB.

The initial TTS adapter depends on `pyttsx3` and an installed Windows SAPI voice. It is selected because it runs locally without API credentials or network access. A different provider can be supplied through the `TTSService` protocol; hosts without a usable SAPI voice should provide another adapter rather than changing the RAG pipeline.
