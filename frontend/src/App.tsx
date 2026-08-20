import { useState } from "react";
import ChatPage from "./pages/ChatPage";
import SkillsPage from "./pages/SkillsPage";
import { api } from "./api/client";

type Page = "chat" | "skills";

export default function App() {
  const [page, setPage] = useState<Page>("chat");
  const [token, setToken] = useState(api.getToken());

  if (!token) {
    return <TokenPrompt onSet={(t) => { api.setToken(t); setToken(t); }} />;
  }

  return (
    <div style={{ maxWidth: 960, margin: "0 auto", padding: 20 }}>
      <nav style={{ marginBottom: 20, display: "flex", gap: 16, alignItems: "center" }}>
        <strong>Company Brain</strong>
        <button onClick={() => setPage("chat")} style={navBtn(page === "chat")}>Chat</button>
        <button onClick={() => setPage("skills")} style={navBtn(page === "skills")}>Skills</button>
        <button
          onClick={() => { api.clearToken(); setToken(""); }}
          style={{ marginLeft: "auto", background: "#eee", border: "none", padding: "6px 12px", borderRadius: 6 }}
        >
          Sign out
        </button>
      </nav>
      {page === "chat" ? <ChatPage /> : <SkillsPage />}
    </div>
  );
}

function navBtn(active: boolean): React.CSSProperties {
  return {
    background: active ? "#007aff" : "#eee",
    color: active ? "#fff" : "#333",
    border: "none",
    padding: "6px 14px",
    borderRadius: 6,
  };
}

function TokenPrompt({ onSet }: { onSet: (t: string) => void }) {
  const [value, setValue] = useState("");
  return (
    <div style={{ maxWidth: 400, margin: "100px auto", textAlign: "center" }}>
      <h2>Sign in</h2>
      <p style={{ color: "#666", marginBottom: 16 }}>Enter your JWT token</p>
      <input
        type="password"
        value={value}
        onChange={(e) => setValue(e.target.value)}
        placeholder="Bearer token"
        style={{ width: "100%", padding: 10, border: "1px solid #ccc", borderRadius: 6, marginBottom: 12 }}
      />
      <button
        onClick={() => value && onSet(value)}
        style={{ width: "100%", padding: 10, background: "#007aff", color: "#fff", border: "none", borderRadius: 6 }}
      >
        Continue
      </button>
    </div>
  );
}