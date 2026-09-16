import { useEffect, useRef, useState, type KeyboardEvent } from "react";
import {
  askQuestion,
  askVoiceQuestion,
  formatTime,
  getStatus,
  getSummary,
  getTranscript,
  processYouTube,
  resolveMediaUrl,
  type Answer,
  type ProcessingStatus,
  type QuestionSource,
  type Summary,
  type TranscriptSegment,
  type VideoStatus,
  uploadVideo,
} from "./api";

type Notice = { kind: "error" | "info"; message: string } | null;
type SourceProps = { source: QuestionSource; onJump: (seconds: number) => void; canJump: boolean };

const statusLabels: Record<ProcessingStatus, string> = {
  queued: "Queued",
  downloading: "Fetching video",
  extracting_audio: "Preparing audio",
  transcribing: "Transcribing",
  indexing: "Building notes",
  completed: "Ready to explore",
  failed: "Processing failed",
};

const statusSteps: ProcessingStatus[] = ["queued", "downloading", "extracting_audio", "transcribing", "indexing", "completed"];

function SourceList({ source, onJump, canJump }: SourceProps) {
  return (
    <div className="source-item">
      <div>
        <span className="source-time">{formatTime(source.start)}</span>
        <span className="source-copy">{source.text}</span>
      </div>
      <button className="jump-button" type="button" onClick={() => onJump(source.start)} disabled={!canJump} title={canJump ? "Jump to this timestamp" : "Video playback is not available"}>
        Jump to timestamp
      </button>
    </div>
  );
}

