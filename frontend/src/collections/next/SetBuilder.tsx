import type { ComponentProps } from "react";
import { CollectionSetEditor } from "../CollectionSetEditor";

export function SetBuilder(props: ComponentProps<typeof CollectionSetEditor>) {
  return <CollectionSetEditor {...props} uiVariant="next" />;
}