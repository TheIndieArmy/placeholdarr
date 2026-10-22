/** Shared Visible On at creation help copy for recipe and Collection Set editors. */

type VisibleOnAtCreationHelpProps = {
  titleClassName: string;
  mutedClassName: string;
  plexPassClassName: string;
};

export function VisibleOnAtCreationHelp(props: VisibleOnAtCreationHelpProps) {
  return (
    <>
      <div className={`mb-1 ${props.titleClassName}`}>Visible On At Creation</div>
      <div className={`mb-3 text-[13px] leading-relaxed ${props.mutedClassName}`}>
        <ul className="list-disc space-y-1.5 pl-4">
          <li>
            These choices apply <strong className="font-semibold">only</strong> when Placeholdarr creates a
            collection in Plex:
            <ul className="mt-1 list-disc space-y-1 pl-4">
              <li>first-time creation</li>
              <li>when a Collection Set adds a new child</li>
              <li>
                when a seasonal delete brings it back next season (only if Delete is selected for when the
                window is inactive)
              </li>
            </ul>
          </li>
          <li>
            Saving or syncing does not change Visible On for a collection that already exists. Post-creation
            changes still belong in Plex.
          </li>
          <li>
            <span className={props.plexPassClassName}>Plex Pass required to use this feature.</span> With all
            off, the collection still appears under the library&apos;s Collections list.
          </li>
        </ul>
      </div>
    </>
  );
}
