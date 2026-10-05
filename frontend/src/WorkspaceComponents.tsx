import { useEffect, useId, useRef, useState, type FormEvent, type KeyboardEvent, type ReactNode, type RefObject } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import type { ChatListItem, ChatMessage, ProcessingEvent, ProcessingStage, Summary, TranscriptSegment } from "./api";
import { formatTime, resolveMediaUrl } from "./api";

export type WorkspaceView = "chat" | "new-video" | "summary" | "video" | "transcript";

export function ChatSidebar({ username, chats, selectedChatId, activeView, videoReady, isLoadingChats, isOpen, onClose, onView, onNewChat, onSelectChat, onLogout, themeToggle }: {
  username: string;
  chats: ChatListItem[];
  selectedChatId: string | null;
  activeView: WorkspaceView;
  videoReady: boolean;
  isLoadingChats: boolean;
  isOpen: boolean;
  onClose: () => void;
  onView: (view: WorkspaceView) => void;
  onNewChat: () => void;
  onSelectChat: (chatId: string) => void;
  onLogout: () => void;
  themeToggle: ReactNode;
}) {
  const selectView = (view: WorkspaceView) => {
    onView(view);
    onClose();
  };

  return <>
    {isOpen && <button className="sidebar-scrim" type="button" aria-label="Close navigation" onClick={onClose} />}
    <aside className={`chat-sidebar ${isOpen ? "is-open" : ""}`}>
      <div className="sidebar-brand"><span className="brand-mark">V</span><span>VideoMind</span><button className="sidebar-close" type="button" onClick={onClose} aria-label="Close navigation">×</button></div>
      <div className="sidebar-actions">
        <button className={`sidebar-action ${activeView === "new-video" ? "active" : ""}`} type="button" onClick={() => selectView("new-video")}><span aria-hidden="true">＋</span>New video</button>
        <button className="sidebar-action" type="button" onClick={() => { onNewChat(); onClose(); }} disabled={!videoReady} title={videoReady ? "Start another conversation with this video" : "Process a video before starting a chat"}><span aria-hidden="true">✳</span>New chat</button>
      </div>
      <div className="sidebar-section-label">Current video</div>
      <nav className="sidebar-context" aria-label="Current video">
        <button type="button" className={activeView === "summary" ? "active" : ""} onClick={() => selectView("summary")} disabled={!videoReady}><span aria-hidden="true">▤</span>Summary</button>
        <button type="button" className={activeView === "video" ? "active" : ""} onClick={() => selectView("video")} disabled={!videoReady}><span aria-hidden="true">▷</span>Video</button>
        <button type="button" className={activeView === "transcript" ? "active" : ""} onClick={() => selectView("transcript")} disabled={!videoReady}><span aria-hidden="true">≡</span>Transcript</button>
      </nav>
      <div className="sidebar-history-heading"><span className="sidebar-section-label">Your chats</span>{isLoadingChats && <span className="sidebar-loading" role="status">Loading</span>}</div>
      <nav className="chat-history" aria-label="Existing chats">
        {chats.map((chat) => <button type="button" key={chat.chat_id} className={`chat-history-item ${selectedChatId === chat.chat_id ? "active" : ""}`} onClick={() => { onSelectChat(chat.chat_id); onClose(); }} title={chat.title}>
          <span className="chat-history-title">{chat.title || "New conversation"}</span>
          <time dateTime={chat.updated_at}>{new Date(chat.updated_at).toLocaleDateString([], { month: "short", day: "numeric" })}</time>
        </button>)}
        {!isLoadingChats && chats.length === 0 && <p className="sidebar-empty">Your saved conversations will appear here.</p>}
      </nav>
      <footer className="sidebar-footer"><div className="sidebar-user"><span className="user-avatar">{username.slice(0, 1).toUpperCase()}</span><span title={username}>{username}</span></div><div className="sidebar-footer-actions">{themeToggle}<button className="logout-button" type="button" onClick={onLogout} title="Log out" aria-label="Log out">↪</button></div></footer>
    </aside>
  </>;
}

