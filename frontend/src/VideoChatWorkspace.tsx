import { useEffect, useRef, useState } from "react";
import {
  ApiError,
  askChatQuestion,
  createChat,
  deleteChat,
  eventsUrl,
  getChat,
  getStatus,
  getSummary,
  getTranscript,
  listChats,
  processYouTube,
  resolveMediaUrl,
  type ChatListItem,
  type ChatRecord,
  type ProcessingEvent,
  type ProcessingStage,
  type Summary,
  type TranscriptSegment,
  type VideoStatus,
  uploadVideo,
} from "./api";
import {
  ChatSidebar,
  ChatWindow,
  NewVideoPanel,
  ProcessingPanel,
  SummaryPanel,
  TranscriptPanel,
  VideoPanel,
  type WorkspaceView,
} from "./WorkspaceComponents";

type Theme = "dark-modern" | "solarized-light";
type Notice = { kind: "error" | "info"; message: string } | null;

export default function VideoChatWorkspace({ username, onLogout, theme, onThemeToggle }: {
  username: string;
  onLogout: () => void;
  theme: Theme;
  onThemeToggle: () => void;
}) {
  const [chats, setChats] = useState<ChatListItem[]>([]);
  const [selectedChatId, setSelectedChatId] = useState<string | null>(null);
  const [selectedChat, setSelectedChat] = useState<ChatRecord | null>(null);
  const [videoId, setVideoId] = useState<string | null>(null);
  const [chatToDelete, setChatToDelete] = useState<ChatListItem | null>(null);
  const [status, setStatus] = useState<VideoStatus | null>(null);
  const [summary, setSummary] = useState<Summary | null>(null);
  const [transcript, setTranscript] = useState<TranscriptSegment[]>([]);
  const [stages, setStages] = useState<ProcessingStage[]>([]);
  const [activity, setActivity] = useState<ProcessingEvent[]>([]);
  const [activeView, setActiveView] = useState<WorkspaceView>("chat");
  const [isSidebarOpen, setSidebarOpen] = useState(false);
  const [isLoadingChats, setLoadingChats] = useState(true);
  const [isLoadingChat, setLoadingChat] = useState(false);
  const [isSubmitting, setSubmitting] = useState(false);
  const [isAsking, setAsking] = useState(false);
  const [question, setQuestion] = useState("");
  const [file, setFile] = useState<File | null>(null);
  const [sourceUrl, setSourceUrl] = useState("");
  const [notice, setNotice] = useState<Notice>(null);
  const videoRef = useRef<HTMLVideoElement>(null);
  const askLockRef = useRef(false);
  const loadSequenceRef = useRef(0);
  const pendingAutoChatRef = useRef<string | null>(null);
  const autoChatCreatedRef = useRef<string | null>(null);
  const pendingJumpRef = useRef<number | null>(null);

  const videoReady = status?.status === "completed" || Boolean(videoId && summary && transcript.length > 0);
  const isProcessing = Boolean(status && status.status !== "completed" && status.status !== "failed");
  const chatVideoMismatch = Boolean(selectedChat && ((selectedChat.video_id && videoId && selectedChat.video_id !== videoId) || (!selectedChat.video_id && Boolean(videoId)) || (selectedChat.video_id && !videoId)));

  const refreshChats = async () => {
    const latest = await listChats();
    setChats(latest);
    return latest;
  };

  const loadActiveVideo = async (nextVideoId: string | null) => {
    if (!nextVideoId) {
      setVideoId(null);
      setStatus(null);
      setSummary(null);
      setTranscript([]);
      setStages([]);
      setActivity([]);
      return;
    }
    setVideoId(nextVideoId);
    setStatus(null);
    setSummary(null);
    setTranscript([]);
    setStages([]);
    setActivity([]);
    try {
      const videoStatus = await getStatus(nextVideoId);
      setStatus(videoStatus);
      setStages(videoStatus.stages || []);
      if (videoStatus.status === "completed") {
        const [transcriptResult, summaryResult] = await Promise.allSettled([getTranscript(nextVideoId), getSummary(nextVideoId)]);
        if (transcriptResult.status === "fulfilled") setTranscript(transcriptResult.value.segments);
        if (summaryResult.status === "fulfilled") setSummary(summaryResult.value.summary);
      }
    } catch (error) {
      if (!(error instanceof ApiError && error.status === 404)) {
        setNotice({ kind: "error", message: error instanceof Error ? error.message : "Could not read video status." });
      }
    }
  };

  const loadChat = async (chatId: string) => {
    const sequence = ++loadSequenceRef.current;
    setSelectedChatId(chatId);
    setSelectedChat(null);
    setLoadingChat(true);
    setNotice(null);
    setActiveView("chat");
    setQuestion("");
    try {
      const chat = await getChat(chatId);
      if (sequence !== loadSequenceRef.current) return;
      setSelectedChat(chat);
      const matchesActiveVideo = Boolean(chat.video_id && videoId && chat.video_id === videoId);
      const hasNoActiveVideo = !videoId && !chat.video_id;
      if (matchesActiveVideo || hasNoActiveVideo) {
        setVideoId(chat.video_id || null);
        setSummary(chat.summary);
        setTranscript(chat.transcript?.segments || []);
        setStatus(null);
        setStages([]);
        setActivity([]);
        if (chat.video_id) {
          try {
            const videoStatus = await getStatus(chat.video_id);
            if (sequence === loadSequenceRef.current) {
              setStatus(videoStatus);
              setStages(videoStatus.stages || []);
              if (videoStatus.status === "completed") {
                const [transcriptResult, summaryResult] = await Promise.allSettled([getTranscript(chat.video_id), getSummary(chat.video_id)]);
                if (sequence !== loadSequenceRef.current) return;
                if (transcriptResult.status === "fulfilled") setTranscript(transcriptResult.value.segments);
                if (summaryResult.status === "fulfilled") setSummary(summaryResult.value.summary);
              }
            }
          } catch (error) {
            if (sequence === loadSequenceRef.current && !(error instanceof ApiError && error.status === 404)) {
              setNotice({ kind: "error", message: error instanceof Error ? error.message : "Could not read video status." });
            }
          }
        }
      }
    } catch (error) {
      if (sequence === loadSequenceRef.current) {
        setNotice({ kind: "error", message: error instanceof Error ? error.message : "Could not load this chat." });
        setSelectedChatId(null);
        setSelectedChat(null);
      }
    } finally {
      if (sequence === loadSequenceRef.current) setLoadingChat(false);
    }
  };

  useEffect(() => {
    void refreshChats().then((items) => {
      if (items.length > 0) void loadChat(items[0].chat_id);
    }).catch((error: unknown) => {
      setNotice({ kind: "error", message: error instanceof Error ? error.message : "Could not load saved chats." });
    }).finally(() => setLoadingChats(false));
  }, [username]);

  useEffect(() => {
    if (!videoId) return;
    let cancelled = false;
    const source = new EventSource(eventsUrl(videoId), { withCredentials: true });
    const refreshStatus = async () => {
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
        if (!cancelled && !(error instanceof ApiError && error.status === 404)) {
          setNotice({ kind: "error", message: error instanceof Error ? error.message : "Could not read processing status." });
        }
      }
    };
    void refreshStatus();
    const handleEvent = (message: MessageEvent) => {
      const event = JSON.parse(message.data) as ProcessingEvent;
      setActivity((current) => [...current.slice(-39), event]);
      void refreshStatus();
    };
    ["stage_started", "stage_progress", "stage_completed", "stage_failed", "processing_completed"].forEach((name) => source.addEventListener(name, handleEvent));
    source.onerror = () => source.close();
    return () => { cancelled = true; source.close(); };
  }, [videoId]);

  useEffect(() => {
    if (!videoId || status?.status !== "completed" || pendingAutoChatRef.current !== videoId || autoChatCreatedRef.current === videoId) return;
    autoChatCreatedRef.current = videoId;
    pendingAutoChatRef.current = null;
    void createChat(videoId).then(async (chat) => {
      await refreshChats();
      await loadChat(chat.chat_id);
    }).catch((error: unknown) => {
      setNotice({ kind: "error", message: error instanceof Error ? error.message : "Video is ready, but a chat could not be created." });
    });
  }, [videoId, status?.status]);

  useEffect(() => {
    const jump = pendingJumpRef.current;
    const player = videoRef.current;
    if (activeView !== "video" || jump === null || !player) return;
    const seek = () => {
      player.currentTime = jump;
      void player.play();
      pendingJumpRef.current = null;
    };
    if (player.readyState >= 1) seek();
    else player.addEventListener("loadedmetadata", seek, { once: true });
  }, [activeView, videoId]);

  const startProcessing = async () => {
    if (!file && !sourceUrl.trim()) {
      setNotice({ kind: "info", message: "Choose a video file or paste a YouTube link first." });
      return;
    }
    setSubmitting(true);
    setNotice(null);
    setSelectedChatId(null);
    setSelectedChat(null);
    setSummary(null);
    setTranscript([]);
    try {
      const job = file ? await uploadVideo(file) : await processYouTube(sourceUrl.trim());
      setFile(null);
      setSourceUrl("");
      pendingAutoChatRef.current = job.video_id;
      autoChatCreatedRef.current = null;
      setVideoId(job.video_id);
      setStatus({ ...job, updated_at: new Date().toISOString(), stages: [] });
      setStages([]);
      setActivity([]);
    } catch (error) {
      setNotice({ kind: "error", message: error instanceof Error ? error.message : "Could not start processing." });
    } finally {
      setSubmitting(false);
    }
  };

  const startNewChat = async () => {
    if (!videoId || !videoReady) return;
    setNotice(null);
    try {
      const chat = await createChat(videoId);
      await refreshChats();
      await loadChat(chat.chat_id);
    } catch (error) {
      setNotice({ kind: "error", message: error instanceof Error ? error.message : "Could not create a new chat." });
    }
  };

  const ask = async () => {
    const currentQuestion = question.trim();
    const chatId = selectedChatId;
    if (!chatId || chatVideoMismatch || !videoReady || !currentQuestion || askLockRef.current) return;
    askLockRef.current = true;
    setAsking(true);
    setNotice(null);
    setQuestion("");
    try {
      const response = await askChatQuestion(chatId, currentQuestion);
      setSelectedChat((current) => current?.chat_id === chatId ? {
        ...current,
        messages: [...current.messages, response.user_message, response.assistant_message],
      } : current);
      await refreshChats();
    } catch (error) {
      setQuestion(currentQuestion);
      setNotice({ kind: "error", message: error instanceof Error ? error.message : "Could not answer that question." });
      try {
        const restored = await getChat(chatId);
        setSelectedChat((current) => current?.chat_id === chatId ? restored : current);
      } catch {
        // Keep the original request error visible.
      }
    } finally {
      askLockRef.current = false;
      setAsking(false);
    }
  };

  const jumpTo = (seconds: number) => {
    if (videoRef.current) {
      videoRef.current.currentTime = seconds;
      void videoRef.current.play();
      return;
    }
    pendingJumpRef.current = seconds;
    setActiveView("video");
  };

  const handleDeleteChat = async (chatId: string) => {
    const targetChat = chats.find((chat) => chat.chat_id === chatId);
    if (!targetChat) return;
    setChatToDelete(null);
    try {
      await deleteChat(chatId);
      setChats((current) => current.filter((chat) => chat.chat_id !== chatId));
      if (selectedChatId === chatId) {
        setSelectedChatId(null);
        setSelectedChat(null);
        setQuestion("");
      }
      setNotice({ kind: "info", message: "Chat deleted." });
      await refreshChats();
    } catch (error) {
      setNotice({ kind: "error", message: error instanceof Error ? error.message : "Could not delete this chat." });
    }
  };

  const updateAnswerAudioRef = (messageId: string, audioRef: string) => {
    const chatId = selectedChatId;
    if (!chatId) return;
    setSelectedChat((current) => current?.chat_id === chatId ? {
      ...current,
      messages: current.messages.map((message) => message.id === messageId
        ? { ...message, answer_audio_ref: audioRef }
        : message),
    } : current);
  };

  const themeToggle = <button className="theme-toggle" type="button" onClick={onThemeToggle} aria-label={`Current theme: ${theme}. Switch theme.`} title="Switch theme"><span aria-hidden="true">◐</span>{theme === "dark-modern" ? "Dark" : "Light"}</button>;
  const title = selectedChat?.title || (selectedChat?.video_id ? "Video conversation" : "New conversation");
  const mismatchMessage = selectedChat?.video_id
    ? `This chat belongs to another video. Load its video to continue chatting.`
    : "This chat is not linked to a video. Select a processed video to continue chatting.";

  return <main className="chat-workspace">
    <ChatSidebar username={username} chats={chats} selectedChatId={selectedChatId} activeView={activeView} videoReady={videoReady} isLoadingChats={isLoadingChats} isOpen={isSidebarOpen} onClose={() => setSidebarOpen(false)} onView={setActiveView} onNewChat={() => void startNewChat()} onSelectChat={(chatId) => void loadChat(chatId)} onDeleteChat={(chatId) => setChatToDelete(chats.find((chat) => chat.chat_id === chatId) || null)} onLogout={onLogout} themeToggle={themeToggle} />
    <div className="chat-main">
      {activeView === "chat" && <ChatWindow chatId={selectedChatId} chatTitle={title} messages={selectedChat?.messages || []} hasVideo={Boolean(videoId)} videoReady={videoReady} isLoading={isLoadingChat} isAsking={isAsking} question={question} chatMismatch={chatVideoMismatch} mismatchMessage={mismatchMessage} onQuestionChange={setQuestion} onSubmit={() => void ask()} onMenu={() => setSidebarOpen(true)} onJump={jumpTo} onAudioRef={updateAnswerAudioRef} onLoadVideoContext={() => selectedChat?.video_id ? void loadActiveVideo(selectedChat.video_id) : undefined} />}
      {activeView === "new-video" && isProcessing && <ProcessingPanel status={status?.status || "queued"} stages={stages} activity={activity} />}
      {activeView === "new-video" && !isProcessing && <NewVideoPanel fileName={file?.name || ""} sourceUrl={sourceUrl} isSubmitting={isSubmitting} onFile={(nextFile) => { setFile(nextFile); setSourceUrl(""); }} onUrlChange={(url) => { setSourceUrl(url); setFile(null); }} onStart={() => void startProcessing()} />}
      {activeView === "summary" && <SummaryPanel summary={summary} videoId={videoId} />}
      {activeView === "video" && <VideoPanel videoId={videoId} videoRef={videoRef} ready={videoReady} />}
      {activeView === "transcript" && <TranscriptPanel transcript={transcript} onJump={jumpTo} ready={videoReady} />}
      {chatToDelete && <div className="delete-backdrop" role="dialog" aria-modal="true" aria-labelledby="delete-chat-title"><div className="delete-dialog"><h2 id="delete-chat-title">Delete this chat?</h2><p>This conversation will be permanently removed.</p><div className="delete-dialog-actions"><button type="button" className="secondary-button" onClick={() => setChatToDelete(null)}>Cancel</button><button type="button" className="danger-button" onClick={() => void handleDeleteChat(chatToDelete.chat_id)}>Delete</button></div></div></div>}
      {notice && <div className={`toast ${notice.kind}`} role="alert">{notice.message}<button type="button" onClick={() => setNotice(null)} aria-label="Dismiss notification">×</button></div>}
    </div>
  </main>;
}