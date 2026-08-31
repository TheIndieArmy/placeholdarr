import { fetchJson, postJson } from "./client";

export type WhatsNewNotice = {
  id: string;
  since_version: string;
  title: string;
  body: string;
  cta_label?: string | null;
  cta_path?: string | null;
  requires_ack?: boolean;
};

export type WhatsNewPayload = {
  app_version: string;
  last_seen_app_version: string | null;
  notices: WhatsNewNotice[];
};

export function groupWhatsNewByVersion(notices: WhatsNewNotice[]): { version: string; notices: WhatsNewNotice[] }[] {
  const groups: { version: string; notices: WhatsNewNotice[] }[] = [];
  for (const notice of notices) {
    const version = String(notice.since_version || "").trim() || "—";
    const last = groups[groups.length - 1];
    if (last && last.version === version) {
      last.notices.push(notice);
    } else {
      groups.push({ version, notices: [notice] });
    }
  }
  return groups;
}

export async function getWhatsNew(options?: { catalog?: boolean }): Promise<WhatsNewPayload> {
  const suffix = options?.catalog ? "?catalog=1" : "";
  return fetchJson<WhatsNewPayload>(`/api/whats-new${suffix}`);
}

export async function dismissWhatsNew(ids: string[]): Promise<WhatsNewPayload> {
  return postJson<WhatsNewPayload>("/api/whats-new/dismiss", { ids });
}
