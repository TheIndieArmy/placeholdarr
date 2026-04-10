/**
 * Centralized webhook configuration and setup instructions.
 * Used by both onboarding wizard and documentation generation.
 */

export interface WebhookService {
  id: string;
  name: string;
  color: string;
  icon: string;
  description: string;
  urlTemplate: string; // e.g., "http://{host}:{port}/webhook?instance={instance}"
  triggers: WebhookTrigger[];
  notes?: string[];
}

export interface WebhookTrigger {
  event: string;
  displayName: string;
  description: string;
  required: boolean;
}

export interface WebhookServiceGroup {
  title: string;
  description: string;
  services: WebhookService[];
}

/**
 * ARR webhook configurations
 */
export const ARR_WEBHOOK_SERVICES: WebhookServiceGroup = {
  title: "ARR Integrations",
  description:
    "Placeholdarr monitors your Radarr and Sonarr instances for content changes via webhooks. Each instance requires its own webhook endpoint.",
  services: [
    {
      id: "radarr",
      name: "Radarr",
      color: "#e30b5c",
      icon: "●",
      description: "Movie management and downloading",
      urlTemplate: "http://{host}:{port}/webhook?instance={instance}",
      triggers: [
        {
          event: "OnGrab",
          displayName: "On Grab",
          description: "Triggered when a release is grabbed from an indexer",
          required: true,
        },
        {
          event: "OnImport",
          displayName: "On File Import",
          description: "Triggered when a movie file is imported/moved",
          required: true,
        },
        {
          event: "OnMovieAdd",
          displayName: "On Movie Add",
          description: "Triggered when a movie is added",
          required: true,
        },
        {
          event: "OnMovieDelete",
          displayName: "On Movie Delete",
          description: "Triggered when a movie is deleted",
          required: true,
        },
        {
          event: "OnMovieFileDelete",
          displayName: "On Movie File Delete",
          description: "Triggered when a movie file is deleted",
          required: true,
        },
      ],
      notes: [
        "Placeholdarr currently supports up to 2 Radarr instances per deployment",
        "Each instance must have its own webhook with the correct instance parameter",
        "The instance parameter uses each instance's stable webhook key shown in ARR settings",
      ],
    },
    {
      id: "sonarr",
      name: "Sonarr",
      color: "#3497dc",
      icon: "●",
      description: "TV series management and downloading",
      urlTemplate: "http://{host}:{port}/webhook?instance={instance}",
      triggers: [
        {
          event: "OnGrab",
          displayName: "On Grab",
          description: "Triggered when a release is grabbed from an indexer",
          required: true,
        },
        {
          event: "OnImport",
          displayName: "On File Import",
          description: "Triggered when an episode file is imported/moved",
          required: true,
        },
        {
          event: "OnSeriesAdd",
          displayName: "On Series Add",
          description: "Triggered when a series is added",
          required: true,
        },
        {
          event: "OnSeriesDelete",
          displayName: "On Series Delete",
          description: "Triggered when a series is deleted",
          required: true,
        },
        {
          event: "OnEpisodeFileDelete",
          displayName: "On Episode File Delete",
          description: "Triggered when an episode file is deleted",
          required: true,
        },
      ],
      notes: [
        "Placeholdarr currently supports up to 2 Sonarr instances per deployment",
        "Each instance must have its own webhook with the correct instance parameter",
        "The instance parameter uses each instance's stable webhook key shown in ARR settings",
      ],
    },
  ],
};

/**
 * Playback source webhook configurations
 */
export const PLAYBACK_WEBHOOK_SERVICES: WebhookServiceGroup = {
  title: "Playback Source Integrations (Optional)",
  description:
    "Placeholdarr can track playback activity from Tautulli, Jellyfin, or Emby to improve content recommendations and status tracking.",
  services: [
    {
      id: "tautulli",
      name: "Tautulli",
      color: "#ffc107",
      icon: "●",
      description: "Plex activity monitoring and analytics",
      urlTemplate: "http://{host}:{port}/webhook?instance=tautulli",
      triggers: [
        {
          event: "Playback Start",
          displayName: "Playback Started",
          description: "User begins playing media",
          required: false,
        },
        {
          event: "Playback Stop",
          displayName: "Playback Stopped",
          description: "User stops or pauses playback",
          required: false,
        },
        {
          event: "Playback Resume",
          displayName: "Playback Resumed",
          description: "User resumes after pause",
          required: false,
        },
      ],
      notes: [
        "Requires Tautulli webhooks send to: Settings → Notifications → Webhooks",
        "Use the fixed instance key 'tautulli' (not derived from server name)",
        "Optional: improves placeholder status and content recommendations",
      ],
    },
    {
      id: "jellyfin",
      name: "Jellyfin",
      color: "#00a4ef",
      icon: "●",
      description: "Open-source media server",
      urlTemplate: "http://{host}:{port}/webhook?instance=jellyfin",
      triggers: [
        {
          event: "Playback Start",
          displayName: "Playback Started",
          description: "User begins playing media",
          required: false,
        },
        {
          event: "Playback Stop",
          displayName: "Playback Stopped",
          description: "User stops playback",
          required: false,
        },
      ],
      notes: [
        "Requires applicable Jellyfin plugin or webhook integration",
        "Use the fixed instance key 'jellyfin' (not derived from server name)",
        "Optional: improves placeholder status tracking",
      ],
    },
    {
      id: "emby",
      name: "Emby",
      color: "#52b54b",
      icon: "●",
      description: "Emby media server",
      urlTemplate: "http://{host}:{port}/webhook?instance=emby",
      triggers: [
        {
          event: "Playback Start",
          displayName: "Playback Started",
          description: "User begins playing media",
          required: false,
        },
        {
          event: "Playback Stop",
          displayName: "Playback Stopped",
          description: "User stops playback",
          required: false,
        },
      ],
      notes: [
        "Requires Emby server webhook plugin configuration",
        "Use the fixed instance key 'emby' (not derived from server name)",
        "Optional: improves placeholder status tracking",
      ],
    },
  ],
};

