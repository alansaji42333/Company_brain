import { useEffect, useState } from "react";
import { api, SkillDetail, SkillSummary } from "../api/client";

export default function SkillsPage() {
  const [skills, setSkills] = useState<SkillSummary[] | null>(null);
  const [selected, setSelected] = useState<SkillDetail | null>(null);
  const [editContent, setEditContent] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  async function loadList() {
    try {
      const data = await api.listSkills();
      setSkills(data.skills);
    } catch (e) {
      setError((e as Error).message);
    }
  }

  useEffect(() => { loadList(); }, []);

  async function openSkill(id: string) {
    try {
      const s = await api.getSkill(id);
      setSelected(s);
      setEditContent(s.content || "");
    } catch (e) {
      setError((e as Error).message);
    }
  }

  async function save() {
    if (!selected) return;
    setBusy(true);
    try {
      await api.updateSkill(selected.id, editContent);
      alert("Saved");
    } catch (e) {
      alert("Error: " + (e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  async function approve() {
    if (!selected || !confirm("Approve this skill? It will be embedded for retrieval.")) return;
    await api.approveSkill(selected.id);
    setSelected(null);
    loadList();
  }

  async function reject() {
    if (!selected || !confirm("Reject this skill? It will be removed from retrieval.")) return;
    await api.rejectSkill(selected.id);
    setSelected(null);
    loadList();
  }

  async function runSynthesis() {
    setBusy(true);
    try {
      const r = await api.synthesize();
      alert(`Synthesis: ${r.new_skills} new, ${r.skipped_duplicates} skipped`);
      loadList();
    } catch (e) {
      alert("Error: " + (e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  if (selected) {
    return (
      <div style={detailStyle}>
        <button onClick={() => setSelected(null)} style={backBtnStyle}>← Back</button>
        <h2>{selected.title}</h2>
        <div style={metaStyle}>
          ID: {selected.id} | Status: <span className={`status ${selected.status}`}>{selected.status}</span> |<br />
          Created: {selected.created_at ? new Date(selected.created_at).toLocaleString() : ""}
        </div>
        <textarea
          value={editContent}
          onChange={(e) => setEditContent(e.target.value)}
          style={textareaStyle}
        />
        <div style={{ display: "flex", gap: 8, marginTop: 12 }}>
          <button onClick={save} disabled={busy} style={saveBtnStyle}>Save</button>
          <button onClick={approve} disabled={busy} style={approveBtnStyle}>Approve</button>
          <button onClick={reject} disabled={busy} style={rejectBtnStyle}>Reject</button>
        </div>
      </div>
    );
  }

  return (
    <div>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 12 }}>
        <h2>Skill Documents</h2>
        <button onClick={runSynthesis} disabled={busy} style={saveBtnStyle}>Run synthesis</button>
      </div>
      {error && <div style={{ color: "#d32f2f", marginBottom: 12 }}>{error}</div>}
      {skills === null ? (
        <p>Loading...</p>
      ) : skills.length === 0 ? (
        <div style={emptyStyle}>No skill documents yet. Run synthesis to generate them.</div>
      ) : (
        <table style={tableStyle}>
          <thead>
            <tr>
              <th style={thStyle}>Title</th>
              <th style={thStyle}>Status</th>
              <th style={thStyle}>Created</th>
            </tr>
          </thead>
          <tbody>
            {skills.map((s) => (
              <tr key={s.id} onClick={() => openSkill(s.id)} style={{ cursor: "pointer" }}>
                <td style={tdStyle}>{s.title}</td>
                <td style={tdStyle}><span className={`status ${s.status}`}>{s.status}</span></td>
                <td style={tdStyle}>{s.created_at ? new Date(s.created_at).toLocaleDateString() : ""}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      <style>{`
        .status { display: inline-block; padding: 2px 8px; border-radius: 10px; font-size: 0.85em; }
        .status.draft { background: #fff3cd; color: #856404; }
        .status.approved { background: #d4edda; color: #155724; }
        .status.rejected { background: #f8d7da; color: #721c24; }
      `}</style>
    </div>
  );
}

const detailStyle: React.CSSProperties = { background: "#fff", borderRadius: 8, boxShadow: "0 2px 8px rgba(0,0,0,0.1)", padding: 20 };
const backBtnStyle: React.CSSProperties = { background: "#6c757d", color: "#fff", border: "none", padding: "8px 16px", borderRadius: 6, marginBottom: 12 };
const metaStyle: React.CSSProperties = { color: "#666", fontSize: "0.9em", marginBottom: 16 };
const textareaStyle: React.CSSProperties = { width: "100%", minHeight: 300, padding: 12, fontFamily: "'SF Mono', Monaco, monospace", fontSize: "0.9em", border: "1px solid #ccc", borderRadius: 6, resize: "vertical" };
const saveBtnStyle: React.CSSProperties = { padding: "8px 20px", border: "none", borderRadius: 6, background: "#007aff", color: "#fff" };
const approveBtnStyle: React.CSSProperties = { padding: "8px 20px", border: "none", borderRadius: 6, background: "#28a745", color: "#fff" };
const rejectBtnStyle: React.CSSProperties = { padding: "8px 20px", border: "none", borderRadius: 6, background: "#dc3545", color: "#fff" };
const emptyStyle: React.CSSProperties = { textAlign: "center", color: "#888", padding: 40, background: "#fff", borderRadius: 8 };
const tableStyle: React.CSSProperties = { width: "100%", background: "#fff", borderRadius: 8, boxShadow: "0 2px 8px rgba(0,0,0,0.1)", borderCollapse: "collapse" };
const thStyle: React.CSSProperties = { padding: "12px 16px", textAlign: "left", borderBottom: "1px solid #eee", background: "#fafafa" };
const tdStyle: React.CSSProperties = { padding: "12px 16px", borderBottom: "1px solid #eee" };