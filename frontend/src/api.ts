const API_BASE = import.meta.env.VITE_API_BASE_URL || "/api";

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
export type VoiceAnswer = Answer & {
  transcribed_question: string;
  audio_answer_location: string;
};

function apiUrl(path: string) {
  return `${API_BASE}${path}`;
}

async function request<T>(path: string, options?: RequestInit): Promise<T> {
  const response = await fetch(apiUrl(path), options);
  if (!response.ok) {
    let message = `Request failed (${response.status})`;
    try {
      const body = await response.json();
      message = body.detail || message;
    } catch {
      // Keep the HTTP status message when the server did not return JSON.
    }
    throw new Error(message);
  }
  return response.json() as Promise<T>;
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
