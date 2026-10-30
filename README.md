# VideoMind

VideoMind is a local-first MVP for uploading media or processing an authorized YouTube URL, transcribing it, generating a grounded summary, and answering text or voice questions about that specific video.

The backend and Vite frontend are implemented. The application requires a username/password account to enter the frontend workspace; the backend provides cookie-based sessions and persistent user-owned chats. User, session, video ownership, job, and chat records are stored in a local SQL database; media and derived artifacts remain on the local filesystem. The current deployment model is one Windows host, with an optional separate worker process.

## Architecture

The pipeline is:

1. Upload a video/audio file or submit a YouTube URL.
2. Hash uploaded files while streaming. Identical uploads share a `media_assets` row and canonical blob, while each account keeps a distinct, owner-checked `video_id`. YouTube downloads remain keyed by their video ID.
3. Extract audio with FFmpeg and retain the normalized intermediate at `data/audio/extracted/{asset_id}.wav` for uploaded assets, or `{video_id}.wav` for YouTube jobs.
4. Transcribe audio with configurable `faster-whisper`.
5. Persist timestamped transcript segments.
6. Chunk transcript segments while preserving timestamps.
7. Embed chunks with configurable local Sentence Transformers models.
8. Store chunks in a ChromaDB collection isolated by `asset_id` (or `video_id` for YouTube jobs).
9. Generate a structured summary with Groq, using hierarchical summarization for long transcripts.
10. Generate summary and answer audio through a replaceable TTS service.
11. Answer questions using a bounded video-scoped retrieval flow: top 5 candidates, dynamic similarity filtering, and fallback to question-focused raw transcript excerpts when transcript evidence is too weak.

YouTube downloads remain in `data/videos/` after processing completes. Uploaded source media and derived transcript, index, summary, and audio artifacts are shared by content-addressed asset, while API responses and ownership checks continue to use each user's video ID.

The backend uses FastAPI and Uvicorn. External service boundaries are defined as protocols in `backend/app/ports/`, with local implementations in `backend/app/adapters/`. `backend/app/container.py` composes those adapters and the injected services; API routes obtain services through FastAPI dependencies. The local blob adapter retains the existing `data/` directory layout.

## Technologies

- Python 3.13 is currently available on the development machine.
- FastAPI and Uvicorn for the API.
- `faster-whisper` for configurable ASR, defaulting to `base`.
- FFmpeg for audio extraction and conversion.
- `yt-dlp` for authorized YouTube retrieval.
- Sentence Transformers for local embeddings, defaulting to `BAAI/bge-m3`.
- ChromaDB for per-video vector collections.
- Groq for summarization and grounded question answering.
- `pyttsx3` as the initial local TTS adapter on Windows.

## Project Layout

```text
backend/app/       API, services, models, schemas, core, utilities, database
frontend/           React/Vite application with login, registration, and video workspace
data/               Local runtime media/assets, transcript, summary, Chroma, chat, and user storage
tests/              Unit and integration tests
.venv/              Project-root Python virtual environment
requirements.txt    Runtime and test dependencies
```

## First-Time Setup on Windows

The backend's Python packages are listed in `requirements.txt`. FFmpeg and Node.js are separate system tools, so they cannot be installed by pip from that file: FFmpeg handles media conversion, and Node.js/npm install and run the frontend. The `av` requirement is constrained to versions compatible with `faster-whisper`.

Use **Command Prompt (cmd.exe)** for the commands below. Install Python 3.13, Node.js LTS, and FFmpeg if they are not already installed. These `winget` commands can install them (Windows may ask you to approve installation):

```cmd
winget install --id Python.Python.3.13 --exact
winget install --id OpenJS.NodeJS.LTS --exact
winget install --id Gyan.FFmpeg.Shared --exact
```

After installation, **close Command Prompt and open a new one** so Windows reloads `PATH`. Check that all three tools are available:

```cmd
py -3.13 --version
node --version
npm --version
ffmpeg -version
```

