import { FormEvent, useState, type CSSProperties } from "react";
import type { AuthStatus } from "../api/auth";
import { loginAuth, setupAuth } from "../api/auth";
import { FG_ON_ACCENT_TEXT_CLASS, accentFilledStyle } from "../brandAccentUi";

type AuthGateProps = {
  status: AuthStatus;
  shellClass: string;
  surfaceStyle: CSSProperties;
  accentHex: string;
  appLabel: string;
  onAuthenticated: (status: AuthStatus) => void;
};

export function AuthGate(props: AuthGateProps) {
  const needsSetup = props.status.mode === "builtin" && !props.status.configured;
  const forwardBlocked = props.status.mode === "forward_auth" && !props.status.authenticated;

  if (props.status.mode === "disabled") {
    return null;
  }

  if (forwardBlocked) {
    return (
      <div className={props.shellClass} style={props.surfaceStyle}>
        <div className="w-full max-w-md mx-auto px-6 py-10 rounded-2xl border border-[#424753]/40 bg-[#141a24]/90 shadow-xl">
          <p className="text-[12px] font-headline uppercase tracking-widest text-slate-400 mb-2">Security</p>
          <h1 className="text-[28px] font-black font-headline text-white tracking-tight mb-3">Proxy authentication required</h1>
          <p className="text-[15px] text-slate-300 leading-relaxed mb-4">
            {props.appLabel} is in <span className="text-white font-semibold">forward_auth</span> mode. Sign in through your
            reverse proxy (Authelia, Authentik, Traefik, etc.). Trusted proxy CIDRs must include the proxy that reaches this
            container.
          </p>
          {!props.status.forward_auth_ready ? (
            <p className="text-[14px] text-amber-300">
              No trusted proxy CIDRs are configured. Set AUTH_TRUSTED_PROXIES in Settings → Security (or via environment).
            </p>
          ) : !props.status.trusted_proxy ? (
            <p className="text-[14px] text-amber-300">
              This request did not come from a trusted proxy IP, so identity headers were ignored.
            </p>
          ) : (
            <p className="text-[14px] text-amber-300">No identity header was provided by the proxy.</p>
          )}
        </div>
      </div>
    );
  }

  return (
    <div className={props.shellClass} style={props.surfaceStyle}>
      <AuthCredentialForm
        mode={needsSetup ? "setup" : "login"}
        accentHex={props.accentHex}
        appLabel={props.appLabel}
        onAuthenticated={props.onAuthenticated}
      />
    </div>
  );
}

function AuthCredentialForm(props: {
  mode: "setup" | "login";
  accentHex: string;
  appLabel: string;
  onAuthenticated: (status: AuthStatus) => void;
}) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function onSubmit(event: FormEvent) {
    event.preventDefault();
    setError(null);
    if (props.mode === "setup" && password !== confirm) {
      setError("Passwords do not match.");
      return;
    }
    if (password.length < 8) {
      setError("Password must be at least 8 characters.");
      return;
    }
    setBusy(true);
    try {
      const status =
        props.mode === "setup"
          ? await setupAuth(username.trim(), password)
          : await loginAuth(username.trim(), password);
      props.onAuthenticated(status);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <form
      onSubmit={(e) => void onSubmit(e)}
      className="w-full max-w-md mx-auto px-6 py-10 rounded-2xl border border-[#424753]/40 bg-[#141a24]/90 shadow-xl space-y-5"
    >
      <div>
        <p className="text-[12px] font-headline uppercase tracking-widest text-slate-400 mb-2">Security</p>
        <h1 className="text-[28px] font-black font-headline text-white tracking-tight mb-2">
          {props.mode === "setup" ? "Create admin account" : `Sign in to ${props.appLabel}`}
        </h1>
        <p className="text-[15px] text-slate-300 leading-relaxed">
          {props.mode === "setup"
            ? "Existing installs and new setups both start here. Choose a username and password to protect the dashboard and API."
            : "Enter your Placeholdarr admin credentials."}
        </p>
      </div>
      <div>
        <label className="block text-[13px] font-semibold text-slate-400 mb-1">Username</label>
        <input
          className="w-full bg-[#0b111b] border border-[#424753]/40 rounded-lg px-3 py-2.5 text-[15px] text-slate-100"
          value={username}
          onChange={(e) => setUsername(e.target.value)}
          autoComplete="username"
          required
          disabled={busy}
        />
      </div>
      <div>
        <label className="block text-[13px] font-semibold text-slate-400 mb-1">Password</label>
        <input
          type="password"
          className="w-full bg-[#0b111b] border border-[#424753]/40 rounded-lg px-3 py-2.5 text-[15px] text-slate-100"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          autoComplete={props.mode === "setup" ? "new-password" : "current-password"}
          required
          minLength={8}
          disabled={busy}
        />
      </div>
      {props.mode === "setup" ? (
        <div>
          <label className="block text-[13px] font-semibold text-slate-400 mb-1">Confirm password</label>
          <input
            type="password"
            className="w-full bg-[#0b111b] border border-[#424753]/40 rounded-lg px-3 py-2.5 text-[15px] text-slate-100"
            value={confirm}
            onChange={(e) => setConfirm(e.target.value)}
            autoComplete="new-password"
            required
            minLength={8}
            disabled={busy}
          />
        </div>
      ) : null}
      {error ? <p className="text-[14px] text-red-400">{error}</p> : null}
      <button
        type="submit"
        disabled={busy}
        className={`w-full py-2.5 rounded-lg text-[14px] font-headline uppercase tracking-wider ${FG_ON_ACCENT_TEXT_CLASS} disabled:opacity-60`}
        style={accentFilledStyle(props.accentHex)}
      >
        {busy ? "Please wait…" : props.mode === "setup" ? "Create account" : "Sign in"}
      </button>
    </form>
  );
}
