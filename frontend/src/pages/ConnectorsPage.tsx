import { useCallback, useEffect, useState } from "react";
import { api, type ConnectorView, type MyOrg } from "../api/client";

/** Org-scoped connector onboarding: Slack (paste bot token) and Drive
 * (Google OAuth web flow — the browser is redirected through Google, the
 * backend stores the encrypted connector, then returns here with
 * ?drive=connected or ?drive=error&reason=...). Writes are admin-only. */
export default function ConnectorsPage({ me }: { me: MyOrg }) {
  const isAdmin = me.role === "admin";
  const [connectors, setConnectors] = useState<ConnectorView[] | null>(null);
  const [loadError, setLoadError] = useState("");
  const [banner, setBanner] = useState<{ kind: "success" | "error"; text: string } | null>(null);
  const [reloadKey, setReloadKey] = useState(0);

  // Post-callback result params (?drive=connected / ?drive=error&reason=...).
  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    const drive = params.get("drive");
    if (drive === "connected") {
      setBanner({ kind: "success", text: "Google Drive connected. Ingest can use it now." });
    } else if (drive === "error") {
      setBanner({ kind: "error", text: driveErrorText(params.get("reason") || "exchange_failed") });
    }
    if (drive) {
      // Clean the URL so a reload doesn't re-show a stale banner.
      window.history.replaceState({}, "", window.location.pathname);
    }
  }, []);

  useEffect(() => {
    let cancelled = false;
    setConnectors(null);
    api
      .listOrgConnectors(me.id)
      .then((res) => {
        if (!cancelled) setConnectors(res.connectors);
      })
      .catch((e: unknown) => {
        if (!cancelled) setLoadError(e instanceof Error ? e.message : String(e));
      });
    return () => {
      cancelled = true;
    };
  }, [me.id, reloadKey]);

  const driveConnector = connectors?.find((c) => c.source === "drive");
  const slackConnector = connectors?.find((c) => c.source === "slack");

  const onSaved = useCallback(() => {
    setBanner({ kind: "success", text: "Saved." });
    setReloadKey((k) => k + 1);
  }, []);

  return (
    <div>
      {banner && (
        <p style={{ padding: 10, borderRadius: 6, background: banner.kind === "success" ? "#e6f6e9" : "#fdecec", color: banner.kind === "success" ? "#1a7a35" : "#a00" }}>
          {banner.text}
        </p>
      )}
      {loadError && <p style={{ color: "#c00" }}>Could not load connectors: {loadError}</p>}
      {!connectors && !loadError && <p style={{ color: "#666" }}>Loading connectors…</p>}

      <DriveCard
        orgId={me.id}
        connector={driveConnector}
        isAdmin={isAdmin}
        onSaved={onSaved}
        onError={(text) => setBanner({ kind: "error", text })}
      />
      <SlackCard
        orgId={me.id}
        connector={slackConnector}
        isAdmin={isAdmin}
        onSaved={onSaved}
        onError={(text) => setBanner({ kind: "error", text })}
      />
    </div>
  );
}

function driveErrorText(reason: string): string {
  switch (reason) {
    case "no_refresh_token":
      return "Google did not grant a refresh token. Reconnect and make sure to approve the consent screen (it re-issues long-term access).";
    case "expired_state":
      return "The Drive setup session timed out. Start the connection again.";
    case "unknown_state":
      return "The Drive setup session is unknown (it may have been abandoned). Start the connection again.";
    case "consumed_state":
      return "That Drive setup session was already used. Start the connection again — do not reuse old links.";
    case "browser_mismatch":
      return "The Drive setup must be completed in the browser that started it. Start the connection again here.";
    case "revoked":
      return "The admin who started this connection no longer has admin access. Ask a current admin to connect Drive.";
    case "missing_params":
      return "Google Drive did not return the expected data. Start the connection again.";
    case "exchange_failed":
      return "Google rejected the Drive authorization. Please try again.";
    case "invalid_config":
      return "The Drive setup produced an invalid configuration. Please try again.";
    default:
      return `Google Drive connection failed (${reason}). Please try again.`;
  }
}