If `ffmpeg -version` is not recognized, Windows cannot find FFmpeg on `PATH` even if winget reports that it is installed. Run `where.exe ffmpeg`; if that finds nothing, locate `ffmpeg.exe` in the FFmpeg package's installation folder and add the folder containing it to your Windows `Path`, then open a new Command Prompt and retry.

From the repository root, create the Python environment and install backend dependencies:

```cmd
cd /d C:\Users\Royal\Projects\VideoMind
py -3.13 -m venv .venv
.venv\Scripts\activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

If you cloned the repository somewhere else, replace the example path with your checkout's path.

The virtual environment must remain at the project root. Do not install project dependencies globally or inside `backend`.

Install the frontend dependencies from the lockfile:

```cmd
cd frontend
npm ci
```

If you already created `.venv` before the PyAV constraint was added, installing `requirements.txt` again will select a compatible PyAV version. To fix only PyAV in that environment, run `python -m pip install "av>=11,<17"` with the environment activated.

ASR and embedding models may download the first time you process a video. Set `GROQ_API_KEY` in your local, untracked `.env` for summarization and question answering.

## Environment Variables

Copy `.env.example` to `.env` only when setting up a new checkout. The checked-in local `.env` contains no real credential. Set the real key only in your local untracked `.env`:

```dotenv
GROQ_API_KEY=your-real-key
GROQ_MODEL=openai/gpt-oss-120b
```

Never hardcode or commit API keys. `.env` is ignored by Git. Tests must use mocked external services and must not require a Groq key.

The application loads environment variables with `python-dotenv`. `DATABASE_URL`, `SQLITE_BUSY_TIMEOUT_MS`, `WORKER_MODE`, and `MAX_CONCURRENT_JOBS` configure persistence and worker behavior. `WORKER_MODE` is `embedded` by default; `external` disables the API's worker thread. `MAX_CONCURRENT_JOBS` defaults to `1`. `COOKIE_SECURE` defaults to false for local development and true when `APP_ENV=production`; SameSite remains Lax. `ALLOWED_ORIGINS` is a comma-separated origin allowlist and defaults to the Vite origins on port 5173. Unsafe HTTP methods require a matching Origin or Referer.

Authentication and ingestion limits can be configured with `LOGIN_RATE_LIMIT`, `REGISTER_RATE_LIMIT`, `INGESTION_RATE_LIMIT`, and `QUESTION_RATE_LIMIT` (defaults: 5/minute, 5/minute, 10/hour, and 30/minute). Upload duration and per-user quotas use `MAX_MEDIA_DURATION_SECONDS`, `MAX_VIDEOS_PER_USER`, and `MAX_TOTAL_MINUTES_PER_USER`. `FFPROBE_BINARY` and `SUBPROCESS_TIMEOUT_SECONDS` configure media probing and command timeouts. ASR, upload byte limits, question length, and storage paths are also configurable through backend settings.

## Running the Backend

Open a Command Prompt at the repository root and start the backend:

```cmd
cd /d C:\Users\Royal\Projects\VideoMind
.venv\Scripts\activate
uvicorn backend.app.main:app --reload
```

With the default `WORKER_MODE=embedded`, the API starts its durable-job worker in a background thread. To run it in a separate terminal instead, set `WORKER_MODE=external` in `.env`, start the API as usual, then run:

```cmd
cd /d C:\Users\Royal\Projects\VideoMind
.venv\Scripts\activate
python -m backend.worker
```

The worker claims jobs from the SQL database, sends heartbeats, and resumes stale jobs from their last completed stage. `MAX_CONCURRENT_JOBS` controls the number of simultaneous jobs per worker process.

## Authentication

The backend provides username/password registration, login, session inspection, and logout:

- `POST /auth/register` accepts `{"username": "...", "password": "..."}` and returns the normalized username. Usernames are 1-32 letters, numbers, dots, underscores, or hyphens; they are case-insensitive. Empty credentials and unexpected fields are rejected.
- `POST /auth/login` accepts the same input and sets an HTTP-only, SameSite=Lax session cookie. Invalid usernames and passwords receive the same response.
- `GET /auth/me` returns the authenticated username and responds with `401` when no valid session exists.
- `POST /auth/logout` invalidates the current session and clears its cookie.

Users and hashed session identifiers are stored in the SQL database. Passwords are hashed with Argon2 through `pwdlib`; plaintext passwords and password hashes are not returned by the API. The database defaults to `sqlite:///data/videomind.db`; SQLite uses WAL, foreign keys, and a configurable busy timeout. Set `DATABASE_URL` in `.env` to select another SQLAlchemy-supported database URL. User records, sessions, video ownership, media-asset deduplication, and chats are database-backed, while transcript, summary, audio, media, and Chroma data retain their local filesystem layout.

