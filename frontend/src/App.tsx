import { useEffect, useRef, useState, type KeyboardEvent } from "react";
import {
  askQuestion,
  askVoiceQuestion,
  eventsUrl,
  formatTime,
  getStatus,
  getSummary,
  getTranscript,
  processYouTube,
  resolveMediaUrl,
  type Answer,
  type ProcessingStatus,
  type ProcessingEvent,
  type ProcessingStage,
  type QuestionSource,
  type Summary,
  type TranscriptSegment,
  type VideoStatus,
  type VoiceAnswer,
  uploadVideo,
} from "./api";

type Notice = { kind: "error" | "info"; message: string } | null;
type SourceProps = { source: QuestionSource; onJump: (seconds: number) => void; canJump: boolean };

const statusLabels: Record<ProcessingStatus, string> = {
  queued: "Queued",
  validating: "Validating media",
  downloading: "Fetching video",
  extracting_audio: "Preparing audio",
  detecting_language: "Detecting language",
  transcribing: "Transcribing",
  building_transcript: "Building transcript",
  chunking: "Chunking transcript",
  embedding: "Generating embeddings",
  indexing: "Building notes",
  summarizing: "Generating summary",
  generating_summary_audio: "Generating summary audio",
  completed: "Ready to explore",
  failed: "Processing failed",
};