// --- Drive card ---------------------------------------------------------------

function DriveCard(
  { orgId, connector, isAdmin, onSaved, onError }:
  {
    orgId: string;
    connector: ConnectorView | undefined;
    isAdmin: boolean;
    onSaved: () => void;
    onError: (text: string) => void;
  },
) {
  const [folderId, setFolderId] = useState("");
  const [starting, setStarting] = useState(false);
  const [sync, setSync] = useState<SyncState | null>(null);

  const connected = Boolean(connector?.configured);

  const startOAuth = async () => {
    // The start call MUST go through the authenticated api client (a plain
    // navigation sends no Authorization header and would get a 401). The
    // backend returns {"authorize_url"} plus the browser-binding cookie;
    // navigating to the URL then walks Google consent → the backend's
    // org-less callback → back to /connectors with ?drive=connected|error.
    setStarting(true);
    try {
      const res = await api.startDriveOAuth(orgId, folderId.trim());
      window.location.assign(res.authorize_url);
    } catch (e: unknown) {
      setStarting(false);
      onError(e instanceof Error ? e.message : String(e));
    }
  };

  const runSync = async () => {
    try {
      const job = await api.ingestAsync("drive", connector?.folder_id);
      if (!job.job_id) throw new Error("Ingest queue rejected the job (no job id)");
      setSync({ jobId: job.job_id, status: "queued" });
      pollJob(job.job_id, (s) => setSync(s));
    } catch (e: unknown) {
      onError(e instanceof Error ? e.message : String(e));
    }
  };

  return (
    <section style={card}>
      <h3 style={cardTitle}>Google Drive</h3>
      <StatusLine connector={connector} />
      {isAdmin && (
        <div style={{ marginTop: 12 }}>
          <p style={{ color: "#666", fontSize: 13 }}>
            Folder ID (the part after <code>/folders/</code> in the Drive URL). You will be redirected to Google to approve access, then returned here.
          </p>
          <input
            value={folderId}
            onChange={(e) => setFolderId(e.target.value)}
            placeholder="folder_id"
            style={{ width: "50%", padding: 8, border: "1px solid #ccc", borderRadius: 6, marginRight: 8 }}
          />
          <button
            onClick={startOAuth}
            disabled={!folderId.trim() || starting}
            style={{ padding: 8, background: connected ? "#5a0" : "#007aff", color: "#fff", border: "none", borderRadius: 6 }}
          >
            {starting ? "Redirecting…" : connected ? "Reconnect Google Drive" : "Connect Google Drive"}
          </button>
        </div>
      )}
      {!isAdmin && connected && <p style={{ color: "#888", fontSize: 13 }}>Only org admins can change this connection.</p>}
      {connected && isAdmin && (
        <DisconnectButton orgId={orgId} source="drive" onSaved={onSaved} onError={onError} />
      )}
      {connected && (
        <SyncButton label="Ingest Drive now" state={sync} onRun={runSync} />
      )}
    </section>
  );
}

// --- Slack card ---------------------------------------------------------------

