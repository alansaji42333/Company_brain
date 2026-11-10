import { useCallback, useEffect, useState } from "react";
import { useAuth0 } from "@auth0/auth0-react";
import ChatPage from "./pages/ChatPage";
import SkillsPage from "./pages/SkillsPage";
import ConnectorsPage from "./pages/ConnectorsPage";
import {
  api,
  getActiveOrgId,
  registerAuth,
  setActiveOrgId,
  type MyOrg,
  type MyOrgSummary,
} from "./api/client";
import { AUTH0_CONFIGURED } from "./auth/config";

type Page = "chat" | "skills" | "connectors";

export default function App() {
  // Stable mode split: the Auth0 tree and the dev (paste-token) tree never
  // mix — useAuth0 requires its provider, so it must only live inside one.
  if (AUTH0_CONFIGURED) {
    return <Auth0App />;
  }
  return <DevApp />;
}

// --- Auth0 Universal Login (production) --------------------------------------

function Auth0App() {
  const { isLoading, isAuthenticated, error, loginWithRedirect, logout, getAccessTokenSilently } =
    useAuth0();
  const [me, setMe] = useState<MyOrg | null>(null);
  const [orgs, setOrgs] = useState<MyOrgSummary[]>([]);
  const [bootError, setBootError] = useState("");

  // Fresh access token for every request (silent refresh; `force` bypasses
  // the SDK's token cache — used by the client's 401-retry path).
  const tokenGetter = useCallback(
    async (force?: boolean) => {
      const token = await getAccessTokenSilently(
        force ? { cacheMode: "off" } : { cacheMode: "on" },
      );
      if (!token) throw new Error("No access token available — sign in again.");
      return token;
    },
    [getAccessTokenSilently],
  );

  // Register the auth plumbing + load the org session once signed in.
  useEffect(() => {
    if (!isAuthenticated) return;

    registerAuth({
      tokenGetter,
      // 401 after one silent retry: the session is unusable — re-run
      // Universal Login to get a fresh one.
      onUnauthorized: () => {
        void loginWithRedirect({ authorizationParams: { prompt: "login" } });
      },
    });

    let cancelled = false;
    const loadSession = async () => {
      const { orgs: memberships } = await api.listMyOrgs();
      if (cancelled) return;
      // Restore the stored org switch (hint only — server verifies), or
      // start on the default (personal) org.
      const stored = getActiveOrgId();
      if (stored && !memberships.some((o) => o.id === stored)) setActiveOrgId(null);
      const myOrg = await api.getMyOrg();
      if (cancelled) return;
      setOrgs(memberships);
      setMe(myOrg);
    };

    void loadSession().catch((e: unknown) => {
      if (!cancelled) setBootError(e instanceof Error ? e.message : String(e));
    });
    return () => {
      cancelled = true;
    };
  }, [isAuthenticated, tokenGetter, loginWithRedirect]);

  if (isLoading) {
    return <Splash label="Checking sign-in…" />;
  }
  if (error) {
    return <SignInError message={error.message} onRetry={() => void loginWithRedirect()} />;
  }
  if (!isAuthenticated) {
    return <SignInScreen onSignIn={() => void loginWithRedirect()} />;
  }
  if (bootError) {
    return <SignInError message={bootError} onRetry={() => window.location.reload()} />;
  }
  if (!me) {
    return <Splash label="Loading your workspace…" />;
  }

  const switchOrg = (orgId: string) => {
    if (orgId === me.id) return;
    setActiveOrgId(orgId);
    void (async () => {
      try {
        setMe(await api.getMyOrg());
      } catch (e: unknown) {
        setBootError(e instanceof Error ? e.message : String(e));
      }
    })();
  };

  const signOut = () =>
    logout({ logoutParams: { returnTo: window.location.origin } });

  return (
    <Shell
      me={me}
      orgs={orgs}
      onSwitchOrg={switchOrg}
      onSignOut={signOut}
      activeOrgKey={me.id} // remount pages so per-org data reloads
    />
  );
}

// --- Dev mode (no VITE_AUTH0_* config) ---------------------------------------

function DevApp() {
  const [token, setToken] = useState(api.getToken());
  const [authFailed, setAuthFailed] = useState(false);

  useEffect(() => {
    if (!token) return;
    registerAuth({
      tokenGetter: async () => api.getToken(),
      // 401 after retry → back to the paste-token screen.
      onUnauthorized: () => {
        api.clearToken();
        setToken("");
        setAuthFailed(true);
      },
    });
  }, [token]);

  if (!token) {
    return <DevSignIn authFailed={authFailed} onSignIn={(value) => {
      api.setToken(value);
      setToken(value);
      setAuthFailed(false);
    }} />;
  }

  return <DevShell onSignOut={() => { api.clearToken(); setToken(""); }} />;
}

