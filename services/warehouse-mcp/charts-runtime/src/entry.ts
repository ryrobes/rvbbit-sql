import * as core from "@tanstack/charts";
import { tooltip } from "@tanstack/charts/tooltip";
import {
  scaleBand,
  scaleLinear,
  scaleLog,
  scaleOrdinal,
  scalePoint,
  scalePow,
  scaleQuantile,
  scaleQuantize,
  scaleSequential,
  scaleSqrt,
  scaleSymlog,
  scaleThreshold,
  scaleTime,
  scaleUtc,
} from "d3-scale";
import {
  curveBasis,
  curveBumpX,
  curveBumpY,
  curveBundle,
  curveCardinal,
  curveCatmullRom,
  curveLinear,
  curveMonotoneX,
  curveMonotoneY,
  curveNatural,
  curveStep,
  curveStepAfter,
  curveStepBefore,
} from "d3-shape";
import type {
  ChartHost,
  ChartHostOptions,
  ChartPoint,
  ChartRenderContext,
  ChartValue,
} from "@tanstack/charts";

const VERSION = "0.3.1";

type AnyPoint = ChartPoint<unknown, ChartValue, ChartValue>;
type AnyOptions = ChartHostOptions<unknown, ChartValue, ChartValue>;
type AnyContext = ChartRenderContext<unknown, ChartValue, ChartValue>;

interface MarkMetadata {
  query?: string;
  x?: string;
  y?: string;
  value?: string | ((point: AnyPoint) => unknown);
  context?: readonly string[] | ((point: AnyPoint) => Record<string, unknown>);
  semanticObject?: string | ((point: AnyPoint) => string | null | undefined);
}

interface RvbbitChartMetadata {
  id: string;
  query?: string;
  marks?: Record<string, MarkMetadata>;
}

interface RvbbitChartSelection {
  chartId: string;
  query: string | null;
  key: string;
  markId: string;
  group: string | number | null;
  groupLabel: string;
  datumIndex: number;
  datum: unknown;
  xValue: ChartValue;
  yValue: ChartValue;
  semanticObject: string | null;
  value: unknown;
  context: Record<string, unknown>;
}

interface RvbbitChartHost {
  update: (options: AnyOptions) => void;
  getScene: ChartHost<unknown, ChartValue, ChartValue>["getScene"];
  selection: () => RvbbitChartSelection | null;
  destroy: () => void;
}

const hosts = new Map<string, RvbbitChartHost>();
const selections = new Map<string, RvbbitChartSelection>();

function boundedAttribute(value: unknown, limit = 240): string | null {
  if (value === null || value === undefined) return null;
  if (!["string", "number", "boolean", "bigint"].includes(typeof value)) return null;
  return String(value).replace(/\s+/g, " ").trim().slice(0, limit) || null;
}

function setAttribute(element: Element, name: string, value: unknown): void {
  const text = boundedAttribute(value);
  if (text === null) element.removeAttribute(name);
  else element.setAttribute(name, text);
}

function safeClass(value: string): string {
  return value.replace(/[^a-zA-Z0-9_-]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 80);
}

function markMetadata(metadata: RvbbitChartMetadata, point: AnyPoint): MarkMetadata {
  return metadata.marks?.[point.markId] ?? {};
}

function pointElementIndex(container: HTMLElement): Map<string, SVGElement> {
  const index = new Map<string, SVGElement>();
  for (const element of container.querySelectorAll<SVGElement>("[data-ts-key]")) {
    const key = element.getAttribute("data-ts-key");
    if (key && !index.has(key)) index.set(key, element);
  }
  return index;
}

function pointElement(
  container: HTMLElement,
  point: AnyPoint,
  elements?: Map<string, SVGElement>,
): SVGElement | null {
  const candidates = [point.key, `${point.key}:dot`];
  if (elements) {
    for (const key of candidates) {
      const element = elements.get(key);
      if (element) return element;
    }
    return null;
  }
  for (const element of container.querySelectorAll<SVGElement>("[data-ts-key]")) {
    if (candidates.includes(element.getAttribute("data-ts-key") ?? "")) return element;
  }
  return null;
}

function pointContext(point: AnyPoint, mark: MarkMetadata): Record<string, unknown> {
  if (typeof mark.context === "function") return mark.context(point) ?? {};
  const datum = point.datum && typeof point.datum === "object"
    ? point.datum as Record<string, unknown>
    : {};
  const fields = mark.context ?? [mark.x, mark.y].filter((field): field is string => Boolean(field));
  const context: Record<string, unknown> = {};
  for (const field of fields) {
    if (field in datum) context[field] = datum[field];
  }
  return context;
}

function pointValue(point: AnyPoint, mark: MarkMetadata): unknown {
  if (typeof mark.value === "function") return mark.value(point);
  if (typeof mark.value === "string" && point.datum && typeof point.datum === "object") {
    return (point.datum as Record<string, unknown>)[mark.value];
  }
  if (mark.y && point.datum && typeof point.datum === "object") {
    const value = (point.datum as Record<string, unknown>)[mark.y];
    if (value !== undefined) return value;
  }
  return point.yValue;
}