The frontend opens on a login screen and offers account registration. Registration validates the username and password confirmation, creates the account through `POST /auth/register`, then signs in through `POST /auth/login`. Existing users can sign in directly. Passwords are sent only to the backend over the configured API connection; the frontend does not save passwords in localStorage. After sign-in, the chatbot workspace loads the authenticated user's saved chats from the backend.

On startup, the frontend calls `GET /auth/me` and only mounts the VideoMind workspace when a valid session is returned. Login sets an HTTP-only, SameSite=Lax session cookie that the browser attaches to subsequent API requests, including after a page refresh. Sessions are random server-side identifiers and expire after 12 hours; the database stores only a hash of each session identifier. A protected API `401` returns the frontend to login, and logout calls `POST /auth/logout` to invalidate the session and clear the cookie.

Chat and video routes require this authenticated session. Uploaded and YouTube-processed videos are assigned to the authenticated account in the database. Identical uploads share one asset, but video status, SSE events, media, transcript, summary, voice answers, and Q&A continue to check ownership per video row. Chats can only reference videos owned by the current user. Deleting a video keeps its chats and marks them `video_missing`; shared media is removed only after its last video reference is deleted. `DELETE /me/data` removes the current user's chats, sessions, and video rows while preserving assets still referenced by another user. Chat answer audio is separately checked against the owning chat and assistant message. Unknown and foreign resource IDs return `404`.

The browser session survives page refreshes and backend restarts through the HTTP-only cookie and persistent session records; the cookie expires after 12 hours. `COOKIE_SECURE` controls HTTPS-only cookies, and unsafe methods are restricted to `ALLOWED_ORIGINS`. Configurable in-memory rate limits protect authentication, ingestion, and question routes. This remains a single-host local-first MVP; perform a deployment review before exposing it publicly. Video artifacts without an ownership record are intentionally inaccessible until assigned through a trusted migration or reprocessed under an account; do not auto-assign legacy artifacts in a multi-account installation.

## Database Setup and Legacy Import

The database schema is managed by Alembic. After installing the Python requirements, initialize or upgrade it from the repository root:

```cmd
python -m alembic upgrade head
```

For an existing installation with JSON users, chats, or `data/videos/.owners.json`, first run the migration in validation-only mode, then import:

```cmd
python -m scripts.migrate_json_to_db --dry-run
python -m scripts.migrate_json_to_db
```

The importer is idempotent and leaves source JSON files unchanged. Keep a backup of runtime data before importing. New installations can skip the importer. `DATABASE_URL` defaults to `sqlite:///data/videomind.db`; `SQLITE_BUSY_TIMEOUT_MS` controls SQLite's busy timeout.

After importing legacy owners and upgrading the schema, link existing local video files to shared assets. Validate first, then run the migration:

```cmd
python -m scripts.migrate_videos_to_assets --dry-run
python -m scripts.migrate_videos_to_assets
```

The asset migration hashes source files, links duplicate videos to one canonical asset, and removes duplicate video-keyed artifacts and vector collections. Videos with no local source file are reported as skipped. Back up `data/` before running it.

## Persistent Chat Storage

Chats, messages, and message sources are stored in the SQL database. A chat record contains its ID and owner, title, creation/update timestamps, optional owned video ID and metadata, summary/transcript fields, and the complete ordered message history. Message/source rows cascade when a chat is deleted.

