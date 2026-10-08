# VideoMind

VideoMind is a local-first MVP for uploading media or processing an authorized YouTube URL, transcribing it, generating a grounded summary, and answering text or voice questions about that specific video.

The backend and Vite frontend are implemented. The application requires a username/password account to enter the frontend workspace; the backend provides cookie-based sessions and persistent user-owned chats. The current deployment model is a single Windows-hosted process with local filesystem artifacts.

## Architecture

The pipeline is:

1. Upload a video/audio file or submit a YouTube URL.
2. Persist the source under `data/videos/{video_id}{extension}` for both uploads and YouTube downloads, and record its authenticated owner in `data/videos/.owners.json`.
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

The backend uses FastAPI and Uvicorn. Route handlers remain thin; authentication, user and video-owner storage, chat storage, processing, ASR, media, embedding, vector-store, LLM, and TTS responsibilities live in separate modules.

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
data/               Local runtime media, video-owner index, transcript, summary, Chroma, chat, and user storage
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

## Authentication

The backend provides username/password registration, login, session inspection, and logout:

- `POST /auth/register` accepts `{"username": "...", "password": "..."}` and returns the normalized username. Usernames are 1-32 letters, numbers, dots, underscores, or hyphens; they are case-insensitive. Empty credentials and unexpected fields are rejected.
- `POST /auth/login` accepts the same input and sets an HTTP-only, SameSite=Lax session cookie. Invalid usernames and passwords receive the same response.
- `GET /auth/me` returns the authenticated username and responds with `401` when no valid session exists.
- `POST /auth/logout` invalidates the current session and clears its cookie.

Users are stored in `data/users/users.json`. Passwords are hashed with Argon2 through `pwdlib`; plaintext passwords and password hashes are not returned by the API. User-file updates use an atomic temporary-file replacement, and missing user storage is initialized automatically. Authentication does not use a database or frontend localStorage.

The frontend opens on a login screen and offers account registration. Registration validates the username and password confirmation, creates the account through `POST /auth/register`, then signs in through `POST /auth/login`. Existing users can sign in directly. Passwords are sent only to the backend over the configured API connection; the frontend does not save passwords in localStorage. After sign-in, the chatbot workspace loads the authenticated user's saved chats from the backend.

On startup, the frontend calls `GET /auth/me` and only mounts the VideoMind workspace when a valid session is returned. Login sets an HTTP-only, SameSite=Lax session cookie that the browser attaches to subsequent API requests, including after a page refresh. Sessions are random server-side identifiers and expire after 12 hours. Session state is in memory, so restarting the backend invalidates all sessions. A protected API `401` returns the frontend to login, and logout calls `POST /auth/logout` to invalidate the session and clear the cookie.

Chat and video routes require this authenticated session. Uploaded and YouTube-processed videos are assigned to the authenticated account; ownership is stored in `data/videos/.owners.json`. Video status, SSE events, media, transcript, summary, voice answers, and Q&A all check ownership. Chats can only reference videos owned by the current user. Chat answer audio is separately checked against the owning chat and assistant message. Unknown and foreign resource IDs return `404`.

The browser session survives page refreshes through the HTTP-only cookie, but session records are in memory and are invalidated when the backend restarts; the cookie expires after 12 hours. The cookie is configured for local HTTP rather than HTTPS. There is no rate limiting, CSRF token, durable job queue, or multi-worker shared job state. Do not expose this local MVP to the public internet as-is. Video artifacts created before video ownership tracking have no trustworthy owner record and are intentionally inaccessible until they are reprocessed under an account; do not auto-assign legacy artifacts in a multi-account installation.

## Persistent Chat Storage

Chats are stored independently as JSON files under `data/chats/{username}/{chat_id}.json`. The API creates directories automatically and uses atomic replacement when updating a file. A chat record contains its ID and owner, title, creation/update timestamps, optional owned video ID and metadata, summary/transcript fields, and the complete ordered message history. Messages preserve IDs, roles, content, timestamps, and optional assistant sources, evidence timestamps, and answer-audio references.

When an associated video has canonical artifacts, chat detail responses restore summary data from `data/summaries/{video_id}.json` and transcript data from `data/transcripts/{video_id}.json`, after ownership is checked. These potentially large artifacts are referenced through the video ID rather than copied into every chat file unless explicitly supplied in chat content. Files remain scoped to the authenticated username; client-provided usernames are rejected as identity, and unknown or foreign chat/video IDs return `404`. Chat IDs are server-generated 32-character lowercase hexadecimal identifiers and path-like IDs are rejected. Malformed chat JSON fails closed with `503` without returning filesystem paths.

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

`POST /chats/{chat_id}/messages` requires an authenticated owner and a chat associated with a video whose processing job is complete. The endpoint saves the user message first, then calls the same `VideoAIService.answer_question(video_id, question)` pipeline used by standalone video Q&A. Retrieval remains restricted to that video's Chroma collection and keeps the configured similarity thresholds and question-focused raw transcript fallback. A missing video returns `404`; a chat without a video returns `400`, and an unfinished video returns `409`.

The assistant reply is saved with its text, source excerpts and start/end times, a timestamp list, creation time, and an answer-audio reference when TTS succeeds. Answer audio uses the existing `VoiceQuestionService` TTS adapter. If TTS is unavailable, the text answer is still returned and persisted without an audio reference. The response includes both saved messages and the source list.