function semanticObject(point: AnyPoint, mark: MarkMetadata): string | null {
  const value = typeof mark.semanticObject === "function"
    ? mark.semanticObject(point)
    : mark.semanticObject;
  return boundedAttribute(value, 160);
}

function decoratePoint(
  container: HTMLElement,
  metadata: RvbbitChartMetadata,
  point: AnyPoint,
  elements?: Map<string, SVGElement>,
): SVGElement | null {
  const element = pointElement(container, point, elements);
  if (!element) return null;
  const mark = markMetadata(metadata, point);
  const query = mark.query ?? metadata.query;
  const objectId = semanticObject(point, mark);
  const value = pointValue(point, mark);
  element.classList.add("rvbbit-chart-point");
  const markClass = safeClass(point.markId);
  if (markClass) element.classList.add(`rvbbit-chart-point--${markClass}`);
  setAttribute(element, "data-rvbbit-chart", metadata.id);
  setAttribute(element, "data-rvbbit-mark", point.markId);
  setAttribute(element, "data-rvbbit-key", point.key);
  setAttribute(element, "data-rvbbit-query", query);
  setAttribute(element, "data-rvbbit-object-ref", objectId);
  setAttribute(element, "data-row-index", point.datumIndex);
  setAttribute(element, "data-field", [mark.x, mark.y].filter(Boolean).join(","));
  setAttribute(element, "data-series", point.groupLabel || null);
  setAttribute(element, "data-value", value);
  setAttribute(element, "data-rvbbit-x", point.xValue);
  setAttribute(element, "data-rvbbit-y", point.yValue);
  if (objectId && window.RVBBIT_DASHBOARD?.bindSemanticObject) {
    window.RVBBIT_DASHBOARD.bindSemanticObject(objectId, element, {
      value,
      context: pointContext(point, mark),
    });
  }
  return element;
}

function ensureRuntimeStyle(document: Document): void {
  if (document.querySelector("style[data-rvbbit-tanstack-charts]")) return;
  const style = document.createElement("style");
  style.setAttribute("data-rvbbit-tanstack-charts", VERSION);
  style.textContent = `
    [data-rvbbit-chart] .rvbbit-chart-point { cursor: pointer; transition: opacity 120ms ease, filter 120ms ease; }
    [data-rvbbit-chart] .rvbbit-chart-point:hover { filter: brightness(1.12); }
    [data-rvbbit-chart] .rvbbit-chart-point[data-rvbbit-selected="true"] {
      filter: drop-shadow(0 0 5px color-mix(in srgb, var(--ts-chart-focus, #f4b942) 72%, transparent));
    }
  `;
  document.head.append(style);
}

function resolveContainer(target: string | HTMLElement): HTMLElement {
  const element = typeof target === "string" ? document.querySelector<HTMLElement>(target) : target;
  if (!element) throw new Error(`RVBBIT chart container not found: ${String(target)}`);
  return element;
}

function createSelection(
  metadata: RvbbitChartMetadata,
  point: AnyPoint,
): RvbbitChartSelection {
  const mark = markMetadata(metadata, point);
  return {
    chartId: metadata.id,
    query: mark.query ?? metadata.query ?? null,
    key: point.key,
    markId: point.markId,
    group: point.group,
    groupLabel: point.groupLabel,
    datumIndex: point.datumIndex,
    datum: point.datum,
    xValue: point.xValue,
    yValue: point.yValue,
    semanticObject: semanticObject(point, mark),
    value: pointValue(point, mark),
    context: pointContext(point, mark),
  };
}

function bindSelection(
  container: HTMLElement,
  metadata: RvbbitChartMetadata,
  point: AnyPoint,
): RvbbitChartSelection {
  const selected = createSelection(metadata, point);
  const element = decoratePoint(container, metadata, point);
  container.querySelectorAll("[data-rvbbit-selected]").forEach((node) => {
    node.removeAttribute("data-rvbbit-selected");
  });
  if (element) element.setAttribute("data-rvbbit-selected", "true");
  selections.set(metadata.id, selected);

  const dashboard = window.RVBBIT_DASHBOARD;
  if (selected.semanticObject && dashboard?.bindSemanticObject) {
    dashboard.bindSemanticObject(selected.semanticObject, element ?? container, {
      value: selected.value,
      context: selected.context,
    });
  }
  window.dispatchEvent(new CustomEvent("rvbbit:chart-select", { detail: selected }));
  return selected;
}

function clearSelection(container: HTMLElement, chartId: string): void {
  container.querySelectorAll("[data-rvbbit-selected]").forEach((node) => {
    node.removeAttribute("data-rvbbit-selected");
  });
  selections.delete(chartId);
  window.dispatchEvent(new CustomEvent("rvbbit:chart-select", {
    detail: { chartId, selection: null },
  }));
}

function decorateScene(
  container: HTMLElement,
  metadata: RvbbitChartMetadata,
  context: AnyContext,
): void {
  setAttribute(container, "data-rvbbit-chart", metadata.id);
  setAttribute(container, "data-rvbbit-query", metadata.query);
  const elements = pointElementIndex(container);
  for (const point of context.scene.points) {
    decoratePoint(container, metadata, point as AnyPoint, elements);
  }
}