export default function App() {
  const [file, setFile] = useState<File | null>(null);
  const [sourceUrl, setSourceUrl] = useState("");
  const [videoId, setVideoId] = useState<string | null>(null);
  const [status, setStatus] = useState<VideoStatus | null>(null);
  const [transcript, setTranscript] = useState<TranscriptSegment[]>([]);
  const [summary, setSummary] = useState<Summary | null>(null);
  const [question, setQuestion] = useState("");
  const [answer, setAnswer] = useState<Answer | null>(null);
  const [voiceQuestion, setVoiceQuestion] = useState("");
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [isAsking, setIsAsking] = useState(false);
  const [isRecording, setIsRecording] = useState(false);
  const [notice, setNotice] = useState<Notice>(null);
  const [previewUrl, setPreviewUrl] = useState<string | null>(null);
  const videoRef = useRef<HTMLVideoElement>(null);
  const recorderRef = useRef<MediaRecorder | null>(null);
  const chunksRef = useRef<Blob[]>([]);

  const isReady = status?.status === "completed";
  const canAsk = Boolean(videoId && isReady);
  const isBusy = status !== null && !isReady && status.status !== "failed";

  useEffect(() => {
    return () => {
      if (previewUrl) URL.revokeObjectURL(previewUrl);
    };
  }, [previewUrl]);

  useEffect(() => {
    if (!videoId) return;
    let cancelled = false;

    const refresh = async () => {
      try {
        const nextStatus = await getStatus(videoId);
        if (cancelled) return;
        setStatus(nextStatus);
        if (nextStatus.status === "completed") {
          const [transcriptResult, summaryResult] = await Promise.allSettled([getTranscript(videoId), getSummary(videoId)]);
          if (cancelled) return;
          if (transcriptResult.status === "fulfilled") setTranscript(transcriptResult.value.segments);
          if (summaryResult.status === "fulfilled") setSummary(summaryResult.value.summary);
        }
      } catch (error) {
        if (!cancelled) setNotice({ kind: "error", message: error instanceof Error ? error.message : "Could not read processing status." });
      }
    };

    void refresh();
    const interval = window.setInterval(() => void refresh(), 1800);
    return () => {
      cancelled = true;
      window.clearInterval(interval);
    };
  }, [videoId]);

  const resetWorkspace = () => {
    setFile(null);
    setSourceUrl("");
    setVideoId(null);
    setStatus(null);
    setTranscript([]);
    setSummary(null);
    setAnswer(null);
    setVoiceQuestion("");
    setNotice(null);
    setPreviewUrl(null);
  };

  const chooseFile = (nextFile: File | null) => {
    setNotice(null);
    if (!nextFile) return;
    if (previewUrl) URL.revokeObjectURL(previewUrl);
    setFile(nextFile);
    setSourceUrl("");
    setPreviewUrl(URL.createObjectURL(nextFile));
  };

  const startProcessing = async () => {
    if (!file && !sourceUrl.trim()) {
      setNotice({ kind: "info", message: "Choose a video file or paste a YouTube link first." });
      return;
    }
    setIsSubmitting(true);
    setNotice(null);
    setAnswer(null);
    try {
      const job = file ? await uploadVideo(file) : await processYouTube(sourceUrl.trim());
      setVideoId(job.video_id);
      setStatus({ ...job, updated_at: new Date().toISOString() });
    } catch (error) {
      setNotice({ kind: "error", message: error instanceof Error ? error.message : "Could not start processing." });
    } finally {
      setIsSubmitting(false);
    }
  };

  const ask = async () => {
    if (!videoId || !question.trim()) return;
    setIsAsking(true);
    setNotice(null);
    try {
      setAnswer(await askQuestion(videoId, question.trim()));
    } catch (error) {
      setNotice({ kind: "error", message: error instanceof Error ? error.message : "Could not answer that question." });
    } finally {
      setIsAsking(false);
    }
  };

  const toggleRecording = async () => {
    if (isRecording) {
      recorderRef.current?.stop();
      setIsRecording(false);
      return;
    }
    if (!canAsk || !navigator.mediaDevices?.getUserMedia) {
      setNotice({ kind: "error", message: "Microphone recording is not available in this browser." });
      return;
    }
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      const recorder = new MediaRecorder(stream);
      chunksRef.current = [];
      recorder.ondataavailable = (event) => {
        if (event.data.size) chunksRef.current.push(event.data);
      };
      recorder.onstop = async () => {
        stream.getTracks().forEach((track) => track.stop());
        setIsAsking(true);
        setNotice(null);
        try {
          const result = await askVoiceQuestion(videoId!, new Blob(chunksRef.current, { type: recorder.mimeType || "audio/webm" }));
          setVoiceQuestion(result.transcribed_question);
          setAnswer(result);
        } catch (error) {
          setNotice({ kind: "error", message: error instanceof Error ? error.message : "Could not process the voice question." });
        } finally {
          setIsAsking(false);
        }
      };
      recorderRef.current = recorder;
      recorder.start();
      setIsRecording(true);
    } catch {
      setNotice({ kind: "error", message: "Microphone permission was not granted." });
    }
  };

  const jumpTo = (seconds: number) => {
    if (!videoRef.current) return;
    videoRef.current.currentTime = seconds;
    void videoRef.current.play();
  };

  const submitOnEnter = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      void ask();
    }
  };

  return (
    <main className="app-shell">
      <header className="topbar">
        <div className="brand"><span className="brand-mark">V</span><span>VideoMind</span></div>
        <button className="new-button" type="button" onClick={resetWorkspace}>New video <span>+</span></button>
      </header>

      <section className="hero">
        <p className="eyebrow">Your video, made searchable</p>
        <h1>Understand more.<br /><em>Watch less.</em></h1>
        <p className="hero-copy">Bring a video, get the signal, ask anything.</p>
      </section>

      <section className="workspace">
        {!videoId && (
          <div className="ingest-panel panel">
            <div className="panel-heading">
              <div><span className="section-kicker">01 / Bring a source</span><h2>What should we explore?</h2></div>
              <span className="soft-label">Private workspace</span>
            </div>
            <label className={`drop-zone ${file ? "has-file" : ""}`}>
              <input type="file" accept="video/*,audio/*" onChange={(event) => chooseFile(event.target.files?.[0] || null)} />
              <span className="upload-icon">↑</span>
              <strong>{file ? file.name : "Drop a video here"}</strong>
              <span>{file ? `${(file.size / 1024 / 1024).toFixed(1)} MB ready to process` : "or click to browse your files"}</span>
            </label>
            <div className="or-divider"><span>or use a link</span></div>
            <div className="url-row">
              <input aria-label="YouTube URL" value={sourceUrl} onChange={(event) => { setSourceUrl(event.target.value); setFile(null); setPreviewUrl(null); }} placeholder="https://youtube.com/watch?v=..." />
              <button className="primary-button" type="button" onClick={() => void startProcessing()} disabled={isSubmitting}>{isSubmitting ? "Starting..." : "Start processing"}<span>↗</span></button>
            </div>
            <p className="helper-text">Supported video and audio files, plus public YouTube links.</p>
          </div>
        )}

        {videoId && (
          <>
            <div className="status-banner panel">
              <div className="status-copy"><span className={`status-dot ${status?.status === "failed" ? "is-error" : isReady ? "is-ready" : "is-loading"}`} /><div><span className="section-kicker">Processing status</span><strong>{status ? statusLabels[status.status] : "Connecting..."}</strong></div></div>
              {status?.status === "failed" && <span className="error-inline">{status.error || "Something went wrong while processing."}</span>}
              {isBusy && <div className="progress-track"><div className="progress-fill" style={{ width: `${Math.max(8, ((statusSteps.indexOf(status?.status || "queued") + 1) / statusSteps.length) * 100)}%` }} /></div>}
            </div>
            {status?.status === "failed" && <div className="failed-panel panel"><strong>We could not finish this video.</strong><span>Check the source and try again with a different file or link.</span><button className="secondary-button" type="button" onClick={resetWorkspace}>Try another source</button></div>}

            <div className="content-grid">
              <section className="media-column">
                <div className="video-frame panel">
                  {previewUrl ? <video ref={videoRef} controls src={previewUrl} /> : <div className="unavailable-media"><span className="play-glyph">▶</span><strong>Playback unavailable</strong><span>The original video is not retained by this workspace.</span></div>}
                </div>
                <div className="transcript panel">
                  <div className="panel-heading compact"><div><span className="section-kicker">02 / The record</span><h2>Transcript</h2></div>{transcript.length > 0 && <span className="count-label">{transcript.length} segments</span>}</div>
                  {transcript.length ? <div className="transcript-list">{transcript.map((segment, index) => <button className="transcript-row" type="button" key={`${segment.start}-${index}`} onClick={() => jumpTo(segment.start)} disabled={!previewUrl}><span>{formatTime(segment.start)}</span><p>{segment.text}</p></button>)}</div> : <EmptyState text={isReady ? "No transcript is available for this source." : "Your timestamped transcript will appear here when processing finishes."} />}
                </div>
              </section>

              <aside className="insight-column">
                <section className="summary panel"><div className="panel-heading compact"><div><span className="section-kicker">03 / The signal</span><h2>Summary</h2></div><span className="summary-icon">✦</span></div>{summary ? <div className="summary-body">{Object.entries(summary).map(([key, value]) => <div className="summary-block" key={key}><span>{key.replaceAll("_", " ")}</span><p>{value}</p></div>)}</div> : <EmptyState text={isReady ? "Summary is not available for this source." : "A concise summary will appear here when processing finishes."} />}{summary && <SummaryAudio videoId={videoId} />}</section>
                <section className="ask panel"><div className="panel-heading compact"><div><span className="section-kicker">04 / Ask the video</span><h2>What do you want to know?</h2></div></div><div className="ask-box"><textarea value={question} onChange={(event) => setQuestion(event.target.value)} onKeyDown={submitOnEnter} disabled={!canAsk || isAsking} placeholder={canAsk ? "Ask about a moment, idea, or detail..." : "Available when processing finishes"} rows={3} /><div className="ask-actions"><span>Enter to ask · Shift + Enter for a new line</span><button className={`mic-button ${isRecording ? "recording" : ""}`} type="button" onClick={() => void toggleRecording()} disabled={!canAsk || isAsking} title={isRecording ? "Stop recording" : "Ask with your microphone"}>{isRecording ? "■" : "●"}</button><button className="ask-button" type="button" onClick={() => void ask()} disabled={!canAsk || !question.trim() || isAsking}>{isAsking ? "Thinking..." : "Ask"}<span>↗</span></button></div></div>{voiceQuestion && <div className="voice-question"><span>Heard you say</span><p>“{voiceQuestion}”</p></div>}{answer && <AnswerCard answer={answer} onJump={jumpTo} canJump={Boolean(previewUrl)} videoId={videoId} />}</section>
              </aside>
            </div>
          </>
        )}
      </section>
      {notice && <div className={`toast ${notice.kind}`} role="alert">{notice.message}<button type="button" onClick={() => setNotice(null)}>×</button></div>}
    </main>
  );
}