export function ChatWindow({ chatTitle, messages, hasVideo, videoReady, isLoading, isAsking, question, onQuestionChange, onSubmit, onMenu, onJump }: {
  chatTitle: string;
  messages: ChatMessage[];
  hasVideo: boolean;
  videoReady: boolean;
  isLoading: boolean;
  isAsking: boolean;
  question: string;
  onQuestionChange: (value: string) => void;
  onSubmit: () => void;
  onMenu: () => void;
  onJump: (seconds: number) => void;
}) {
  const feedRef = useRef<HTMLDivElement>(null);
  const submit = (event: FormEvent<HTMLFormElement>) => { event.preventDefault(); onSubmit(); };
  const submitOnEnter = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      onSubmit();
    }
  };

  useEffect(() => {
    feedRef.current?.scrollTo({ top: feedRef.current.scrollHeight, behavior: "smooth" });
  }, [messages, isAsking]);

  return <section className="chat-window" aria-label="Conversation">
    <header className="chat-header"><button className="mobile-menu-button" type="button" onClick={onMenu} aria-label="Open navigation">☰</button><div className="chat-header-title"><span className="chat-header-kicker">VideoMind conversation</span><h1>{chatTitle || "New conversation"}</h1></div><span className={`chat-context-status ${videoReady ? "ready" : ""}`}>{videoReady ? "Video ready" : hasVideo ? "Processing" : "No video"}</span></header>
    <div className="chat-feed" ref={feedRef} aria-live="polite">
      {isLoading && <div className="chat-state"><span className="loading-spinner" />Loading conversation...</div>}
      {!isLoading && messages.length === 0 && <div className="chat-welcome"><span className="welcome-mark">V</span><h2>{hasVideo ? "What would you like to know?" : "Start with a video"}</h2><p>{hasVideo ? "Ask a question about the selected video. Your conversation will be saved here." : "Choose New video to process a source, or open one of your saved conversations."}</p></div>}
      {!isLoading && [...messages].sort((left, right) => Date.parse(left.timestamp) - Date.parse(right.timestamp)).map((message) => <ChatMessage key={message.id} message={message} onJump={onJump} canJump={videoReady} />)}
      {isAsking && <div className="assistant-pending"><span className="loading-spinner" /><span>Thinking through the video...</span></div>}
    </div>
    <form className="chat-composer" onSubmit={submit}>{!videoReady && <p className="composer-note">{hasVideo ? "Questions will be available when video processing finishes." : "Select or process a video before asking questions."}</p>}<div className="composer-box"><textarea aria-label="Message" placeholder={videoReady ? "Ask anything about this video..." : "Ask about your video"} rows={1} value={question} onChange={(event) => onQuestionChange(event.target.value)} onKeyDown={submitOnEnter} disabled={!videoReady || isAsking || isLoading} /><button type="submit" className="send-button" aria-label={isAsking ? "Sending message" : "Send message"} disabled={!videoReady || !question.trim() || isAsking || isLoading}>{isAsking ? <span className="send-pulse">···</span> : "↑"}</button></div><span className="composer-hint">Enter to send · Shift + Enter for a new line</span></form>
  </section>;
}

function ChatMessage({ message, onJump, canJump }: { message: ChatMessage; onJump: (seconds: number) => void; canJump: boolean }) {
  const [sourcesExpanded, setSourcesExpanded] = useState(false);
  const sourcesId = useId();
  const isUser = message.role === "user";
  if (message.role === "system") return null;
  return <article className={`chat-message ${isUser ? "from-user" : "from-assistant"}`}><div className="message-avatar" aria-hidden="true">{isUser ? "Y" : "V"}</div><div className="message-content"><div className="message-meta"><strong>{isUser ? "You" : "VideoMind"}</strong><time dateTime={message.timestamp}>{new Date(message.timestamp).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })}</time></div>{isUser ? <p className="message-user-text">{message.content}</p> : <div className="message-markdown"><ReactMarkdown remarkPlugins={[remarkGfm]}>{message.content}</ReactMarkdown></div>}{!isUser && message.sources.length > 0 && <div className="message-sources"><button className="message-sources-toggle" type="button" aria-expanded={sourcesExpanded} aria-controls={sourcesId} onClick={() => setSourcesExpanded((expanded) => !expanded)}><span className={`source-chevron ${sourcesExpanded ? "expanded" : ""}`} aria-hidden="true">›</span><span>Relevant transcript / Sources</span><span className="source-count">{message.sources.length}</span></button><div className="message-source-list" id={sourcesId} hidden={!sourcesExpanded}>{message.sources.map((source, index) => <div className="message-source-item" key={`${source.start}-${index}`}><div className="message-source-times"><button type="button" onClick={() => onJump(source.start)} disabled={!canJump} aria-label={`Seek to ${formatTime(source.start)}`}><span>Start</span><time>{formatTime(source.start)}</time></button><button type="button" onClick={() => onJump(source.end)} disabled={!canJump} aria-label={`Seek to ${formatTime(source.end)}`}><span>End</span><time>{formatTime(source.end)}</time></button></div><p>{source.text}</p></div>)}</div></div>}</div></article>;
}