function DevShell({ onSignOut }: { onSignOut: () => void }) {
  return (
    <Shell
      me={null}
      orgs={[]} // org context requires sign-in; dev tokens resolve implicitly
      onSignOut={onSignOut}
    />
  );
}

// --- shared shell ------------------------------------------------------------

function Shell({
  me,
  orgs,
  onSwitchOrg,
  onSignOut,
  activeOrgKey,
}: {
  me: MyOrg | null;
  orgs: MyOrgSummary[];
  onSwitchOrg?: (orgId: string) => void;
  onSignOut: () => void;
  activeOrgKey?: string;
}) {
  const [page, setPage] = useState<Page>("chat");
  return (
    <div style={{ maxWidth: 960, margin: "0 auto", padding: 20 }}>
      <nav style={{ marginBottom: 20, display: "flex", gap: 16, alignItems: "center" }}>
        <strong>Company Brain</strong>
        <button onClick={() => setPage("chat")} style={navBtn(page === "chat")}>Chat</button>
        <button onClick={() => setPage("skills")} style={navBtn(page === "skills")}>Skills</button>
        {me && (
          <button onClick={() => setPage("connectors")} style={navBtn(page === "connectors")}>Connectors</button>
        )}
        {me && (
          <span style={{ color: "#666", fontSize: 14 }}>
            {me.name} · {me.role}
          </span>
        )}
        {me && orgs.length > 1 && onSwitchOrg && (
          <select
            value={me.id}
            onChange={(e) => onSwitchOrg(e.target.value)}
            style={{ padding: 6, borderRadius: 6, border: "1px solid #ccc" }}
            title="Switch organization"
          >
            {orgs.map((o) => (
              <option key={o.id} value={o.id}>
                {o.name}
              </option>
            ))}
          </select>
        )}
        <button
          onClick={onSignOut}
          style={{ marginLeft: "auto", background: "#eee", border: "none", padding: "6px 12px", borderRadius: 6 }}
        >
          Sign out
        </button>
      </nav>
      <div key={activeOrgKey}>
        {page === "chat" ? (
          <ChatPage />
        ) : page === "connectors" && me ? (
          <ConnectorsPage me={me} />
        ) : (
          <SkillsPage />
        )}
      </div>
    </div>
  );
}

// --- sign-in screens -----------------------------------------------------------

/** Production sign-in: Auth0 Universal Login, one button. */
function SignInScreen({ onSignIn }: { onSignIn: () => void }) {
  return (
    <div style={{ maxWidth: 400, margin: "100px auto", textAlign: "center" }}>
      <h2>Company Brain</h2>
      <p style={{ color: "#666", marginBottom: 20 }}>
        Sign in with your work account to reach your organization's workspace.
      </p>
      <button
        onClick={onSignIn}
        style={{ width: "100%", padding: 12, background: "#007aff", color: "#fff", border: "none", borderRadius: 6 }}
      >
        Sign in
      </button>
    </div>
  );
}

/** Dev fallback (no VITE_AUTH0_* config): paste a backend-issued JWT. */
function DevSignIn(
  { onSignIn, authFailed }: { onSignIn: (value: string) => void; authFailed?: boolean },
) {
  const [value, setValue] = useState("");
  return (
    <div style={{ maxWidth: 400, margin: "100px auto", textAlign: "center" }}>
      <h2>Company Brain</h2>
      <p style={{ color: "#888", marginTop: -8 }}>Dev mode</p>
      <p style={{ color: "#666", marginBottom: 16 }}>Paste a JWT token to continue</p>
      {authFailed && <p style={{ color: "#c00" }}>That token was rejected — sign in again.</p>}
      <input
        type="password"
        value={value}
        onChange={(e) => setValue(e.target.value)}
        placeholder="Bearer token"
        style={{ width: "100%", padding: 10, border: "1px solid #ccc", borderRadius: 6, marginBottom: 12 }}
      />
      <button
        onClick={() => value && onSignIn(value)}
        disabled={!value}
        style={{ width: "100%", padding: 10, background: "#007aff", color: "#fff", border: "none", borderRadius: 6, opacity: value ? 1 : 0.6 }}
      >
        Continue
      </button>
    </div>
  );
}

function Splash({ label }: { label: string }) {
  return (
    <div style={{ maxWidth: 400, margin: "100px auto", textAlign: "center", color: "#666" }}>
      <h2>Company Brain</h2>
      <p>{label}</p>
    </div>
  );
}

function SignInError({ message, onRetry }: { message: string; onRetry: () => void }) {
  return (
    <div style={{ maxWidth: 400, margin: "100px auto", textAlign: "center" }}>
      <h2>Company Brain</h2>
      <p style={{ color: "#c00" }}>Sign-in failed: {message}</p>
      <button
        onClick={onRetry}
        style={{ padding: 10, background: "#007aff", color: "#fff", border: "none", borderRadius: 6 }}
      >
        Try again
      </button>
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
