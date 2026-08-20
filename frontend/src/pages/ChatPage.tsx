import { useEffect, useRef, useState } from "react";
import { api, ChatResponse, Source, ToolProposal } from "../api/client";

interface Message {
  id: number;
  role: "user" | "assistant";
  text?: string;
  sources?: Source[];
  toolProposal?: { tool_id: string; tool_name: string; arguments: Record<string, unknown>; description: string };
  toolResult?: { tool_id: string; success: boolean; result: Record<string, unknown> };
  streaming?: boolean;
}

export default function ChatPage() {
  const [messages, setMessages] = useState<Message[]>([]);
  const [input, setInput] = useState("");
  const [conversationId, setConversationId] = useState<string | null>(null);
  const [pendingTool, setPendingTool] = useState<ToolProposal | null>(null);
  const [busy, setBusy] = useState(false);
  const scrollRef = useRef<HTMLDivElement>(null);
  const wsRef = useRef<WebSocket | null>(null);
  const nextId = useRef(1);

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight });
  }, [messages]);

  // Keep a single authenticated WS connection for the session.
  useEffect(() => {
    if (wsRef.current) return;
    wsRef.current = api.openChatStream({
      onSources: (sources, convId) => {
        setConversationId(convId);
        setMessages((prev) => {
          if (prev.length === 0) return prev;
          const last = prev[prev.length - 1];
          return prev.map((m) => (m.id === last.id ? { ...m, sources } : m));
        });
      },
      onToken: (content) => {
        setMessages((prev) => {
          if (prev.length === 0) return prev;
          const last = prev[prev.length - 1];
          return prev.map((m) =>
            m.id === last.id ? { ...m, text: (m.text || "") + content } : m,
          );
        });
      },
      onToolProposal: (evt) => {
        setConversationId(evt.conversation_id);
        setPendingTool(evt);
        setMessages((prev) => {
          if (prev.length === 0) return prev;
          const last = prev[prev.length - 1];
          return prev.map((m) =>
            m.id === last.id
              ? {
                  ...m,
                  streaming: false,
                  toolProposal: {
                    tool_id: evt.tool_id,
                    tool_name: evt.tool_name,
                    arguments: evt.arguments,
                    description: evt.description,
                  },
                }
              : m,
          );
        });
        setBusy(false);
      },
      onToolResult: (evt) => {
        setMessages((prev) => {
          if (prev.length === 0) return prev;
          const last = prev[prev.length - 1];
          return prev.map((m) =>
            m.id === last.id
              ? { ...m, toolResult: { tool_id: evt.tool_id, success: evt.success, result: evt.result } }
              : m,
          );
        });
      },
      onMessage: (resp: ChatResponse) => {
        if (resp.conversation_id) setConversationId(resp.conversation_id);
        setMessages((prev) => {
          if (prev.length === 0) return prev;
          const last = prev[prev.length - 1];
          return prev.map((m) =>
            m.id === last.id
              ? { ...m, text: resp.answer || m.text || "", sources: resp.sources, streaming: false }
              : m,
          );
        });
        setBusy(false);
      },
      onError: (detail) => {
        setMessages((prev) => {
          if (prev.length === 0) return prev;
          const last = prev[prev.length - 1];
          return prev.map((m) => (m.id === last.id ? { ...m, text: `Error: ${detail}`, streaming: false } : m));
        });
        setBusy(false);
      },
    });
    return () => {
      wsRef.current?.close();
      wsRef.current = null;
    };
  }, []);

  function addMessage(m: Omit<Message, "id">): number {
    const id = nextId.current++;
    setMessages((prev) => [...prev, { ...m, id }]);
    return id;
  }

  function send() {
    const text = input.trim();
    if (!text || busy || pendingTool) return;
    setInput("");
    setBusy(true);
    addMessage({ role: "user", text });
    addMessage({ role: "assistant", text: "", streaming: true });
    if (wsRef.current) api.sendChat(wsRef.current, text, conversationId);
  }

  function confirm(approved: boolean) {
    if (!conversationId || !pendingTool || !wsRef.current) return;
    setPendingTool(null);
    setBusy(true);
    api.confirmTool(wsRef.current, conversationId, pendingTool.tool_id, approved);
    // A new streaming assistant bubble will be produced by onToken events.
    addMessage({ role: "assistant", text: "", streaming: true });
  }

  return (
    <div>
      <div ref={scrollRef} style={chatBoxStyle}>
        {messages.map((m) => (
          <MessageBubble key={m.id} m={m} onConfirm={confirm} disabled={busy || !pendingTool} />
        ))}
      </div>
      <div style={{ display: "flex", gap: 8, marginTop: 12 }}>
        <input
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && send()}
          placeholder="Ask a question about your documents..."
          style={inputStyle}
          autoFocus
        />
        <button onClick={send} disabled={busy || !!pendingTool} style={sendBtnStyle}>
          {busy ? "..." : "Send"}
        </button>
      </div>
    </div>
  );
}

