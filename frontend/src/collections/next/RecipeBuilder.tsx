import { CollectionEditor } from "../CollectionEditor";
import type { ComponentProps } from "react";

export function RecipeBuilder(props: ComponentProps<typeof CollectionEditor>) {
  return <CollectionEditor {...props} uiVariant="next" />;
}