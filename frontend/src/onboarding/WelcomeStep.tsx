export function WelcomeStep() {
  return (
    <div className="mx-auto max-w-2xl space-y-5 text-left">
      <div className="space-y-3">
        <h2 className="font-headline text-[28px] font-bold tracking-tight text-white sm:text-[32px]">
          Welcome to Placeholdarr
        </h2>
        <p className="text-[16px] leading-relaxed text-slate-300">
          Placeholdarr lets your Plex, Jellyfin, or Emby library grow with lightweight placeholders instead of full
          downloads until you actually want the content. Play a placeholder to make a request and Placeholdarr asks
          Radarr or Sonarr to take action based on this app setup.
        </p>
        <p className="text-[16px] leading-relaxed text-slate-300">
          Next, we&apos;ll decide how you want Placeholdarr to work, connect your player and Arr apps, choose where
          placeholders live, set multi-Arr routing if you need it, and finish a few optional details.
        </p>
      </div>
    </div>
  );
}
