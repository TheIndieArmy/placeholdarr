import { useEffect, useRef, useState } from "react";
import {
  postPlexOauthComplete,
  postPlexOauthPin,
  postPlexOauthPoll,
  postPlexOauthServers,
  type PlexOauthServer,
} from "../api/plex";
import { BrandSelect } from "../BrandSelect";

type Mode = "oauth" | "manual";
type OauthPhase = "idle" | "waiting" | "pick_server" | "saving" | "done" | "error";

export const PLEX_OAUTH_META_KEYS = [
  "PLEX_AUTH_METHOD",
  "PLEX_ACCOUNT_USERNAME",
  "PLEX_SERVER_NAME",
  "PLEX_SERVER_MACHINE_ID",
] as const;

export function plexConnectedSummary(values: Record<string, unknown>): {
  method: string;
  username: string;
  serverName: string;
  url: string;
} {
  return {
    method: String(values.PLEX_AUTH_METHOD ?? "").trim().toLowerCase(),
    username: String(values.PLEX_ACCOUNT_USERNAME ?? "").trim(),
    serverName: String(values.PLEX_SERVER_NAME ?? "").trim(),
    url: String(values.PLEX_URL ?? "").trim(),
  };
}

export function PlexMediaConnect(props: {
  values: Record<string, unknown>;
  accentHex: string;
  focusClass: string;
  fields: Array<{ key: string; label: string; description?: string; type: string; secret?: boolean; has_saved_value?: boolean }>;
  onFieldChange: (key: string, value: unknown) => void;
  onConnected: () => void | Promise<void>;
}) {
  const summary = plexConnectedSummary(props.values);
  const alreadyOauth = summary.method === "oauth" && Boolean(summary.url);
  const [mode, setMode] = useState<Mode>(alreadyOauth || !summary.url ? "oauth" : "manual");
  const [phase, setPhase] = useState<OauthPhase>(alreadyOauth ? "done" : "idle");
  const [message, setMessage] = useState<string | null>(null);
  const [authUrl, setAuthUrl] = useState<string | null>(null);
  const [servers, setServers] = useState<PlexOauthServer[]>([]);
  const [selectedMachine, setSelectedMachine] = useState("");
  const [pendingToken, setPendingToken] = useState<string | null>(null);
  const [pendingClientId, setPendingClientId] = useState<string | null>(null);
  const [pendingUsername, setPendingUsername] = useState<string | null>(null);
  const pollRef = useRef<number | null>(null);
  const pinRef = useRef<{ id: number; client_id: string } | null>(null);

  useEffect(() => {
    return () => {
      if (pollRef.current != null) window.clearInterval(pollRef.current);
    };
  }, []);

  const stopPoll = () => {
    if (pollRef.current != null) {
      window.clearInterval(pollRef.current);
      pollRef.current = null;
    }
  };

  const urlField = props.fields.find((f) => f.key === "PLEX_URL");
  const tokenField = props.fields.find((f) => f.key === "PLEX_TOKEN");
  const otherFields = props.fields.filter((f) => f.key !== "PLEX_URL" && f.key !== "PLEX_TOKEN");

  const startOauth = async () => {
    stopPoll();
    setMode("oauth");
    setPhase("waiting");
    setMessage("Opening Plex sign-in…");
    setAuthUrl(null);
    setServers([]);
    setSelectedMachine("");
    setPendingToken(null);
    try {
      const pin = await postPlexOauthPin();
      pinRef.current = { id: pin.id, client_id: pin.client_id };
      setPendingClientId(pin.client_id);
      setAuthUrl(pin.auth_url);
      setMessage(`Code ${pin.code}. Finish signing in with Plex in the opened window.`);
      window.open(pin.auth_url, "_blank", "noopener,noreferrer");
      pollRef.current = window.setInterval(() => {
        void (async () => {
          const cur = pinRef.current;
          if (!cur) return;
          try {
            const result = await postPlexOauthPoll({ pin_id: cur.id, client_id: cur.client_id });
            if (!result.authorized || !result.auth_token) return;
            stopPoll();
            setPendingToken(result.auth_token);
            setPendingClientId(result.client_id || cur.client_id);
            setPendingUsername(result.username || null);
            const list = result.servers || [];
            setServers(list);
            if (list.length === 1 && list[0]?.machine_id) {
              setSelectedMachine(list[0].machine_id);
            } else {
              setSelectedMachine("");
            }
            setPhase("pick_server");
            setMessage(
              result.username
                ? `Signed in as ${result.username}. Choose a Plex Media Server.`
                : "Signed in. Choose a Plex Media Server.",
            );
          } catch (e) {
            stopPoll();
            setPhase("error");
            setMessage(e instanceof Error ? e.message : "Plex sign-in poll failed");
          }
        })();
      }, 2000);
    } catch (e) {
      setPhase("error");
      setMessage(e instanceof Error ? e.message : "Could not start Plex sign-in");
    }
  };

  const completeOauth = async () => {
    if (!selectedMachine) return;
    setPhase("saving");
    setMessage("Saving Plex connection…");
    try {
      const result = await postPlexOauthComplete({
        auth_token: pendingToken || undefined,
        client_id: pendingClientId || undefined,
        machine_id: selectedMachine,
        username: pendingUsername,
      });
      props.onFieldChange("ENABLE_PLEX", true);
      props.onFieldChange("PLEX_URL", result.url || "");
      props.onFieldChange("PLEX_AUTH_METHOD", "oauth");
      props.onFieldChange("PLEX_ACCOUNT_USERNAME", result.username || pendingUsername || "");
      props.onFieldChange("PLEX_SERVER_NAME", result.server_name || "");
      props.onFieldChange("PLEX_SERVER_MACHINE_ID", result.machine_id || selectedMachine);
      // Token is secret-saved server-side; clear draft so we don't overwrite with blank on Save.
      props.onFieldChange("PLEX_TOKEN", "");
      setPhase("done");
      const who = result.username || pendingUsername;
      const server = result.server_name || "Plex Media Server";
      setMessage(who ? `Connected as ${who} · ${server}` : `Connected · ${server}`);
      await props.onConnected();
    } catch (e) {
      setPhase("error");
      setMessage(e instanceof Error ? e.message : "Failed to save Plex connection");
    }
  };

  const changeServer = async () => {
    setMode("oauth");
    setPhase("pick_server");
    setMessage("Loading servers…");
    try {
      const data = await postPlexOauthServers(
        pendingToken
          ? { auth_token: pendingToken, client_id: pendingClientId || undefined }
          : undefined,
      );
      setServers(data.servers || []);
      const current = String(props.values.PLEX_SERVER_MACHINE_ID ?? "").trim();
      setSelectedMachine(current || data.servers?.[0]?.machine_id || "");
      if (!pendingToken) {
        // Complete will need a fresh sign-in if we only have stored tokens and
        // servers endpoint used them; re-auth for complete with account token.
        setMessage("Choose a server. If saving fails, sign in with Plex again.");
      } else {
        setMessage("Choose a Plex Media Server.");
      }
    } catch (e) {
      setPhase("error");
      setMessage(e instanceof Error ? e.message : "Could not list Plex servers");
    }
  };

  const switchManual = () => {
    stopPoll();
    setMode("manual");
    setPhase("idle");
    setMessage(null);
    props.onFieldChange("PLEX_AUTH_METHOD", "manual");
  };

  return (
    <div className="space-y-4">
      {mode === "oauth" ? (
        <div className="space-y-3">
          {phase === "done" || (alreadyOauth && phase !== "pick_server" && phase !== "waiting" && phase !== "saving") ? (
            <div className="rounded-xl border border-emerald-500/30 bg-emerald-500/10 px-3 py-3 space-y-1">
              <div className="text-[12px] font-headline uppercase tracking-widest text-emerald-400/90">Connected</div>
              <p className="text-[15px] text-slate-100">
                {summary.username || pendingUsername || "Plex account"}
                {summary.serverName || props.values.PLEX_SERVER_NAME ? (
                  <span className="text-slate-400"> · {String(summary.serverName || props.values.PLEX_SERVER_NAME)}</span>
                ) : null}
              </p>
              {summary.url ? (
                <p className="font-mono text-[12px] text-slate-500 truncate" title={summary.url}>
                  {summary.url}
                </p>
              ) : null}
            </div>
          ) : null}

          {phase === "pick_server" ? (
            <div className="space-y-2">
              <label className="block space-y-1">
                <span className="text-[14px] font-semibold text-slate-300">Plex Media Server</span>
                <p className="text-[13px] text-slate-500">
                  Pick which server Placeholdarr should use. Local connections are preferred when available.
                </p>
                <BrandSelect
                  fullWidth
                  value={selectedMachine}
                  options={[
                    { value: "", label: servers.length ? "Select a server…" : "No servers found" },
                    ...servers.map((s) => ({
                      value: s.machine_id,
                      label: `${s.name}${s.owned === false ? " (shared)" : ""}${s.uri ? "" : " · offline?"}`,
                    })),
                  ]}
                  onChange={setSelectedMachine}
                  ariaLabel="Plex Media Server"
                />
              </label>
              {selectedMachine ? (
                <p className="text-[12px] text-slate-500 font-mono truncate">
                  {servers.find((s) => s.machine_id === selectedMachine)?.uri || "No URI for this server"}
                </p>
              ) : null}
              <button
                type="button"
                disabled={!selectedMachine || phase === "saving"}
                onClick={() => void completeOauth()}
                className="w-full rounded-xl py-2.5 text-[14px] font-semibold disabled:opacity-40"
                style={{ backgroundColor: props.accentHex, color: "#0b0f14" }}
              >
                {phase === "saving" ? "Connecting…" : "Use this server"}
              </button>
            </div>
          ) : null}

          {phase !== "pick_server" ? (
            <div className="flex flex-wrap gap-2">
              {phase === "done" || alreadyOauth ? (
                <>
                  <button
                    type="button"
                    onClick={() => void startOauth()}
                    className="rounded-xl border border-[#424753]/55 px-3 py-2 text-[13px] text-slate-200 hover:bg-[#252e3a]/80"
                  >
                    Sign in again
                  </button>
                  <button
                    type="button"
                    onClick={() => void changeServer()}
                    className="rounded-xl border border-[#424753]/55 px-3 py-2 text-[13px] text-slate-200 hover:bg-[#252e3a]/80"
                  >
                    Change server
                  </button>
                </>
              ) : (
                <button
                  type="button"
                  onClick={() => void startOauth()}
                  disabled={phase === "waiting" || phase === "saving"}
                  className="rounded-xl px-3 py-2.5 text-[14px] font-semibold disabled:opacity-50"
                  style={{ backgroundColor: props.accentHex, color: "#0b0f14" }}
                >
                  {phase === "waiting" ? "Waiting for Plex…" : "Sign in with Plex"}
                </button>
              )}
              {phase === "waiting" ? (
                <button
                  type="button"
                  onClick={() => {
                    stopPoll();
                    setPhase("idle");
                    setMessage(null);
                  }}
                  className="rounded-xl border border-[#424753]/55 px-3 py-2 text-[13px] text-slate-300"
                >
                  Cancel
                </button>
              ) : null}
              {authUrl && phase === "waiting" ? (
                <a href={authUrl} target="_blank" rel="noopener noreferrer" className="text-[13px] underline text-slate-400 self-center">
                  Open Plex again
                </a>
              ) : null}
            </div>
          ) : null}

          {message ? (
            <p
              className={`text-[13px] ${
                phase === "error" ? "text-red-400" : phase === "done" ? "text-emerald-400" : "text-slate-400"
              }`}
            >
              {message}
            </p>
          ) : null}

          <button
            type="button"
            onClick={switchManual}
            className="text-[13px] text-slate-500 underline underline-offset-2 hover:text-slate-300"
          >
            Connect with URL and token instead
          </button>
        </div>
      ) : (
        <div className="space-y-4">
          <p className="text-[13px] text-slate-500">
            Manual connection. Prefer Sign in with Plex when you can; it picks the server URI for you.
          </p>
          {urlField ? (
            <div>
              <label className="mb-1 block text-[14px] font-semibold text-slate-300">{urlField.label}</label>
              {urlField.description ? <p className="mb-1.5 text-[13px] text-slate-500">{urlField.description}</p> : null}
              <input
                className={`w-full rounded-lg border border-[#424753]/40 bg-[#0f1419] px-3 py-2 text-[16px] text-slate-200 placeholder-slate-600 outline-none ${props.focusClass}`}
                type="text"
                value={String(props.values.PLEX_URL ?? "")}
                placeholder="http://plex.local:32400"
                onChange={(e) => {
                  props.onFieldChange("PLEX_URL", e.target.value);
                  props.onFieldChange("PLEX_AUTH_METHOD", "manual");
                }}
              />
            </div>
          ) : null}
          {tokenField ? (
            <div>
              <label className="mb-1 block text-[14px] font-semibold text-slate-300">{tokenField.label}</label>
              {tokenField.description ? (
                <p className="mb-1.5 text-[13px] text-slate-500">{tokenField.description}</p>
              ) : null}
              <input
                className={`w-full rounded-lg border border-[#424753]/40 bg-[#0f1419] px-3 py-2 text-[16px] text-slate-200 placeholder-slate-600 outline-none ${props.focusClass}`}
                type="password"
                value={String(props.values.PLEX_TOKEN ?? "")}
                placeholder={
                  tokenField.has_saved_value
                    ? "Saved value retained unless overwritten"
                    : "Enter plex token…"
                }
                onChange={(e) => {
                  props.onFieldChange("PLEX_TOKEN", e.target.value);
                  props.onFieldChange("PLEX_AUTH_METHOD", "manual");
                }}
              />
            </div>
          ) : null}
          <button
            type="button"
            onClick={() => {
              setMode("oauth");
              setPhase(alreadyOauth ? "done" : "idle");
              setMessage(null);
            }}
            className="text-[13px] text-slate-500 underline underline-offset-2 hover:text-slate-300"
          >
            Use Sign in with Plex instead
          </button>
        </div>
      )}

      {otherFields.map((field) => {
        const value = props.values[field.key];
        return (
          <div key={field.key}>
            <label className="mb-1 block text-[14px] font-semibold text-slate-300">{field.label}</label>
            {field.description ? <p className="mb-1.5 text-[13px] text-slate-500">{field.description}</p> : null}
            <input
              className={`w-full rounded-lg border border-[#424753]/40 bg-[#0f1419] px-3 py-2 text-[16px] text-slate-200 placeholder-slate-600 outline-none ${props.focusClass}`}
              type={field.type === "int" ? "number" : field.secret ? "password" : "text"}
              value={String(value ?? "")}
              placeholder={
                field.secret && field.has_saved_value
                  ? "Saved value retained unless overwritten"
                  : `Enter ${field.label.toLowerCase()}…`
              }
              onChange={(e) => props.onFieldChange(field.key, e.target.value)}
            />
          </div>
        );
      })}
    </div>
  );
}