Every assistant answer has its own sound control. Audio generated with the answer is played only after the user clicks; the browser does not autoplay. The control shows a loading spinner while audio is generated or fetched, changes to a pause state during playback, and reports failures with a retry action. Repeated clicks while a request is pending are ignored, and the backend serializes generation per chat message. If eager synthesis failed or a saved answer has no audio reference, the control retries through `POST /chats/{chat_id}/messages/{message_id}/audio`, which calls the same `VoiceQuestionService.synthesize_answer` implementation. Playback uses `GET /chats/{chat_id}/messages/{message_id}/audio`; this route requires the authenticated chat owner and verifies the assistant message's saved reference. The existing `GET /videos/{video_id}/answers/{answer_id}/audio` route remains available for the voice-question workflow.

Chat message JSON stores only an audio URL/reference, never WAV bytes. New chat-answer references are scoped to the owning chat and message (`/chats/{chat_id}/messages/{message_id}/audio/{answer_id}`); the WAV stays under `data/audio/answers/{video_id}/{answer_id}.wav`. Thus reopening a chat restores the reference, and playback remains owner-checked. The frontend fetches the protected endpoint with the session cookie and keeps only a temporary browser object URL for playback.

In the chat, each assistant reply's **Relevant transcript / Sources** section starts collapsed to keep the conversation compact. Its keyboard-accessible expand button reveals transcript excerpts and start/end timestamps; each timestamp seeks playback to that point. Expand/collapse state is independent for every reply. This changes only source presentation, not retrieval or answer generation.

Persistent chat history is the complete ordered conversation and survives backend reloads. It is separate from LLM context: each chat question sends at most the latest three prior user/assistant messages to Groq, never the full history. Retrieved evidence remains the authority for factual answers. Chat files and authentication user records are runtime user data and are removed when Vanish is run.

## Running the Frontend

From the project root in PowerShell:

```powershell
Set-Location frontend
npm install
npm run dev
```

The Vite development server runs at `http://localhost:5173` and proxies `/api` requests to the backend at `http://127.0.0.1:8000`. Start the backend separately before registering or signing in, and before using upload, YouTube processing, transcript, summary, or question workflows. Create an account from the login screen, or sign in with an existing account; the session survives page refreshes until logout, backend restart, or its 12-hour expiry.

The Python virtual environment installs backend dependencies from `requirements.txt`. Frontend dependencies cannot be installed into that environment because React and Vite are Node/npm packages. Install them once with `npm install` inside `frontend`; validate the production bundle with `npm run build`.

### Frontend Themes

The frontend supports two themes: **Dark Modern** (the default) and **Solarized Light**. Use the theme button in the login header or workspace sidebar to switch themes. The selected theme is stored in browser `localStorage` under `videomind.theme` and is restored on refresh. Both themes use the same application UI and shared CSS design tokens; changing themes does not alter video, transcript, summary, Q&A, processing, or authentication behavior.

### Chat Workspace

The authenticated frontend is a chatbot-style workspace. On desktop, a persistent sidebar sits beside a full-height conversation; on smaller screens, the sidebar opens as a drawer. The sidebar contains **New video**, **New chat**, the current video's **Summary**, **Video**, and **Transcript** views, the authenticated user's saved chat list (title and last-updated date), and account/theme controls including **Log out**.

Selecting a saved chat loads its full chronological message history and video ID through `GET /chats/{chat_id}`. The chat's summary and transcript are restored from that response, while processing status and canonical artifacts are refreshed through the existing video endpoints. Messages are rendered distinctly by role; the question composer stays at the bottom and disables submission while a reply is being generated. Chat history remains backend-owned JSON data and is not mirrored into browser localStorage.

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

Vanish clears video-processing artifacts and authentication, user, and chat runtime data, including registered account records and persistent chat histories. It preserves `data/` and all seven allowlisted directories so the application can regenerate them cleanly.

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

Upload and YouTube ingestion assign the new video ID to the current account before processing begins. Status, replayable SSE progress, media playback, transcript, summary, summary audio, typed/voice questions, and voice-answer audio reject anonymous requests and foreign owners. `GET /videos/{video_id}` is also owner-checked.

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

The test suite covers authentication registration/login/session behavior and password hashing, persistent chat CRUD/list/reload and ownership isolation, as well as video ID generation, segment-aware chunking, timestamp preservation, collection isolation, retrieval, LLM prompts, request validation, malformed inputs, and flows with mocked external services. Tests run without a real Groq API key.

The implemented command is:

```powershell
.\.venv\Scripts\Activate.ps1
python -m pytest
```

The current suite verifies retrieval thresholds, raw transcript fallback, voice flow, history truncation, and per-video isolation.

## Current limitations

- Job status is in memory and `BackgroundTasks` is process-local. A restart loses status, and multiple workers do not share jobs.
- Sessions and processing jobs are process-local. Backend restart invalidates sessions and loses active job status; multiple workers do not share session or job state. There is no rate limiting or CSRF defense, and the cookie is configured for local HTTP. This is not ready for an internet-facing deployment.
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

Uploads and YouTube URLs start background processing and return a generated `video_id`. The original source remains in `data/videos/`; extracted audio uses a temporary working directory. Processing stages and replayable SSE events are process-local. On completion, the frontend loads saved playback, transcript, and summary context and creates a persistent chat. New chat creates another chat for the selected video and does not redownload or reprocess it.

Persistent chat JSON contains the complete conversation. Groq receives only the latest three prior user/assistant messages for chat continuity, plus the selected video's retrieved evidence or bounded raw-transcript fallback. Transcript evidence is video-scoped, threshold-filtered, timestamped, and exposed as collapsed per-answer sources in the UI.

Vanish clears the video directory, including `.owners.json`, along with the other documented allowlisted runtime data. It leaves project files and caches outside that allowlist untouched.
