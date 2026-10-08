const API_BASE = import.meta.env.VITE_API_BASE_URL || "/api";

export type AuthUser = { username: string };

export class ApiError extends Error {
  constructor(message: string, readonly status: number) {
    super(message);
    this.name = "ApiError";
  }
}

let onSessionExpired: (() => void) | null = null;
const pendingGetRequests = new Map<string, Promise<unknown>>();

export function setSessionExpiredHandler(handler: (() => void) | null) {
  onSessionExpired = handler;
}

export type ProcessingStatus =
  | "queued"
  | "validating"
  | "downloading"
  | "extracting_audio"
  | "detecting_language"
  | "transcribing"
  | "building_transcript"
  | "chunking"
  | "embedding"
  | "indexing"
  | "summarizing"
  | "generating_summary_audio"
  | "completed"
  | "failed";

export type TranscriptSegment = { text: string; start: number; end: number };
export type QuestionSource = TranscriptSegment;

export type VideoJob = {
  video_id: string;
  status: ProcessingStatus;
};

export type VideoStatus = VideoJob & { error?: string | null; updated_at: string; stages: ProcessingStage[] };
export type StageStatus = "pending" | "running" | "completed" | "failed" | "skipped";
export type ProcessingStage = {
  id: ProcessingStatus;
  display_name: string;
  status: StageStatus;
  progress: number | null;
  message: string | null;
  detail: string | null;
  started_at: string | null;
  completed_at: string | null;
  error: string | null;
};
export type ProcessingEvent = {
  event: string;
  video_id: string;
  stage: ProcessingStatus | null;
  status: string;
  progress: number | null;
  message: string | null;
  detail: string | null;
  timestamp: string;
};
export type Summary = { [key: string]: string };
export type Answer = { answer: string; sources: QuestionSource[] };
export type ChatMessage = {
  id: string;
  role: "user" | "assistant" | "system";
  content: string;
  timestamp: string;
  sources: QuestionSource[];
  timestamps: number[];
  answer_audio_ref: string | null;
};
export type ChatListItem = {
  chat_id: string;
  title: string;
  created_at: string;
  updated_at: string;
  video_id: string | null;
};
export type ChatRecord = ChatListItem & {
  username: string;
  video_metadata: Record<string, unknown> | null;
  summary: Summary | null;
  transcript: { video_id?: string; segments: TranscriptSegment[] } | null;
  messages: ChatMessage[];
};
export type ChatQuestionResponse = {
  user_message: ChatMessage;
  assistant_message: ChatMessage;
  sources: QuestionSource[];
  answer_audio_location: string | null;
};
export type ChatAnswerAudioResponse = { answer_audio_ref: string };
export type VoiceAnswer = Answer & {
  transcribed_question: string;
  audio_answer_location: string;
};

function apiUrl(path: string) {
  return `${API_BASE}${path}`;
}

function request<T>(path: string, options?: RequestInit): Promise<T> {
  const method = (options?.method || "GET").toUpperCase();
  if (method === "GET") {
    const pending = pendingGetRequests.get(path);
    if (pending) return pending as Promise<T>;
  }
  const pending = performRequest<T>(path, options);
  if (method === "GET") {
    pendingGetRequests.set(path, pending);
    void pending.finally(() => {
      if (pendingGetRequests.get(path) === pending) pendingGetRequests.delete(path);
    }).catch(() => undefined);
  }
  return pending;
}

async function performRequest<T>(path: string, options?: RequestInit): Promise<T> {
  let response: Response;
  try {
    response = await fetch(apiUrl(path), { ...options, credentials: "include" });
  } catch {
    throw new Error("Can't reach the VideoMind server. Check that the backend is running and try again.");
  }
  if (response.status === 401 && !path.startsWith("/auth/")) {
    pendingGetRequests.clear();
    onSessionExpired?.();
  }
  if (!response.ok) {
    let message = `Request failed (${response.status})`;
    try {
      const body = await response.json();
      if (typeof body.detail === "string") message = body.detail;
      else if (Array.isArray(body.detail)) message = body.detail.map((item: { msg?: string }) => item.msg).filter(Boolean).join(" ") || message;
    } catch {
      // Keep the HTTP status message when the server did not return JSON.
    }
    throw new ApiError(message, response.status);
  }
  if (response.status === 204) return undefined as T;
  return response.json() as Promise<T>;
}