/**
 * Helper function to generate webhook instructions for documentation
 */
export function generateWebhookInstructions(
  service: WebhookService,
  host: string,
  port: number | string,
  instanceKey: string
): string {
  const url = service.urlTemplate
    .replace("{host}", host)
    .replace("{port}", String(port))
    .replace("{instance}", instanceKey);

  const requiredTriggers = service.triggers.filter((t) => t.required);
  const optionalTriggers = service.triggers.filter((t) => !t.required);

  let instructions = `## ${service.name} Setup\n\n`;
  instructions += `1. In ${service.name}, go to Settings → Webhooks\n`;
  instructions += `2. Create New Webhook with URL: \`${url}\`\n`;
  instructions += `3. Enable these events:\n`;

  requiredTriggers.forEach((trigger) => {
    instructions += `   - ${trigger.displayName} (${trigger.description})\n`;
  });

  if (optionalTriggers.length > 0) {
    instructions += `4. Optional events:\n`;
    optionalTriggers.forEach((trigger) => {
      instructions += `   - ${trigger.displayName} (${trigger.description})\n`;
    });
  }

  instructions += `5. Test the webhook and save\n\n`;

  if (service.notes) {
    instructions += `**Notes:**\n`;
    service.notes.forEach((note) => {
      instructions += `- ${note}\n`;
    });
  }

  return instructions;
}

/**
 * Helper to validate instance key format
 */
export function validateInstanceKey(key: string): boolean {
  // Instance keys should be lowercase alphanumeric with underscores
  return /^[a-z0-9_]+$/.test(key);
}

/**
 * Generate README documentation for webhook setup
 */
export function generateWebhookReadme(): string {
  let readme = "# Webhook Setup\n\n";
  readme += "Placeholdarr receives webhooks on the `/webhook` endpoint and routes each request by the `instance` query parameter.\n\n";

  // ARR webhooks
  readme += `## ${ARR_WEBHOOK_SERVICES.title}\n\n`;
  readme += `${ARR_WEBHOOK_SERVICES.description}\n\n`;

  ARR_WEBHOOK_SERVICES.services.forEach((service) => {
    readme += `### ${service.name}\n\n`;
    readme += `${service.description}\n\n`;

    readme += `**Required Events:**\n`;
    service.triggers.forEach((trigger) => {
      readme += `- **${trigger.displayName}**: ${trigger.description}\n`;
    });
    readme += "\n";

    if (service.notes) {
      readme += `**Important Notes:**\n`;
      service.notes.forEach((note) => {
        readme += `- ${note}\n`;
      });
      readme += "\n";
    }
  });

  // Playback webhooks
  readme += `## ${PLAYBACK_WEBHOOK_SERVICES.title}\n\n`;
  readme += `${PLAYBACK_WEBHOOK_SERVICES.description}\n\n`;

  PLAYBACK_WEBHOOK_SERVICES.services.forEach((service) => {
    readme += `### ${service.name}\n\n`;
    readme += `${service.description}\n\n`;

    if (service.notes) {
      readme += `**Setup Notes:**\n`;
      service.notes.forEach((note) => {
        readme += `- ${note}\n`;
      });
      readme += "\n";
    }
  });

  readme += "## Testing Webhooks\n\n";
  readme += "After configuring each webhook:\n";
  readme += "1. Verify the instance key matches exactly (case-sensitive)\n";
  readme += "2. Test the webhook from your service's webhook settings\n";
  readme += "3. Check Placeholdarr logs for confirmation of received events\n";
  readme += "4. Monitor the dashboard to see events being processed\n";

  return readme;
}
