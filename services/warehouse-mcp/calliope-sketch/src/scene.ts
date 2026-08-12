import { convertToExcalidrawElements } from "@excalidraw/excalidraw";
import type { ExcalidrawElement } from "@excalidraw/excalidraw/element/types";

export type SketchScene = {
  elements?: Array<Record<string, unknown>>;
  appState?: Record<string, unknown>;
  files?: Record<string, never>;
};

function isNativeElement(element: Record<string, unknown>) {
  return Number.isInteger(element.version)
    && Number.isInteger(element.versionNonce)
    && Number.isInteger(element.seed);
}

function stableLabelId(elementId: unknown) {
  return `${String(elementId || "label").slice(0, 89)}--label`;
}

/**
 * Turn Calliope's small semantic DSL into one canonical Excalidraw scene.
 *
 * A scene may be mixed: human-edited elements are already native while newly
 * added Calliope elements are semantic skeletons. Conversion must retain the
 * human work, materialize labels/bindings once, and strip the construction
 * hints so the next mixed revision cannot duplicate them.
 */
export function materializeSketchElements(scene: SketchScene): ExcalidrawElement[] {
  const source = Array.isArray(scene?.elements)
    ? scene.elements.filter((element): element is Record<string, unknown> =>
      Boolean(element && typeof element === "object" && element.isDeleted !== true))
    : [];
  const sourceIds = new Set(source.map((element) => String(element.id || "")));
  const hasBoundText = (element: Record<string, unknown>) =>
    Array.isArray(element.boundElements)
      && element.boundElements.some((bound: any) =>
        bound?.type === "text" && sourceIds.has(String(bound.id || "")));
  const needsMaterialization = source.some((element) =>
    !isNativeElement(element)
      || (Boolean((element.label as any)?.text) && !hasBoundText(element)));

  const skeletons = source.map((element) => {
    const clean: Record<string, any> = { ...element };
    if (Array.isArray(clean.boundElements)) {
      clean.boundElements = clean.boundElements.filter((bound: any) =>
        bound && sourceIds.has(String(bound.id || "")));
    }
    // Native elements may retain the semantic construction hints after a
    // human save. Do not feed those hints back through the converter or it can
    // manufacture a second label/binding when one is already present.
    if (isNativeElement(element)) {
      if (hasBoundText(element)) delete clean.label;
      if (clean.startBinding) delete clean.start;
      if (clean.endBinding) delete clean.end;
    } else if (clean.label?.text && !clean.label.id) {
      clean.label = { ...clean.label, id: stableLabelId(clean.id) };
    }
    return clean;
  });

  const materialized = needsMaterialization
    ? convertToExcalidrawElements(
      skeletons as Parameters<typeof convertToExcalidrawElements>[0],
      { regenerateIds: false },
    ) as ExcalidrawElement[]
    : skeletons as unknown as ExcalidrawElement[];
  const materializedIds = new Set(materialized.map((element) => String(element.id)));
  return materialized.map((element) => {
    const clean: Record<string, any> = { ...element };
    // Persist only canonical Excalidraw bindings. These semantic hints did
    // their job during conversion and otherwise cause duplicate labels on the
    // next mixed human/agent revision.
    delete clean.label;
    delete clean.start;
    delete clean.end;
    if (Array.isArray(clean.boundElements)) {
      clean.boundElements = clean.boundElements.filter((bound: any) =>
        bound && materializedIds.has(String(bound.id || "")));
    }
    return clean as ExcalidrawElement;
  });
}