When an associated video has canonical artifacts, chat detail responses restore summary data from `data/summaries/{asset_id}.json` and transcript data from `data/transcripts/{asset_id}.json` for uploaded assets, or the video ID for YouTube jobs, after ownership is checked. These potentially large artifacts are referenced rather than copied into every chat unless explicitly supplied in chat content. Client-provided usernames are rejected as identity, and unknown or foreign chat/video IDs return `404`. Chat IDs are server-generated 32-character lowercase hexadecimal identifiers and path-like IDs are rejected. Removing a video detaches and marks its chats `video_missing` instead of deleting them.

Chat API (all endpoints require the HTTP-only authenticated session cookie):

- `GET /chats` lists the current user's chats, newest activity first.
- `POST /chats` creates a chat and returns its generated `chat_id`.
- `POST /chats/{chat_id}/messages` accepts `{"question": "Explain this concept"}` and returns the saved `user_message`, generated `assistant_message`, its `sources`, and optional `answer_audio_location`.
- `POST /chats/{chat_id}/messages/{message_id}/audio` idempotently generates missing answer audio using the existing voice service and persists the scoped reference.
- `GET /chats/{chat_id}/messages/{message_id}/audio` streams the saved WAV only to the authenticated owner of that chat message.
- `GET /chats/{chat_id}` returns one owned chat and resolves available video context.
- `PUT /chats/{chat_id}` updates chat metadata/content, including the complete message list when supplied.
- `DELETE /chats/{chat_id}` deletes an owned chat and returns `204`.

### Chat Question Flow

`POST /chats/{chat_id}/messages` requires an authenticated owner and a chat associated with a video whose processing job is complete. The endpoint saves the user message first, then calls the same `VideoAIService.answer_question(asset_id, question)` pipeline used by standalone video Q&A. Retrieval remains restricted to the shared asset's Chroma collection and keeps the configured similarity thresholds and question-focused raw transcript fallback. A missing video returns `404`; a chat without a video returns `400`, and an unfinished video returns `409`.

The assistant reply is saved with its text, source excerpts and start/end times, a timestamp list, creation time, and an answer-audio reference when TTS succeeds. Answer audio uses the existing `VoiceQuestionService` TTS adapter. If TTS is unavailable, the text answer is still returned and persisted without an audio reference. The response includes both saved messages and the source list.

Every assistant answer has its own sound control. Audio generated with the answer is played only after the user clicks; the browser does not autoplay. The control shows a loading spinner while audio is generated or fetched, changes to a pause state during playback, and reports failures with a retry action. Repeated clicks while a request is pending are ignored, and the backend serializes generation per chat message. If eager synthesis failed or a saved answer has no audio reference, the control retries through `POST /chats/{chat_id}/messages/{message_id}/audio`, which calls the same `VoiceQuestionService.synthesize_answer` implementation. Playback uses `GET /chats/{chat_id}/messages/{message_id}/audio`; this route requires the authenticated chat owner and verifies the assistant message's saved reference. The existing `GET /videos/{video_id}/answers/{answer_id}/audio` route remains available for the voice-question workflow.

Chat message JSON stores only an audio URL/reference, never WAV bytes. New chat-answer references are scoped to the owning chat and message (`/chats/{chat_id}/messages/{message_id}/audio/{answer_id}`); the WAV stays under `data/audio/answers/{asset_id}/{answer_id}.wav` for uploaded assets, or the video ID for YouTube jobs. Thus reopening a chat restores the reference, and playback remains owner-checked. The frontend fetches the protected endpoint with the session cookie and keeps only a temporary browser object URL for playback.

In the chat, each assistant reply's **Relevant transcript / Sources** section starts collapsed to keep the conversation compact. Its keyboard-accessible expand button reveals transcript excerpts and start/end timestamps; each timestamp seeks playback to that point. Expand/collapse state is independent for every reply. This changes only source presentation, not retrieval or answer generation.

Persistent chat history is the complete ordered conversation and survives backend reloads. It is separate from LLM context: each chat question sends at most the latest three prior user/assistant messages to Groq, never the full history. Retrieved evidence remains the authority for factual answers. Chat files and authentication user records are runtime user data and are removed when Vanish is run.