function SlackCard(
  { orgId, connector, isAdmin, onSaved, onError }:
  {
    orgId: string;
    connector: ConnectorView | undefined;
    isAdmin: boolean;
    onSaved: () => void;
    onError: (text: string) => void;
  },
) {
  const [botToken, setBotToken] = useState("");
  const [channels, setChannels] = useState("");
  const [saving, setSaving] = useState(false);
  const [sync, setSync] = useState<SyncState | null>(null);
  const [testing, setTesting] = useState(false);
  const [channelList, setChannelList] = useState<{ id: string; name: string }[] | null>(null);

  // The connector list loads async — seed the channel field when it arrives
  // (and on re-save/reload), but never overwrite while the admin is typing
  // a token-only edit: the field mirrors the SAVED state, not current typing.
  useEffect(() => {
    if (connector?.channel_ids) setChannels(connector.channel_ids.join(", "));
  }, [connector]);

  const connected = Boolean(connector?.configured);

  const save = async () => {
    const ids = channels.split(/[,\n]/).map((s) => s.trim()).filter(Boolean);
    if (!botToken.trim() || ids.length === 0) return;
    setSaving(true);
    try {
      await api.putSlackConnector(orgId, botToken.trim(), ids);
      setBotToken("");
      onSaved();
    } catch (e: unknown) {
      onError(e instanceof Error ? e.message : String(e));
    } finally {
      setSaving(false);
    }
  };

  const runSync = async () => {
    try {
      const job = await api.ingestSlack();
      if (!job.job_id) throw new Error("Ingest queue rejected the job (no job id)");
      setSync({ jobId: job.job_id, status: "queued" });
      pollJob(job.job_id, (s) => setSync(s));
    } catch (e: unknown) {
      onError(e instanceof Error ? e.message : String(e));
    }
  };

  const testAndList = async () => {
    // Validates the pasted bot token against Slack (auth.test +
    // conversations.list) and lists the channels so the admin can click
    // instead of typing IDs. The backend stores nothing, never logs or
    // echoes the token.
    setTesting(true);
    try {
      const res = await api.testSlackConnector(orgId, botToken.trim());
      setChannelList(res.channels);
      if (res.channels.length === 0) onError("Token is valid but the bot is not in any channels yet — invite it to channels in Slack.");
    } catch (e: unknown) {
      setChannelList(null);
      onError(e instanceof Error ? e.message : String(e));
    } finally {
      setTesting(false);
    }
  };

  const toggleChannel = (id: string) => {
    const ids = currentChannelIds(channels);
    const next = ids.includes(id) ? ids.filter((c) => c !== id) : [...ids, id];
    setChannels(next.join(", "));
  };

  return (
    <section style={card}>
      <h3 style={cardTitle}>Slack</h3>
      <StatusLine connector={connector} />
      {isAdmin && (
        <div style={{ marginTop: 12 }}>
          <p style={{ color: "#666", fontSize: 13 }}>
            Bot token (xoxb-…, from your Slack app's "OAuth & Permissions" page — it is stored encrypted)
            and channel IDs to ingest.
          </p>
          <input
            type="password"
            value={botToken}
            onChange={(e) => setBotToken(e.target.value)}
            placeholder="xoxb-…"
            style={{ width: "50%", padding: 8, border: "1px solid #ccc", borderRadius: 6, marginRight: 8, marginBottom: 8 }}
          />
          <input
            value={channels}
            onChange={(e) => setChannels(e.target.value)}
            placeholder="C123456, C789012"
            style={{ width: "50%", padding: 8, border: "1px solid #ccc", borderRadius: 6, marginBottom: 8 }}
          />
          <br />
          <button
            onClick={testAndList}
            disabled={!botToken.trim() || testing}
            style={{ padding: 8, background: "#eee", border: "1px solid #ddd", borderRadius: 6, marginBottom: 8, marginRight: 8 }}
          >
            {testing ? "Talking to Slack…" : "Test and list channels"}
          </button>
          <button
            onClick={save}
            disabled={!botToken.trim() || saving || currentChannelIds(channels).length === 0}
            style={{ padding: 8, background: "#007aff", color: "#fff", border: "none", borderRadius: 6, marginBottom: 8 }}
          >
            {saving ? "Saving…" : connected ? "Update Slack connection" : "Connect Slack"}
          </button>
          {channelList && (
            <div style={{ marginTop: 8 }}>
              <p style={{ color: "#666", fontSize: 13 }}>Click channels to include; selected ones appear above:</p>
              <div style={{ display: "flex", flexWrap: "wrap", gap: 6 }}>
                {channelList.map((c) => {
                  const selected = currentChannelIds(channels).includes(c.id);
                  return (
                    <button
                      key={c.id}
                      onClick={() => toggleChannel(c.id)}
                      title={`${c.id} — click to ${selected ? "remove" : "add"}`}
                      style={{
                        padding: "4px 10px",
                        fontSize: 13,
                        borderRadius: 14,
                        border: "1px solid",
                        borderColor: selected ? "#007aff" : "#ccc",
                        background: selected ? "#e5f1ff" : "#fff",
                        color: "#111",
                      }}
                    >
                      #{c.name}
                    </button>
                  );
                })}
              </div>
            </div>
          )}
        </div>
      )}
      {!isAdmin && connected && <p style={{ color: "#888", fontSize: 13 }}>Only org admins can change this connection.</p>}
      {connected && isAdmin && (
        <DisconnectButton orgId={orgId} source="slack" onSaved={onSaved} onError={onError} />
      )}
      {connected && (
        <SyncButton label="Ingest Slack now" state={sync} onRun={runSync} />
      )}
    </section>
  );
}

function DisconnectButton(
  { orgId, source, onSaved, onError }:
  { orgId: string; source: "slack" | "drive"; onSaved: () => void; onError: (text: string) => void },
) {
  const [busy, setBusy] = useState(false);
  const disconnect = async () => {
    // After deletion the org is UNCONFIGURED (ingest stops) — nothing falls
    // back to global credentials silently (server-side enforced).
    setBusy(true);
    try {
      await api.deleteOrgConnector(orgId, source);
      onSaved();
    } catch (e: unknown) {
      onError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };
  return (
    <div style={{ marginTop: 10 }}>
      <button
        onClick={disconnect}
        disabled={busy}
        style={{ padding: "4px 10px", background: "#f8d7da", color: "#900", border: "1px solid #f1b0b7", borderRadius: 6, fontSize: 13 }}
      >
        {busy ? "Disconnecting…" : `Disconnect ${source}`}
      </button>
    </div>
  );
}

// --- shared bits ----------------------------------------------------------------

/** Parse the channel-ID field into a clean list (comma/newline separated). */
function currentChannelIds(channels: string): string[] {
  return channels.split(/[,\n]/).map((s) => s.trim()).filter(Boolean);
}

interface SyncState {
  jobId: string;
  status: string;
  result?: Record<string, unknown> | null;
}

function SyncButton(
  { label, state, onRun }:
  { label: string; state: SyncState | null; onRun: () => void },
) {
  return (
    <div style={{ marginTop: 12, color: "#666", fontSize: 13 }}>
      <button onClick={onRun} style={{ padding: "6px 10px", background: "#eee", border: "1px solid #ddd", borderRadius: 6 }}>
        {label}
      </button>
      {state && (
        <span style={{ marginLeft: 10 }}>
          {state.status === "completed" || state.status === "success"
            ? `Done: ${JSON.stringify(state.result ?? {})}`.slice(0, 140)
            : state.status === "failed" || state.status === "error"
              ? "Ingest failed — check the worker logs."
              : `Job ${state.jobId}: ${state.status}…`}
        </span>
      )}
    </div>
  );
}

function StatusLine({ connector }: { connector: ConnectorView | undefined }) {
  if (!connector?.configured) {
    return <p style={{ color: "#a60" }}>Not connected</p>;
  }
  const detail =
    connector.source === "slack"
      ? (connector.channel_ids || []).join(", ")
      : `folder ${connector.folder_id || "?"}`;
  return (
    <p style={{ color: "#1a7a35" }}>
      Connected ({detail})
      {connector.updated_at ? ` · last updated ${new Date(connector.updated_at).toLocaleString()}` : ""}
    </p>
  );
}

const card: React.CSSProperties = {
  border: "1px solid #ddd",
  borderRadius: 8,
  padding: 16,
  marginBottom: 16,
};

const cardTitle: React.CSSProperties = { marginTop: 0, marginBottom: 8 };

// Poll GET /api/ingest/status/{job_id} (all statuses except
// queued/running are terminal) every 2 seconds until done.
function pollJob(jobId: string, onStatus: (s: SyncState) => void): void {
  let attempts = 0;
  const tick = async () => {
    attempts += 1;
    try {
      const job = await api.getJob(jobId);
      onStatus({ jobId, status: job.status, result: job.result });
      if (["queued", "running", "pending"].includes(job.status) && attempts < 120) {
        setTimeout(tick, 2000);
      }
    } catch {
      onStatus({ jobId, status: "failed" });
    }
  };
  void tick();
}