export function getCurrentUser() {
  return request<AuthUser>("/auth/me");
}

export function login(username: string, password: string) {
  pendingGetRequests.clear();
  return request<AuthUser>("/auth/login", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ username, password }),
  });
}

export function register(username: string, password: string) {
  return request<AuthUser>("/auth/register", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ username, password }),
  });
}

export function logout() {
  pendingGetRequests.clear();
  return request<void>("/auth/logout", { method: "POST" });
}

export function uploadVideo(file: File) {
  const form = new FormData();
  form.append("file", file);
  return request<VideoJob>("/videos/upload", { method: "POST", body: form });
}

export function processYouTube(url: string) {
  return request<VideoJob>("/videos/youtube", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ url }),
  });
}

export function getStatus(videoId: string) {
  return request<VideoStatus>(`/videos/${videoId}/status`);
}

export function eventsUrl(videoId: string) {
  return apiUrl(`/videos/${videoId}/events`);
}

export function getTranscript(videoId: string) {
  return request<{ video_id: string; segments: TranscriptSegment[] }>(`/videos/${videoId}/transcript`);
}

export function getSummary(videoId: string) {
  return request<{ video_id: string; summary: Summary }>(`/videos/${videoId}/summary`);
}

export function listChats() {
  return request<ChatListItem[]>("/chats");
}

export function createChat(videoId?: string) {
  return request<ChatRecord>("/chats", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(videoId ? { video_id: videoId } : {}),
  });
}

export function getChat(chatId: string) {
  return request<ChatRecord>(`/chats/${chatId}`);
}

export function askChatQuestion(chatId: string, question: string) {
  return request<ChatQuestionResponse>(`/chats/${chatId}/messages`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ question }),
  });
}

export function generateChatAnswerAudio(chatId: string, messageId: string) {
  return request<ChatAnswerAudioResponse>(`/chats/${chatId}/messages/${messageId}/audio`, { method: "POST" });
}

export async function fetchChatAnswerAudio(chatId: string, messageId: string) {
  const path = `/chats/${chatId}/messages/${messageId}/audio`;
  let response: Response;
  try {
    response = await fetch(apiUrl(path), { credentials: "include" });
  } catch {
    throw new Error("Can't reach the VideoMind server. Check that the backend is running and try again.");
  }
  if (response.status === 401) onSessionExpired?.();
  if (!response.ok) {
    let message = `Request failed (${response.status})`;
    try {
      const body = await response.json();
      if (typeof body.detail === "string") message = body.detail;
    } catch {
      // Keep the HTTP status message when the server did not return JSON.
    }
    throw new ApiError(message, response.status);
  }
  return response.blob();
}

export function askQuestion(videoId: string, question: string) {
  return request<Answer>(`/videos/${videoId}/question`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ question }),
  });
}

export function askVoiceQuestion(videoId: string, audio: Blob) {
  const form = new FormData();
  form.append("file", audio, "voice-question.webm");
  return request<VoiceAnswer>(`/videos/${videoId}/voice-question`, { method: "POST", body: form });
}

export function resolveMediaUrl(path: string) {
  if (path.startsWith("http")) return path;
  return apiUrl(path);
}

export function formatTime(seconds: number) {
  const total = Math.max(0, Math.floor(seconds));
  const minutes = Math.floor(total / 60);
  const remaining = String(total % 60).padStart(2, "0");
  return `${minutes}:${remaining}`;
}