## Running the Frontend

Open a **second Command Prompt** and start the frontend:

```cmd
cd /d C:\Users\Royal\Projects\VideoMind\frontend
npm run dev
```

The Vite development server runs at `http://localhost:5173` and proxies `/api` requests to the backend at `http://127.0.0.1:8000`. Start the backend separately before registering or signing in, and before using upload, YouTube processing, transcript, summary, or question workflows. Create an account from the login screen, or sign in with an existing account; the session survives page refreshes until logout, backend restart, or its 12-hour expiry.

The Python virtual environment installs backend dependencies from `requirements.txt`. Frontend dependencies cannot be installed into that environment because React and Vite are Node/npm packages. Install them with `npm ci` inside `frontend`; validate the production bundle with `npm run build`.

### Frontend Themes

The frontend supports two themes: **Dark Modern** (the default) and **Solarized Light**. Use the theme button in the login header or workspace sidebar to switch themes. The selected theme is stored in browser `localStorage` under `videomind.theme` and is restored on refresh. Both themes use the same application UI and shared CSS design tokens; changing themes does not alter video, transcript, summary, Q&A, processing, or authentication behavior.

### Chat Workspace

The authenticated frontend is a chatbot-style workspace. On desktop, a persistent sidebar sits beside a full-height conversation; on smaller screens, the sidebar opens as a drawer. The sidebar contains **New video**, **New chat**, the current video's **Summary**, **Video**, and **Transcript** views, the authenticated user's saved chat list (title and last-updated date), and account/theme controls including **Log out**.

Selecting a saved chat loads its full chronological message history and video ID through `GET /chats/{chat_id}`. The chat's summary and transcript are restored from that response, while processing status and canonical artifacts are refreshed through the existing video endpoints. Messages are rendered distinctly by role; the question composer stays at the bottom and disables submission while a reply is being generated. Chat history remains backend-owned SQL data and is not mirrored into browser localStorage.

**New video** opens the existing upload or YouTube ingestion workflow. When processing completes, the frontend creates and opens a chat associated with the resulting video ID. **New chat** creates an empty chat linked to the currently selected, already-processed video; it does not upload or download that video again. The Summary, Video, and Transcript sidebar views use the context restored for the selected chat.

### Current chat/video safeguards and controls

The workspace validates the selected chat against the active video context before the composer allows a new answer. If the chat belongs to a different video, the UI shows a mismatch warning, disables the question composer, and exposes a direct action to load the selected chat's own video context before continuing. This is a frontend guardrail; the backend still enforces ownership and rejects non-matching video/chat combinations.

Each chat row also includes a delete control. Clicking it opens a confirmation dialog and then calls the authenticated `DELETE /chats/{chat_id}` route to remove the chat and its persisted messages. The selected chat is cleared from the UI immediately after deletion, and the sidebar refreshes.

The summary panel includes an explicit **Play Summary** control. It calls the authenticated summary-audio route, creates a temporary browser audio object, and enables a pause/retry flow without auto-playing on load. This remains scoped to the current video's canonical summary output and respects the existing owner checks.

Frontend responsibilities are split across `frontend/src/App.tsx` for authentication, `frontend/src/VideoChatWorkspace.tsx` for workspace state and API orchestration, `frontend/src/WorkspaceComponents.tsx` for the sidebar, chat messages/composer, ingestion and context panels, `frontend/src/api.ts` for typed HTTP calls, and `frontend/src/styles.css` for responsive layout and themes.

## Runtime Data Reset: Vanish

Use the standalone cleanup utility to remove all local VideoMind application runtime and user data while leaving the repository and source code intact.

### What Vanish clears

`python scripts/vanish.py`

This standalone command targets only these explicitly allowlisted runtime directories under `data/`:

- `data/videos/`
- `data/audio/`
- `data/transcripts/`
- `data/summaries/`
- `data/chroma/`
- `data/users/`
- `data/chats/`

