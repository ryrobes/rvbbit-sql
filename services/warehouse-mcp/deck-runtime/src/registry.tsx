/* The spec → component registry. Every name a deck spec may reference.
 * Vendored components are used as-is (NOTICE: never patch the engine);
 * rvbbit-specific pseudo-components (Text) live here, beside it. */
import type { ComponentType, ReactNode } from "react";

import Slide from "./vendor/deck/Slide";
import Accordion from "./vendor/components/Accordion";
import Agenda from "./vendor/components/Agenda";
import Bento from "./vendor/components/Bento";
import BigNumber from "./vendor/components/BigNumber";
import BrowserFrame from "./vendor/components/BrowserFrame";
import Chat from "./vendor/components/Chat";
import CodeWindow from "./vendor/components/CodeWindow";
import Comparison from "./vendor/components/Comparison";
import Contrast from "./vendor/components/Contrast";
import CountUp from "./vendor/components/CountUp";
import Cover from "./vendor/components/Cover";
import Marquee from "./vendor/components/Marquee";
import Pricing from "./vendor/components/Pricing";
import Quote from "./vendor/components/Quote";
import Section from "./vendor/components/Section";
import Split from "./vendor/components/Split";
import SpotlightCard from "./vendor/components/SpotlightCard";
import StatGrid from "./vendor/components/StatGrid";
import Steps from "./vendor/components/Steps";
import Table from "./vendor/components/Table";
import Tabs from "./vendor/components/Tabs";
import Team from "./vendor/components/Team";
import TiltCard from "./vendor/components/TiltCard";
import Timeline from "./vendor/components/Timeline";
import VisualDashboard from "./vendor/components/VisualDashboard";
import { BarChart, DonutChart, LineChart } from "./vendor/components/Charts";

/* Narrative slide without JSX: headline / subhead / body paragraphs,
 * centered by default (upstream taste rule: center what stands alone). */
function Text({
  headline,
  subhead,
  body,
  kicker,
  center = true,
  accent
}: {
  headline?: string;
  subhead?: string;
  body?: string[];
  kicker?: string;
  center?: boolean;
  accent?: string;
  nav?: string;
  notes?: string;
}) {
  return (
    <Slide center={center}>
      {kicker ? <p className="kicker">{kicker}</p> : null}
      {headline ? (
        <h2 className="headline">
          {headline}
          {accent ? <span className="accent-text"> {accent}</span> : null}
        </h2>
      ) : null}
      {subhead ? <p className="subhead">{subhead}</p> : null}
      {(body ?? []).map((t, i) => (
        <p key={i} className="body-text" style={{ maxWidth: 760, marginInline: center ? "auto" : undefined }}>
          {t}
        </p>
      ))}
    </Slide>
  );
}

/* Chart slides: the bare chart components aren't slides, so wrap them in
 * a titled Slide. `data`/`points`/`value` arrive via the spec data binding. */
function ChartSlide({
  kind = "bar",
  title,
  subtitle,
  data,
  points,
  value,
  label,
  height = 300,
  nav,
  notes
}: {
  kind?: "bar" | "line" | "donut";
  title?: string;
  subtitle?: string;
  data?: { label: string; value: number }[];
  points?: number[];
  value?: number;
  label?: string;
  height?: number;
  nav?: string;
  notes?: string;
}) {
  void nav;
  void notes;
  return (
    <Slide center>
      <div style={{ width: "100%", textAlign: "center" }}>
        {title ? <h2 className="headline" style={{ marginInline: "auto" }}>{title}</h2> : null}
        {subtitle ? <p className="subhead" style={{ marginInline: "auto" }}>{subtitle}</p> : null}
        <div style={{ width: "min(860px, 92%)", marginInline: "auto", marginTop: 24 }}>
          {kind === "bar" && data ? <BarChart data={data} height={height} /> : null}
          {kind === "line" && points ? <LineChart points={points} height={height} /> : null}
          {kind === "donut" && value != null ? <DonutChart value={value} label={label} /> : null}
        </div>
      </div>
    </Slide>
  );
}

// eslint-disable-next-line @typescript-eslint/no-explicit-any
export const REGISTRY: Record<string, ComponentType<any>> = {
  Slide,
  Text,
  ChartSlide,
  Accordion,
  Agenda,
  Bento,
  BigNumber,
  BrowserFrame,
  Chat,
  CodeWindow,
  Comparison,
  Contrast,
  CountUp,
  Cover,
  Marquee,
  Pricing,
  Quote,
  Section,
  Split,
  SpotlightCard,
  StatGrid,
  Steps,
  Table,
  Tabs,
  Team,
  TiltCard,
  Timeline,
  VisualDashboard
};

export function ErrorSlide({ name, error }: { name: string; error: string }): ReactNode {
  return (
    <Slide center nav="⚠">
      <p className="kicker">slide failed</p>
      <h2 className="headline">{name}</h2>
      <p className="subhead" style={{ marginInline: "auto", opacity: 0.7 }}>{error}</p>
    </Slide>
  );
}