function mountRvbbitChart(
  target: string | HTMLElement,
  initialOptions: AnyOptions,
  metadata: RvbbitChartMetadata,
): RvbbitChartHost {
  if (!metadata?.id?.trim()) throw new TypeError("mountRvbbitChart requires metadata.id");
  const container = resolveContainer(target);
  ensureRuntimeStyle(container.ownerDocument);
  hosts.get(metadata.id)?.destroy();

  let scenePoints: AnyPoint[] = [];
  let clickedPointKey: string | null = null;

  // TanStack's pointer selection intentionally uses distance to a scene point.
  // That is excellent for sparse marks, but a click near the edge of a large bar
  // can fall outside the hit radius even though the SVG datum itself was clicked.
  // Capture its stable key before TanStack's bubble listener runs and prefer the
  // exact datum without changing native focus, tooltip, or keyboard behavior.
  const rememberClickedPoint = (event: MouseEvent): void => {
    const targetElement = event.target instanceof Element
      ? event.target.closest<SVGElement>("[data-rvbbit-key]")
      : null;
    clickedPointKey = targetElement && container.contains(targetElement)
      ? targetElement.getAttribute("data-rvbbit-key")
      : null;
  };
  const forgetClickedPoint = (): void => {
    clickedPointKey = null;
  };
  container.addEventListener("click", rememberClickedPoint, true);

  const wrapOptions = (options: AnyOptions): AnyOptions => {
    const userSelect = options.onSelect;
    const userRender = options.onRender;
    return {
      ...options,
      onSelect(point) {
        const exactPoint = clickedPointKey
          ? scenePoints.find((candidate) => candidate.key === clickedPointKey) ?? null
          : null;
        const selectedPoint = exactPoint ?? point;
        if (selectedPoint) bindSelection(container, metadata, selectedPoint as AnyPoint);
        else clearSelection(container, metadata.id);
        userSelect?.(selectedPoint);
      },
      onRender(context) {
        scenePoints = [...context.scene.points] as AnyPoint[];
        decorateScene(container, metadata, context as AnyContext);
        userRender?.(context);
      },
    };
  };

  const chart = core.mountChart(container, wrapOptions(initialOptions));
  // Registered after TanStack's listener so cleanup happens after onSelect.
  container.addEventListener("click", forgetClickedPoint);
  const host: RvbbitChartHost = {
    update(options) {
      chart.update(wrapOptions(options));
    },
    getScene: chart.getScene,
    selection: () => selections.get(metadata.id) ?? null,
    destroy() {
      container.removeEventListener("click", rememberClickedPoint, true);
      container.removeEventListener("click", forgetClickedPoint);
      chart.destroy();
      selections.delete(metadata.id);
      hosts.delete(metadata.id);
      container.removeAttribute("data-rvbbit-chart");
      container.removeAttribute("data-rvbbit-query");
    },
  };
  hosts.set(metadata.id, host);
  return host;
}

declare global {
  interface Window {
    RVBBIT_CHARTS?: Record<string, unknown>;
    RVBBIT_DASHBOARD?: {
      bindSemanticObject?: (
        id: string,
        target: Element,
        state: { value: unknown; context: Record<string, unknown> },
      ) => unknown;
    };
  }
}

const scales = Object.freeze({
  band: scaleBand,
  linear: scaleLinear,
  log: scaleLog,
  ordinal: scaleOrdinal,
  point: scalePoint,
  pow: scalePow,
  quantile: scaleQuantile,
  quantize: scaleQuantize,
  sequential: scaleSequential,
  sqrt: scaleSqrt,
  symlog: scaleSymlog,
  threshold: scaleThreshold,
  time: scaleTime,
  utc: scaleUtc,
});

const curves = Object.freeze({
  basis: curveBasis,
  bumpX: curveBumpX,
  bumpY: curveBumpY,
  bundle: curveBundle,
  cardinal: curveCardinal,
  catmullRom: curveCatmullRom,
  linear: curveLinear,
  monotoneX: curveMonotoneX,
  monotoneY: curveMonotoneY,
  natural: curveNatural,
  step: curveStep,
  stepAfter: curveStepAfter,
  stepBefore: curveStepBefore,
});

const api = Object.freeze({
  ...core,
  tooltip,
  scales,
  curves,
  version: VERSION,
  mountRvbbitChart,
  host: (chartId: string) => hosts.get(chartId) ?? null,
  selection: (chartId: string) => selections.get(chartId) ?? null,
});

if (window.RVBBIT_CHARTS && window.RVBBIT_CHARTS.version !== VERSION) {
  console.warn(
    `RVBBIT Charts ${VERSION} replaced runtime ${String(window.RVBBIT_CHARTS.version ?? "unknown")}`,
  );
}
window.RVBBIT_CHARTS = api;
window.dispatchEvent(new CustomEvent("rvbbit:charts-ready", { detail: { version: VERSION } }));