export function NewVideoPanel({ fileName, sourceUrl, isSubmitting, onFile, onUrlChange, onStart }: {
  fileName: string;
  sourceUrl: string;
  isSubmitting: boolean;
  onFile: (file: File | null) => void;
  onUrlChange: (url: string) => void;
  onStart: () => void;
}) {
  return <section className="workspace-panel new-video-panel"><div className="workspace-panel-heading"><span className="chat-header-kicker">New video</span><h1>Bring a video into focus.</h1><p>Upload a media file or paste a public YouTube link to create a searchable workspace.</p></div><label className={`video-drop-zone ${fileName ? "has-file" : ""}`}><input type="file" accept="video/*,audio/*" onChange={(event) => onFile(event.target.files?.[0] || null)} /><span className="upload-symbol">↑</span><strong>{fileName || "Choose a video or audio file"}</strong><span>{fileName ? "Ready to process" : "Browse from your device"}</span></label><div className="or-divider"><span>or paste a link</span></div><form className="new-video-url" onSubmit={(event) => { event.preventDefault(); onStart(); }}><input aria-label="YouTube URL" value={sourceUrl} onChange={(event) => onUrlChange(event.target.value)} placeholder="https://youtube.com/watch?v=..." /><button className="primary-button" type="submit" disabled={isSubmitting || (!fileName && !sourceUrl.trim())}>{isSubmitting ? "Starting..." : "Process video"}<span aria-hidden="true">↗</span></button></form><p className="new-video-footnote">Once ready, a chat will be created automatically. You can start more chats with this video without processing it again.</p></section>;
}

export function ProcessingPanel({ status, stages, activity }: { status: string; stages: ProcessingStage[]; activity: ProcessingEvent[] }) {
  const failed = status === "failed";
  return <section className="workspace-panel processing-panel"><div className="workspace-panel-heading"><span className="chat-header-kicker">Video processing</span><h1>{failed ? "Processing needs attention" : status === "completed" ? "Your video is ready" : "Building your video workspace"}</h1><p>{failed ? "The source could not be processed. Choose another source to try again." : status === "completed" ? "Opening a chat for this video..." : "VideoMind is preparing a transcript and summary."}</p></div><div className="processing-list">{stages.map((stage) => <div className={`processing-list-item ${stage.status}`} key={stage.id}><span aria-hidden="true">{stage.status === "completed" || stage.status === "skipped" ? "✓" : stage.status === "running" ? "◉" : stage.status === "failed" ? "×" : "○"}</span><strong>{stage.display_name}</strong>{stage.progress !== null && <time>{stage.progress}%</time>}</div>)}</div>{activity.length > 0 && <p className="processing-latest">{activity[activity.length - 1].message || activity[activity.length - 1].event.replaceAll("_", " ")}</p>}</section>;
}

export function SummaryPanel({ summary }: { summary: Summary | null }) {
  return <section className="workspace-panel context-panel"><div className="workspace-panel-heading"><span className="chat-header-kicker">Current video</span><h1>Summary</h1><p>A structured overview of the selected video.</p></div>{summary ? <div className="context-summary">{Object.entries(summary).map(([key, value]) => <section key={key}><span>{key.replaceAll("_", " ")}</span><p>{value}</p></section>)}</div> : <div className="context-empty">Summary is not available for this video yet.</div>}</section>;
}

export function VideoPanel({ videoId, videoRef, ready }: { videoId: string | null; videoRef: RefObject<HTMLVideoElement | null>; ready: boolean }) {
  return <section className="workspace-panel context-panel"><div className="workspace-panel-heading"><span className="chat-header-kicker">Current video</span><h1>Video</h1><p>Source playback for the selected conversation.</p></div><div className="context-video-frame">{videoId && ready ? <video ref={videoRef} controls src={resolveMediaUrl(`/videos/${videoId}/media`)} /> : <div className="context-empty">Video playback is not available yet.</div>}</div></section>;
}

export function TranscriptPanel({ transcript, onJump, ready }: { transcript: TranscriptSegment[]; onJump: (seconds: number) => void; ready: boolean }) {
  return <section className="workspace-panel context-panel"><div className="workspace-panel-heading"><span className="chat-header-kicker">Current video</span><h1>Transcript</h1><p>{transcript.length ? `${transcript.length} timestamped segments` : "Transcript for the selected video."}</p></div>{transcript.length ? <div className="context-transcript">{transcript.map((segment, index) => <button type="button" key={`${segment.start}-${index}`} onClick={() => onJump(segment.start)} disabled={!ready}><time>{formatTime(segment.start)}</time><span>{segment.text}</span></button>)}</div> : <div className="context-empty">Transcript is not available for this video yet.</div>}</section>;
}