const statusSteps: ProcessingStatus[] = [
  "queued",
  "validating",
  "downloading",
  "extracting_audio",
  "detecting_language",
  "transcribing",
  "building_transcript",
  "chunking",
  "embedding",
  "indexing",
  "summarizing",
  "generating_summary_audio",
  "completed",
];

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
  const [stages, setStages] = useState<ProcessingStage[]>([]);
  const [activity, setActivity] = useState<ProcessingEvent[]>([]);
  const [showActivity, setShowActivity] = useState(false);
  const [showProcessingDetails, setShowProcessingDetails] = useState(true);
  const [showTranscript, setShowTranscript] = useState(false);
  const [showSummary, setShowSummary] = useState(false);
  const [previewUrl, setPreviewUrl] = useState<string | null>(null);
  const videoRef = useRef<HTMLVideoElement>(null);
  const recorderRef = useRef<MediaRecorder | null>(null);
  const chunksRef = useRef<Blob[]>([]);

  const isReady = status?.status === "completed";
  const canAsk = Boolean(videoId && isReady);
  const isBusy = status !== null && !isReady && status.status !== "failed";

  useEffect(() => {
    setShowProcessingDetails(!isReady);
  }, [isReady]);

  useEffect(() => {
    const recovered = window.localStorage.getItem("videomind.videoId");
    if (recovered) setVideoId(recovered);
  }, []);

  useEffect(() => {
    return () => {
      if (previewUrl) URL.revokeObjectURL(previewUrl);
    };
  }, [previewUrl]);

  useEffect(() => {
    if (!videoId) return;
    let cancelled = false;
    const source = new EventSource(eventsUrl(videoId));

    const refresh = async () => {
      try {
        const nextStatus = await getStatus(videoId);
        if (cancelled) return;
        setStatus(nextStatus);
        setStages(nextStatus.stages || []);
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
    const handleEvent = (message: MessageEvent) => {
      const event = JSON.parse(message.data) as ProcessingEvent;
      setActivity((current) => [...current.slice(-39), event]);
      void refresh();
    };
    ["stage_started", "stage_progress", "stage_completed", "stage_failed", "processing_completed"].forEach((name) => source.addEventListener(name, handleEvent));
    source.onerror = () => { source.close(); };
    return () => {
      cancelled = true;
      source.close();
    };
  }, [videoId]);

  const resetWorkspace = () => {
    setFile(null);
    setSourceUrl("");
    setVideoId(null);
    window.localStorage.removeItem("videomind.videoId");
    setStatus(null);
    setTranscript([]);
    setSummary(null);
    setAnswer(null);
    setVoiceQuestion("");
    setNotice(null);
    setStages([]);
    setActivity([]);
    setShowProcessingDetails(true);
    setShowTranscript(false);
    setShowSummary(false);
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
      window.localStorage.setItem("videomind.videoId", job.video_id);
      setStatus({ ...job, updated_at: new Date().toISOString(), stages: [] });
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

  const overview = summary
    ? Object.entries(summary).find(([key]) => key.toLowerCase() === "overview")?.[1]
    : null;

  return (
    <main className="app-shell">
      <header className="topbar">
        <div className="brand"><span className="brand-mark">V</span><span>VideoMind</span></div>
        <button className="new-button" type="button" onClick={resetWorkspace}>New video <span>+</span></button>
      </header>

      {!videoId && <section className="hero">
        <p className="eyebrow">Your video, made searchable</p>
        <h1>Understand more.<br /><em>Watch less.</em></h1>
        <p className="hero-copy">Bring a video, get the signal, ask anything.</p>
      </section>}

      <section className="workspace">
        {!videoId && (
          <div className="ingest-panel panel">
            <div className="panel-heading">
              <div><span className="section-kicker">01 / The source</span><h2>What should we explore?</h2></div>
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
            <div className="results-stack">
              <ProcessingActivity stages={stages} activity={activity} showActivity={showActivity} onToggle={() => setShowActivity((value) => !value)} ready={isReady} expanded={showProcessingDetails} onExpandedToggle={() => setShowProcessingDetails((value) => !value)} transcriptCount={transcript.length} />
              {status?.status === "failed" && <div className="failed-panel panel"><strong>We could not finish this video.</strong><span>Check the source and try again with a different file or link.</span><button className="secondary-button" type="button" onClick={resetWorkspace}>Try another source</button></div>}

              <section className="summary panel">
                <div className="panel-heading compact collapsible-heading">
                  <div><span className="section-kicker">03 / The signal</span><h2>Summary</h2></div>
                  <button className="activity-toggle section-toggle" type="button" onClick={() => setShowSummary((value) => !value)} aria-expanded={showSummary} aria-controls="summary-content">
                    {showSummary ? "Hide summary" : "View summary"}<span>{showSummary ? "−" : "+"}</span>
                  </button>
                </div>
                {!showSummary && overview && <p className="summary-teaser">{overview}</p>}
                <div id="summary-content" hidden={!showSummary}>
                  {summary ? <div className="summary-body">{Object.entries(summary).map(([key, value]) => <div className="summary-block" key={key}><span>{key.replaceAll("_", " ")}</span><p>{value}</p></div>)}</div> : <EmptyState text={isReady ? "Summary is not available for this source." : "A concise summary will appear here when processing finishes."} />}
                  {summary && <SummaryAudio videoId={videoId} />}
                </div>
              </section>

              <section className="ask panel primary-action"><div className="panel-heading compact"><div><span className="section-kicker">04 / Ask the video</span><h2>What do you want to know?</h2></div></div><div className="ask-box"><textarea value={question} onChange={(event) => setQuestion(event.target.value)} onKeyDown={submitOnEnter} disabled={!canAsk || isAsking} placeholder={canAsk ? "Ask about a moment, idea, or detail..." : "Available when processing finishes"} rows={3} /><div className="ask-actions"><span>Enter to ask · Shift + Enter for a new line</span><button className={`mic-button ${isRecording ? "recording" : ""}`} type="button" onClick={() => void toggleRecording()} disabled={!canAsk || isAsking} title={isRecording ? "Stop recording" : "Ask with your microphone"}>{isRecording ? "■" : "●"}</button><button className="ask-button" type="button" onClick={() => void ask()} disabled={!canAsk || !question.trim() || isAsking}>{isAsking ? "Thinking..." : "Ask"}<span>↗</span></button></div></div>{voiceQuestion && <div className="voice-question"><span>Heard you say</span><p>“{voiceQuestion}”</p></div>}{answer && <AnswerCard answer={answer} onJump={jumpTo} canJump={Boolean(previewUrl)} videoId={videoId} />}</section>

              <div className="video-frame panel">
                {previewUrl ? <video ref={videoRef} controls src={previewUrl} /> : <div className="unavailable-media"><span className="play-glyph">▶</span><strong>Playback unavailable</strong><span>The original video is not retained by this workspace.</span></div>}
              </div>

              <section className="transcript panel">
                <div className="panel-heading compact collapsible-heading">
                  <div><span className="section-kicker">02 / The record</span><h2>Transcript</h2></div>
                  <div className="section-heading-actions">
                    {transcript.length > 0 && <span className="count-label">{transcript.length} segments</span>}
                    <button className="activity-toggle section-toggle" type="button" onClick={() => setShowTranscript((value) => !value)} aria-expanded={showTranscript} aria-controls="transcript-content">
                      {showTranscript ? "Hide transcript" : "View transcript"}<span>{showTranscript ? "−" : "+"}</span>
                    </button>
                  </div>
                </div>
                <div id="transcript-content" hidden={!showTranscript}>
                  {transcript.length ? <div className="transcript-list">{transcript.map((segment, index) => <button className="transcript-row" type="button" key={`${segment.start}-${index}`} onClick={() => jumpTo(segment.start)} disabled={!previewUrl}><span>{formatTime(segment.start)}</span><p>{segment.text}</p></button>)}</div> : <EmptyState text={isReady ? "No transcript is available for this source." : "Your timestamped transcript will appear here when processing finishes."} />}
                </div>
              </section>
            </div>
          </>
        )}
      </section>
      {notice && <div className={`toast ${notice.kind}`} role="alert">{notice.message}<button type="button" onClick={() => setNotice(null)}>×</button></div>}
    </main>
  );
}

function EmptyState({ text }: { text: string }) { return <div className="empty-state"><span>◌</span><p>{text}</p></div>; }

function ProcessingActivity({ stages, activity, showActivity, onToggle, ready, expanded, onExpandedToggle, transcriptCount }: { stages: ProcessingStage[]; activity: ProcessingEvent[]; showActivity: boolean; onToggle: () => void; ready: boolean; expanded: boolean; onExpandedToggle: () => void; transcriptCount: number }) {
  return <section className="processing-activity panel" aria-label="Processing activity">
    {!expanded && <button className="processing-summary-bar" type="button" onClick={onExpandedToggle} aria-expanded={expanded} aria-controls="processing-details"><span className="processing-summary-icon" aria-hidden="true">✓</span><span><strong>Video ready</strong>{transcriptCount > 0 && ` · ${transcriptCount} segments transcribed`}</span><span className="processing-summary-action">View details <span>+</span></span></button>}
    <div id="processing-details" hidden={!expanded}>
    <div className="activity-header"><div><span className="section-kicker">Processing activity</span><h2>{ready ? "Video ready" : "Working through your video"}</h2></div><div className="activity-header-actions"><span className={`activity-state ${ready ? "ready" : ""}`}>{ready ? "Complete" : "Live"}</span><button className="activity-toggle section-toggle" type="button" onClick={onExpandedToggle} aria-expanded={expanded} aria-controls="processing-details">Hide details <span>−</span></button></div></div>
    <div className="stage-list" role="list">
      {stages.map((stage) => <div className={`stage-row ${stage.status}`} role="listitem" key={stage.id}>
        <span className="stage-icon" aria-hidden="true">{stage.status === "completed" || stage.status === "skipped" ? "✓" : stage.status === "running" ? "◉" : stage.status === "failed" ? "×" : "○"}</span>
        <div className="stage-main"><strong>{statusLabels[stage.id] || stage.display_name}</strong>{stage.status === "running" && stage.message && <span>{stage.message}</span>}{stage.detail && <small>{stage.detail}</small>}</div>
        {stage.progress !== null && stage.status !== "skipped" && <span className="stage-progress">{stage.progress}%</span>}
      </div>)}
    </div>
    <button className="activity-toggle" type="button" onClick={onToggle} aria-expanded={showActivity}>{showActivity ? "Hide activity details" : "View activity details"}<span>{showActivity ? "−" : "+"}</span></button>
    {showActivity && <div className="activity-log">{activity.length ? activity.map((event, index) => <div key={`${event.timestamp}-${index}`}><time>{new Date(event.timestamp).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" })}</time><span>{event.message || event.event.replaceAll("_", " ")}</span></div>) : <span>No activity recorded yet.</span>}</div>}
    </div>
  </section>;
}

function SummaryAudio({ videoId }: { videoId: string }) {
  const [failed, setFailed] = useState(false);
  if (failed) return <p className="audio-unavailable">Summary audio is unavailable for this video.</p>;
  return <div className="audio-player"><span>Listen to summary</span><audio controls src={resolveMediaUrl(`/videos/${videoId}/summary/audio`)} onError={() => setFailed(true)} /></div>;
}

function AnswerCard({ answer, onJump, canJump, videoId }: { answer: Answer | VoiceAnswer; onJump: (seconds: number) => void; canJump: boolean; videoId: string }) {
  const voiceAudio = "audio_answer_location" in answer ? answer.audio_answer_location : null;
  return <div className="answer-card"><div className="answer-heading"><span>Answer</span>{voiceAudio && <audio controls src={resolveMediaUrl(voiceAudio)} />}</div><p className="answer-text">{answer.answer}</p>{answer.sources.length > 0 && <div className="sources"><span className="sources-label">Sources from the video</span>{answer.sources.map((source, index) => <SourceList key={`${source.start}-${index}`} source={source} onJump={onJump} canJump={canJump} />)}</div>}{answer.sources.length === 0 && <p className="no-sources">No matching timestamp was returned.</p>}<span className="answer-video-id" aria-hidden="true">{videoId}</span></div>;
}
