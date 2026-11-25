import type { ComponentProps } from "react";
import { CollectionsPanel } from "../CollectionsPanel";

export function CollectionsPanelNext(props: Omit<ComponentProps<typeof CollectionsPanel>, "useNextUi">) {
  return <CollectionsPanel {...props} useNextUi />;
}