function EmptyState({ text }: { text: string }) { return <div className="empty-state"><span>◌</span><p>{text}</p></div>; }

function SummaryAudio({ videoId }: { videoId: string }) {
  const [failed, setFailed] = useState(false);
  if (failed) return <p className="audio-unavailable">Summary audio is unavailable for this video.</p>;
  return <div className="audio-player"><span>Listen to summary</span><audio controls src={resolveMediaUrl(`/videos/${videoId}/summary/audio`)} onError={() => setFailed(true)} /></div>;
}

function AnswerCard({ answer, onJump, canJump, videoId }: { answer: Answer; onJump: (seconds: number) => void; canJump: boolean; videoId: string }) {
  const voiceAudio = "audio_answer_location" in answer ? answer.audio_answer_location : null;
  return <div className="answer-card"><div className="answer-heading"><span>Answer</span>{voiceAudio && <audio controls src={resolveMediaUrl(voiceAudio)} />}</div><p className="answer-text">{answer.answer}</p>{answer.sources.length > 0 && <div className="sources"><span className="sources-label">Sources from the video</span>{answer.sources.map((source, index) => <SourceList key={`${source.start}-${index}`} source={source} onJump={onJump} canJump={canJump} />)}</div>}{answer.sources.length === 0 && <p className="no-sources">No matching timestamp was returned.</p>}<span className="answer-video-id" aria-hidden="true">{videoId}</span></div>;
}