Vanish clears video-processing artifacts and authentication, user, and chat runtime data, including registered account records and persistent chat histories. It removes `data/videomind.db*` (including SQLite sidecar files) and recreates an empty schema, while preserving `data/` and all seven allowlisted directories.

### What Vanish preserves

Vanish does not delete:

- project source code
- backend or frontend dependencies
- the `.venv` environment
- `.git` metadata
- README and documentation
- `.env` and config files
- project caches outside the explicit runtime data allowlist
- anything outside the repository’s `data/` directory

### Allowlist and Cache Protection

Project caches remain untouched. The script explicitly clears only the seven listed runtime directories; it does not use a broad `data/*` deletion and does not target repository caches such as `.pytest_cache`, build output, or any project cache outside the runtime data allowlist.

### Server independence

Vanish is a standalone command-line utility. It does not require the FastAPI server, Uvicorn, frontend, or API routes to be running. It can be invoked directly from the project root with:

```powershell
python scripts/vanish.py
```

## API Surface

Authentication endpoints:

- `POST /auth/register`
- `POST /auth/login`
- `GET /auth/me`
- `POST /auth/logout`

Chat endpoints (authenticated):

- `GET /chats`
- `POST /chats`
- `POST /chats/{chat_id}/messages`
- `GET /chats/{chat_id}`
- `PUT /chats/{chat_id}`
- `DELETE /chats/{chat_id}`

Video endpoints:

Implemented video endpoints (all require an authenticated owner):

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
- `DELETE /videos/{video_id}` (removes this user's video; shared asset cleanup waits for the last reference)
- `DELETE /me/data` (removes this user's chats, sessions, and video rows)

Upload and YouTube ingestion assign the new video ID to the current account before processing begins. Status, replayable SSE progress, media playback, transcript, summary, summary audio, typed/voice questions, and voice-answer audio reject anonymous requests and foreign owners. `GET /videos/{video_id}` is also owner-checked.

Long-running processing runs in the background with stage snapshots and replayable events. Stages include `validating`, `downloading`, `extracting_audio`, `detecting_language`, `transcribing`, `building_transcript`, `chunking`, `embedding`, `indexing`, `summarizing`, `generating_summary_audio`, `completed`, and `failed`. The event stream emits `stage_started`, `stage_progress`, `stage_completed`, `stage_failed`, and `processing_completed`.

## RAG and ChromaDB

Each video receives a generated `video_id`. Uploaded videos with identical SHA-256 content share a canonical `asset_id` and ChromaDB collection named `video_<asset_id>`; YouTube jobs continue to use their `video_id` as the artifact key. Every stored chunk includes its text, artifact ID, chunk ID, source, and start/end timestamps. Question retrieval resolves the owned video to its asset before querying, so one user's upload cannot grant another user access to the video row.

The implemented retrieval policy is:

- `top_k = 5`
- `MIN_SIMILARITY = 0.70`
- `RELATIVE_MARGIN = 0.10`
- Keep only chunks with similarity >= `max(MIN_SIMILARITY, top_similarity - RELATIVE_MARGIN)` and similarity >= `MIN_SIMILARITY`

This is a dynamic filter: weak results are discarded before the LLM sees them, but retrieval failure does not stop the question from being processed. If no transcript chunks pass the threshold, the system selects question-focused raw transcript excerpts from the resolved asset's `data/transcripts/{asset_id}.json` (or the YouTube video ID), preserving timestamps and limiting the context to 12,000 characters before asking the LLM whether the question is answerable. The structured summary remains available for display and audio, but is not used as the no-match question context.

The LLM must not invent unsupported facts. If the question is unrelated to the video or the raw transcript has insufficient evidence, the answer states that the available video context does not provide enough information. Voice-question transcriptions are query-only and are never inserted into the video's transcript collection.

## YouTube and Upload Processing

Uploads are validated by size, extension, magic bytes, and probed duration before they are queued. Filenames are sanitized and never used as identifiers. YouTube URLs are limited to YouTube hosts, playlists are disabled, and yt-dlp runs with a configured timeout. Per-user video-count and total-duration quotas apply before jobs are enqueued.

The actual storage model is persistent: every successfully downloaded YouTube source is saved to `data/videos/{video_id}{extension}` and remains available after the job completes. Upload sources are stored once under `data/videos/assets/{asset_id}{extension}`. Extracted audio, transcript/chunk/embedding JSON, summaries, summary audio, answer audio, and Chroma collections use the asset ID for uploaded media and the video ID for YouTube jobs, so processing a repeated upload reuses its existing artifacts. Playback is intentionally blocked until the job reaches `completed`, at which point the frontend loads the saved source through the owner-checked media route.

Invalid URLs, unsupported media, missing FFmpeg, failed downloads, corrupted files, empty audio, and service failures still produce logged, user-facing errors without exposing secrets.

## Voice Question Pipeline

The implemented microphone flow is:

```text
record audio -> upload voice question -> ASR -> text question -> video-scoped RAG -> raw transcript fallback if needed -> text answer -> TTS -> audio answer
```

Typed and spoken questions share the same video-scoped retrieval, raw transcript fallback, and per-video conversation memory.

## Testing

The test suite covers authentication registration/login/session behavior and password hashing, persistent chat CRUD/list/reload and ownership isolation, as well as video ID generation, segment-aware chunking, timestamp preservation, collection isolation, retrieval, LLM prompts, request validation, malformed inputs, and flows with mocked external services. Tests run without a real Groq API key.

The implemented command is:

```powershell
.\.venv\Scripts\Activate.ps1
python -m pytest
```

The current suite verifies retrieval thresholds, raw transcript fallback, voice flow, history truncation, and per-video isolation.

## Current limitations

- Job state and event history are persisted in SQL. Workers share the queue using atomic claims and recover stale heartbeats; media/artifact files must remain available on the configured local filesystem.
- Sessions are persisted but expire after 12 hours. Rate limiting is in-memory and therefore local to one process; review the operational model before running multiple API processes or exposing the service publicly.
- Video owner metadata is local JSON beside the source media. Older video artifacts without an owner entry cannot be safely attributed automatically and must be reprocessed under an account.
- ChromaDB, transcripts, summaries, and audio use local filesystem storage. Use a database/object store and isolated vector namespaces for multi-instance deployment.
- ASR, embeddings, Groq, FFmpeg, and Windows SAPI TTS are blocking and resource-intensive. Production deployment needs bounded worker pools, retries, quotas, and retention cleanup.

## Troubleshooting

- **`ffmpeg` not found:** install FFmpeg and verify `ffmpeg -version` in a new terminal.
- **Groq configuration error:** set `GROQ_API_KEY` in the untracked local `.env`; never place it in source code.
- **Model download or memory errors:** choose smaller configurable ASR or embedding models and ensure sufficient disk/RAM.
- **YouTube download failure:** verify the URL is authorized and accessible to `yt-dlp`; network and platform restrictions can prevent retrieval.
- **Windows PowerShell activation failure:** use the project interpreter directly or apply your organization's approved execution-policy setting.

## Current Runtime Behavior

Uploads and YouTube URLs enqueue a durable job and return a generated `video_id`. The original source remains in `data/videos/`; normalized audio, transcript/chunk/embedding checkpoints, summaries, and audio remain under `data/`. The worker checkpoints completed stages and uses database-backed events for status and SSE replay. `Last-Event-ID` reconnects replay only events not yet received. On completion, the frontend loads saved playback, transcript, and summary context and creates a persistent chat. New chat creates another chat for the selected video and does not redownload or reprocess it.

Persistent chat records contain the complete conversation. Groq receives only the latest three prior user/assistant messages for chat continuity, plus the selected video's retrieved evidence or bounded raw-transcript fallback. Transcript evidence is video-scoped, threshold-filtered, timestamped, and exposed as collapsed per-answer sources in the UI.

Vanish clears the documented allowlisted runtime data, removes `data/videomind.db*`, and recreates an empty database schema. It leaves project files and caches outside that allowlist untouched.