function MessageBubble({ m, onConfirm, disabled }: { m: Message; onConfirm: (a: boolean) => void; disabled: boolean }) {
  return (
    <div style={{ marginBottom: 16, textAlign: m.role === "user" ? "right" : "left" }}>
      {m.text && (
        <div style={bubbleStyle(m.role)}>
          {m.text}
          {m.streaming && <span style={{ opacity: 0.5 }}>▌</span>}
        </div>
      )}
      {m.toolProposal && (
        <div style={confirmBoxStyle}>
          <div style={{ marginBottom: 10 }}>
            <strong>Proposed action:</strong> {m.toolProposal.description}
            <div style={{ fontSize: "0.85em", color: "#666", marginTop: 4 }}>
              {m.toolProposal.tool_name}({JSON.stringify(m.toolProposal.arguments)})
            </div>
          </div>
          <div style={{ display: "flex", gap: 8 }}>
            <button onClick={() => onConfirm(true)} disabled={disabled} style={confirmBtnStyle}>Confirm</button>
            <button onClick={() => onConfirm(false)} disabled={disabled} style={declineBtnStyle}>Decline</button>
          </div>
        </div>
      )}
      {m.toolResult && (
        <div style={{ fontSize: "0.85em", color: m.toolResult.success ? "#155724" : "#721c24", marginTop: 4 }}>
          Tool {m.toolResult.success ? "succeeded" : "failed"}: {JSON.stringify(m.toolResult.result)}
        </div>
      )}
      {m.sources && m.sources.length > 0 && (
        <div style={{ fontSize: "0.85em", color: "#666", marginTop: 4 }}>
          Sources: {m.sources.map((s, i) => (
            <span key={i}>
              <a href={s.url || "#"} target="_blank" rel="noreferrer">
                {s.name}{s.type ? ` (${s.type})` : ""}
              </a>
              {i < m.sources!.length - 1 ? ", " : ""}
            </span>
          ))}
        </div>
      )}
    </div>
  );
}

const chatBoxStyle: React.CSSProperties = {
  background: "#fff",
  borderRadius: 8,
  boxShadow: "0 2px 8px rgba(0,0,0,0.1)",
  padding: 16,
  minHeight: 400,
  maxHeight: 600,
  overflowY: "auto",
};

function bubbleStyle(role: string): React.CSSProperties {
  return {
    display: "inline-block",
    padding: "10px 14px",
    borderRadius: 18,
    maxWidth: "80%",
    textAlign: "left",
    whiteSpace: "pre-wrap",
    background: role === "user" ? "#007aff" : "#e9e9eb",
    color: role === "user" ? "#fff" : "#000",
  };
}

const confirmBoxStyle: React.CSSProperties = {
  background: "#fff8e1",
  border: "1px solid #ffe082",
  borderRadius: 8,
  padding: "12px 16px",
  marginTop: 8,
};

const confirmBtnStyle: React.CSSProperties = { background: "#34c759", color: "#fff", border: "none", padding: "6px 18px", borderRadius: 6 };
const declineBtnStyle: React.CSSProperties = { background: "#ff3b30", color: "#fff", border: "none", padding: "6px 18px", borderRadius: 6 };
const inputStyle: React.CSSProperties = { flex: 1, padding: "10px 14px", border: "1px solid #ccc", borderRadius: 20, fontSize: "1em", outline: "none" };
const sendBtnStyle: React.CSSProperties = { padding: "10px 20px", background: "#007aff", color: "#fff", border: "none", borderRadius: 20, fontSize: "1em" };