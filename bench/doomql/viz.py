#!/usr/bin/env python3
"""Build a self-contained HTML viewer for compatible DoomQL scale runs."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from .run import system_label
except ImportError:
    from run import system_label


HERE = Path(__file__).resolve().parent
RESULTS = HERE / "results"
FORMAT = "doomql-scale-curves-v1"
SIGNATURE_FIELDS = (
    "world",
    "frames",
    "warmups",
    "width",
    "height",
    "draw_distance",
    "turn_degrees",
    "grid_scale",
    "render_type",
    "maps",
    "replay_session_sha256",
)


def default_inputs() -> list[Path]:
    return sorted(RESULTS.glob("scale-episode1-*.json"))


def signature(run: dict[str, Any]) -> dict[str, Any]:
    return {field: run.get(field) for field in SIGNATURE_FIELDS}


def load_curve_payload(paths: list[Path]) -> dict[str, Any]:
    if not paths:
        raise ValueError("no scale result documents supplied")

    runs: list[tuple[int, Path, dict[str, Any]]] = []
    expected_signature: dict[str, Any] | None = None
    seen_scales: set[int] = set()
    systems: list[str] = []

    for path in paths:
        try:
            run = json.loads(path.read_text(encoding="utf-8"))
            rows = int(run["environment"]["source_rows"])
            run_results = run["results"]
        except (OSError, TypeError, ValueError, KeyError, json.JSONDecodeError) as exc:
            raise ValueError(f"invalid DoomQL result {path}: {exc}") from exc
        if rows <= 0:
            raise ValueError(f"invalid source row count in {path}: {rows}")
        if rows in seen_scales:
            raise ValueError(f"duplicate {rows:,}-row result: {path}")
        seen_scales.add(rows)

        current_signature = signature(run)
        if expected_signature is None:
            expected_signature = current_signature
        elif current_signature != expected_signature:
            differences = [
                field
                for field in SIGNATURE_FIELDS
                if current_signature[field] != expected_signature[field]
            ]
            raise ValueError(
                f"incompatible benchmark settings in {path}: {', '.join(differences)}"
            )

        for result in run_results:
            system = str(result["system"])
            if system not in systems:
                systems.append(system)
        runs.append((rows, path, run))

    runs.sort(key=lambda item: item[0])
    points = []
    all_parity_ok = True
    for rows, path, run in runs:
        results = {}
        reference = next(
            (
                result
                for result in run["results"]
                if result["system"] == run.get("parity_reference")
            ),
            None,
        )
        reference_hashes = reference.get("frame_hashes") if reference else None
        for result in run["results"]:
            parity_ok = (
                result.get("status") == "ok"
                and reference_hashes is not None
                and result.get("frame_hashes") == reference_hashes
            )
            if result.get("status") not in {"ok", "skip"} or (
                result.get("status") == "ok" and not parity_ok
            ):
                all_parity_ok = False
            results[str(result["system"])] = {
                "status": result.get("status"),
                "route": result.get("route"),
                "first_ms": result.get("first_ms"),
                "median_ms": result.get("median_ms"),
                "p95_ms": result.get("p95_ms"),
                "fps": result.get("fps"),
                "parity_ok": parity_ok,
                "error": result.get("error"),
            }
        points.append(
            {
                "rows": rows,
                "table": run.get("table"),
                "generated_at": run.get("generated_at"),
                "source": str(path),
                "results": results,
            }
        )

    assert expected_signature is not None
    return {
        "format": FORMAT,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "benchmark": expected_signature,
        "scales": [point["rows"] for point in points],
        "systems": [
            {"id": system, "label": system_label(system)} for system in systems
        ],
        "all_parity_ok": all_parity_ok,
        "points": points,
    }


HTML = r'''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>DoomQL Scale Curves</title>
<style>
  :root {
    color-scheme: dark;
    --bg: #101210;
    --panel: #171a17;
    --panel-2: #1d211d;
    --border: #343a34;
    --grid: #2b302b;
    --text: #f2f3ed;
    --muted: #a8afa6;
    --accent: #e7b84b;
    --good: #75c88a;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0;
    min-width: 320px;
    background: var(--bg);
    color: var(--text);
    font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    font-size: 14px;
    letter-spacing: 0;
  }
  button, input { font: inherit; }
  .shell { max-width: 1540px; margin: 0 auto; padding: 24px 28px 40px; }
  header {
    display: flex;
    align-items: end;
    justify-content: space-between;
    gap: 20px;
    padding-bottom: 18px;
    border-bottom: 1px solid var(--border);
  }
  h1 { margin: 0; font-size: 27px; line-height: 1.1; font-weight: 760; letter-spacing: 0; }
  .subtitle { margin: 7px 0 0; color: var(--muted); }
  .status { display: flex; gap: 18px; align-items: center; color: var(--muted); white-space: nowrap; }
  .status b { color: var(--text); font-variant-numeric: tabular-nums; }
  .parity { color: var(--good); }
  .toolbar {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 14px 22px;
    padding: 16px 0;
  }
  .control { display: flex; align-items: center; gap: 8px; }
  .control-label { color: var(--muted); font-size: 12px; font-weight: 700; text-transform: uppercase; }
  .segments { display: inline-flex; border: 1px solid var(--border); border-radius: 5px; overflow: hidden; }
  .segments button {
    min-height: 32px;
    padding: 5px 11px;
    border: 0;
    border-right: 1px solid var(--border);
    background: var(--panel);
    color: var(--muted);
    cursor: pointer;
  }
  .segments button:last-child { border-right: 0; }
  .segments button:hover { color: var(--text); background: var(--panel-2); }
  .segments button.active { color: #11130f; background: var(--accent); font-weight: 750; }
  .chart-wrap {
    position: relative;
    min-height: 460px;
    border-top: 1px solid var(--border);
    border-bottom: 1px solid var(--border);
  }
  #chart { display: block; width: 100%; height: min(58vh, 620px); min-height: 460px; }
  .tooltip {
    position: absolute;
    z-index: 5;
    min-width: 172px;
    padding: 9px 11px;
    border: 1px solid #555d55;
    border-radius: 4px;
    background: #101310;
    box-shadow: 0 8px 24px #0009;
    pointer-events: none;
    opacity: 0;
    transform: translate(10px, -50%);
  }
  .tooltip.visible { opacity: 1; }
  .tip-system { font-weight: 750; }
  .tip-value { margin-top: 4px; font-size: 18px; font-variant-numeric: tabular-nums; }
  .tip-meta { margin-top: 3px; color: var(--muted); font-size: 12px; }
  .legend {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(300px, 1fr));
    gap: 7px 14px;
    padding: 16px 0;
  }
  .legend label {
    display: flex;
    min-width: 0;
    align-items: center;
    gap: 8px;
    padding: 6px 8px;
    border: 1px solid transparent;
    border-radius: 4px;
    color: var(--muted);
    cursor: pointer;
  }
  .legend label:hover { border-color: var(--border); color: var(--text); background: var(--panel); }
  .legend label.enabled { color: var(--text); }
  .legend input { position: absolute; opacity: 0; pointer-events: none; }
  .swatch { width: 15px; height: 3px; flex: 0 0 auto; border-radius: 1px; }
  .legend-name { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .latest {
    margin-left: auto;
    white-space: nowrap;
    font-variant-numeric: tabular-nums;
    font-size: 12px;
  }
  .table-head { display: flex; justify-content: space-between; align-items: baseline; gap: 16px; margin-top: 14px; }
  h2 { margin: 0; font-size: 16px; letter-spacing: 0; }
  .table-meta { color: var(--muted); font-size: 12px; }
  .table-scroll { overflow-x: auto; margin-top: 10px; border-top: 1px solid var(--border); }
  table { width: 100%; min-width: 800px; border-collapse: collapse; font-variant-numeric: tabular-nums; }
  th, td { padding: 10px 12px; border-bottom: 1px solid var(--grid); text-align: right; }
  th { color: var(--muted); font-size: 11px; text-transform: uppercase; font-weight: 750; }
  th:first-child, td:first-child { position: sticky; left: 0; text-align: left; background: var(--bg); }
  tbody tr:hover td { background: var(--panel); }
  tbody tr:hover td:first-child { background: var(--panel); }
  .table-system { display: inline-flex; align-items: center; gap: 8px; white-space: nowrap; }
  .axis-label, .tick-label { fill: var(--muted); font-family: inherit; }
  .axis-label { font-size: 12px; font-weight: 650; }
  .tick-label { font-size: 11px; }
  .grid-line { stroke: var(--grid); stroke-width: 1; vector-effect: non-scaling-stroke; }
  .axis-line { stroke: #4b524b; stroke-width: 1; vector-effect: non-scaling-stroke; }
  .curve { fill: none; stroke-width: 2.3; vector-effect: non-scaling-stroke; }
  .point { stroke: var(--bg); stroke-width: 2; vector-effect: non-scaling-stroke; cursor: crosshair; }
  .point:hover { stroke: var(--text); stroke-width: 3; }
  @media (max-width: 760px) {
    .shell { padding: 18px 14px 28px; }
    header { align-items: flex-start; flex-direction: column; }
    .status { width: 100%; justify-content: space-between; gap: 8px; }
    .toolbar { gap: 11px 16px; }
    .control { align-items: flex-start; flex-direction: column; }
    #chart { height: 470px; }
    .legend { grid-template-columns: repeat(auto-fit, minmax(170px, 1fr)); }
  }
</style>
</head>
<body>
<main class="shell">
  <header>
    <div>
      <h1>DoomQL Scale Curves</h1>
      <p class="subtitle" id="subtitle"></p>
    </div>
    <div class="status">
      <span><b id="scale-count"></b> scales</span>
      <span><b id="engine-count"></b> engines</span>
      <span class="parity" id="parity"></span>
    </div>
  </header>
  <section class="toolbar" aria-label="Chart controls">
    <div class="control">
      <span class="control-label">Metric</span>
      <div class="segments" id="metric-controls">
        <button type="button" data-value="median_ms" class="active">Median</button>
        <button type="button" data-value="p95_ms">P95</button>
        <button type="button" data-value="first_ms">Cold</button>
        <button type="button" data-value="fps">FPS</button>
      </div>
    </div>
    <div class="control">
      <span class="control-label">Rows</span>
      <div class="segments" id="x-controls">
        <button type="button" data-value="linear" class="active">Linear</button>
        <button type="button" data-value="log">Log</button>
      </div>
    </div>
    <div class="control">
      <span class="control-label">Value</span>
      <div class="segments" id="y-controls">
        <button type="button" data-value="linear" class="active">Linear</button>
        <button type="button" data-value="log">Log</button>
      </div>
    </div>
  </section>
  <section class="chart-wrap">
    <svg id="chart" role="img" aria-label="DoomQL benchmark curves by database and row count"></svg>
    <div class="tooltip" id="tooltip"></div>
  </section>
  <section class="legend" id="legend" aria-label="Engine visibility"></section>
  <section>
    <div class="table-head">
      <h2>Measured Values</h2>
      <span class="table-meta" id="table-meta"></span>
    </div>
    <div class="table-scroll"><table id="results-table"></table></div>
  </section>
</main>
<script id="curve-data" type="application/json">__DOOMQL_DATA__</script>
<script>
(() => {
  const data = JSON.parse(document.getElementById('curve-data').textContent);
  const svg = document.getElementById('chart');
  const tooltip = document.getElementById('tooltip');
  const colors = [
    '#f0c44f', '#73d08b', '#43b7b1', '#8ec5ef', '#d68dd8', '#a4a4e9',
    '#f4896d', '#f2f0df', '#df9f52', '#d3d873', '#db77a0', '#72a7e2'
  ];
  const colorBySystem = new Map(data.systems.map((system, index) => [system.id, colors[index % colors.length]]));
  const labelBySystem = new Map(data.systems.map(system => [system.id, system.label]));
  const state = {
    metric: 'median_ms',
    xScale: 'linear',
    yScale: 'linear',
    enabled: new Set(data.systems.map(system => system.id)),
  };
  const metricMeta = {
    median_ms: { label: 'Warm median', unit: 'ms' },
    p95_ms: { label: 'P95 latency', unit: 'ms' },
    first_ms: { label: 'Cold latency', unit: 'ms' },
    fps: { label: 'Frames per second', unit: 'fps' },
  };
  const ns = 'http://www.w3.org/2000/svg';

  function el(name, attrs = {}, text = '') {
    const node = document.createElementNS(ns, name);
    for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, value);
    if (text) node.textContent = text;
    return node;
  }

  function formatRows(value) {
    if (value >= 1e9) return `${value / 1e9}B`;
    if (value >= 1e6) return `${value / 1e6}M`;
    if (value >= 1e3) return `${value / 1e3}K`;
    return String(value);
  }

  function formatValue(value, metric = state.metric) {
    if (value == null || !Number.isFinite(Number(value))) return '-';
    const number = Number(value);
    if (metric === 'fps') return `${number < 10 ? number.toFixed(2) : number.toFixed(1)} fps`;
    if (number >= 1000) return `${(number / 1000).toFixed(number >= 10000 ? 1 : 2)} s`;
    return `${number.toFixed(number >= 100 ? 1 : 2)} ms`;
  }

  function tickValue(value) {
    if (state.metric === 'fps') return value >= 10 ? value.toFixed(0) : value.toFixed(1);
    if (value >= 1000) return `${(value / 1000).toFixed(value >= 10000 ? 0 : 1)}s`;
    if (value >= 100) return value.toFixed(0);
    if (value >= 10) return value.toFixed(1);
    return value.toFixed(2);
  }

  function valuesFor(system) {
    return data.points
      .map(point => ({
        rows: point.rows,
        point,
        result: point.results[system],
        value: Number(point.results[system]?.[state.metric]),
      }))
      .filter(item => item.result?.status === 'ok' && Number.isFinite(item.value) && item.value > 0);
  }

  function niceMax(maxValue) {
    if (maxValue <= 0) return 1;
    const magnitude = 10 ** Math.floor(Math.log10(maxValue));
    const normalized = maxValue / magnitude;
    const nice = normalized <= 1 ? 1 : normalized <= 2 ? 2 : normalized <= 5 ? 5 : 10;
    return nice * magnitude;
  }

  function renderChart() {
    svg.replaceChildren();
    const width = Math.max(320, svg.clientWidth || 1200);
    const height = Math.max(460, svg.clientHeight || 560);
    svg.setAttribute('viewBox', `0 0 ${width} ${height}`);
    const compact = width < 600;
    const margin = compact
      ? { top: 24, right: 14, bottom: 54, left: 48 }
      : { top: 24, right: 34, bottom: 58, left: 76 };
    const innerWidth = width - margin.left - margin.right;
    const innerHeight = height - margin.top - margin.bottom;
    const active = data.systems.filter(system => state.enabled.has(system.id));
    const allValues = active.flatMap(system => valuesFor(system.id).map(item => item.value));
    const minRows = Math.min(...data.scales);
    const maxRows = Math.max(...data.scales);
    const minValue = Math.min(...allValues, 1);
    const maxValue = Math.max(...allValues, 1);
    const yTop = state.yScale === 'log' ? maxValue * 1.18 : niceMax(maxValue * 1.06);
    const yBottom = state.yScale === 'log' ? Math.max(minValue / 1.25, 0.001) : 0;

    const x = rows => {
      if (maxRows === minRows) return margin.left + innerWidth / 2;
      const position = state.xScale === 'log'
        ? (Math.log(rows) - Math.log(minRows)) / (Math.log(maxRows) - Math.log(minRows))
        : (rows - minRows) / (maxRows - minRows);
      return margin.left + position * innerWidth;
    };
    const y = value => {
      const position = state.yScale === 'log'
        ? (Math.log(value) - Math.log(yBottom)) / (Math.log(yTop) - Math.log(yBottom))
        : (value - yBottom) / (yTop - yBottom);
      return margin.top + innerHeight - position * innerHeight;
    };

    const yTicks = [];
    if (state.yScale === 'log') {
      const start = Math.floor(Math.log10(yBottom));
      const end = Math.ceil(Math.log10(yTop));
      for (let power = start; power <= end; power += 1) {
        const value = 10 ** power;
        if (value >= yBottom && value <= yTop) yTicks.push(value);
      }
    } else {
      for (let index = 0; index <= 5; index += 1) yTicks.push(yTop * index / 5);
    }
    yTicks.forEach(value => {
      const position = y(value || 0);
      svg.append(el('line', { x1: margin.left, y1: position, x2: width - margin.right, y2: position, class: 'grid-line' }));
      svg.append(el('text', { x: margin.left - 12, y: position + 4, 'text-anchor': 'end', class: 'tick-label' }, tickValue(value)));
    });
    data.scales.forEach(rows => {
      const position = x(rows);
      svg.append(el('line', { x1: position, y1: margin.top, x2: position, y2: height - margin.bottom, class: 'grid-line' }));
      svg.append(el('text', { x: position, y: height - margin.bottom + 22, 'text-anchor': 'middle', class: 'tick-label' }, formatRows(rows)));
    });
    svg.append(el('line', { x1: margin.left, y1: height - margin.bottom, x2: width - margin.right, y2: height - margin.bottom, class: 'axis-line' }));
    svg.append(el('line', { x1: margin.left, y1: margin.top, x2: margin.left, y2: height - margin.bottom, class: 'axis-line' }));
    svg.append(el('text', { x: margin.left + innerWidth / 2, y: height - 13, 'text-anchor': 'middle', class: 'axis-label' }, `Source rows (${state.xScale})`));
    const yLabel = el('text', { x: 16, y: margin.top + innerHeight / 2, 'text-anchor': 'middle', class: 'axis-label', transform: `rotate(-90 16 ${margin.top + innerHeight / 2})` }, metricMeta[state.metric].label);
    svg.append(yLabel);

    active.forEach(system => {
      const values = valuesFor(system.id);
      if (!values.length) return;
      const color = colorBySystem.get(system.id);
      const pathData = values.map((item, index) => `${index ? 'L' : 'M'} ${x(item.rows).toFixed(2)} ${y(item.value).toFixed(2)}`).join(' ');
      svg.append(el('path', { d: pathData, stroke: color, class: 'curve' }));
      values.forEach(item => {
        const circle = el('circle', { cx: x(item.rows), cy: y(item.value), r: 5, fill: color, class: 'point', tabindex: 0 });
        const show = event => showTooltip(event, system.id, item);
        circle.addEventListener('mouseenter', show);
        circle.addEventListener('mousemove', show);
        circle.addEventListener('focus', show);
        circle.addEventListener('mouseleave', hideTooltip);
        circle.addEventListener('blur', hideTooltip);
        svg.append(circle);
      });
    });
  }

  function showTooltip(event, system, item) {
    const chartRect = svg.getBoundingClientRect();
    const sourceX = event.clientX || chartRect.left + Number(event.target.getAttribute('cx')) * chartRect.width / Number(svg.viewBox.baseVal.width);
    const sourceY = event.clientY || chartRect.top + Number(event.target.getAttribute('cy')) * chartRect.height / Number(svg.viewBox.baseVal.height);
    tooltip.replaceChildren();
    const name = document.createElement('div');
    name.className = 'tip-system';
    name.textContent = labelBySystem.get(system);
    const value = document.createElement('div');
    value.className = 'tip-value';
    value.textContent = formatValue(item.value);
    const meta = document.createElement('div');
    meta.className = 'tip-meta';
    meta.textContent = `${formatRows(item.rows)} rows | ${item.result.route || 'standalone'}`;
    tooltip.append(name, value, meta);
    tooltip.style.left = `${Math.min(sourceX - chartRect.left, chartRect.width - 205)}px`;
    tooltip.style.top = `${Math.max(42, sourceY - chartRect.top)}px`;
    tooltip.classList.add('visible');
  }

  function hideTooltip() { tooltip.classList.remove('visible'); }

  function renderLegend() {
    const legend = document.getElementById('legend');
    legend.replaceChildren();
    const latest = data.points[data.points.length - 1];
    data.systems.forEach(system => {
      const label = document.createElement('label');
      label.className = state.enabled.has(system.id) ? 'enabled' : '';
      const input = document.createElement('input');
      input.type = 'checkbox';
      input.checked = state.enabled.has(system.id);
      input.addEventListener('change', () => {
        if (input.checked) state.enabled.add(system.id); else state.enabled.delete(system.id);
        label.classList.toggle('enabled', input.checked);
        renderChart();
        renderTable();
      });
      const swatch = document.createElement('span');
      swatch.className = 'swatch';
      swatch.style.background = colorBySystem.get(system.id);
      const name = document.createElement('span');
      name.className = 'legend-name';
      name.textContent = system.label;
      const value = document.createElement('span');
      value.className = 'latest';
      value.textContent = formatValue(latest.results[system.id]?.[state.metric]);
      label.append(input, swatch, name, value);
      legend.append(label);
    });
  }

  function renderTable() {
    const table = document.getElementById('results-table');
    table.replaceChildren();
    const head = document.createElement('thead');
    const headerRow = document.createElement('tr');
    ['Engine', ...data.scales.map(formatRows)].forEach(text => {
      const th = document.createElement('th');
      th.textContent = text;
      headerRow.append(th);
    });
    head.append(headerRow);
    const body = document.createElement('tbody');
    data.systems.filter(system => state.enabled.has(system.id)).forEach(system => {
      const row = document.createElement('tr');
      const labelCell = document.createElement('td');
      const label = document.createElement('span');
      label.className = 'table-system';
      const swatch = document.createElement('span');
      swatch.className = 'swatch';
      swatch.style.background = colorBySystem.get(system.id);
      label.append(swatch, document.createTextNode(system.label));
      labelCell.append(label);
      row.append(labelCell);
      data.points.forEach(point => {
        const cell = document.createElement('td');
        cell.textContent = point.results[system.id]?.status === 'ok'
          ? formatValue(point.results[system.id][state.metric])
          : point.results[system.id]?.status || '-';
        row.append(cell);
      });
      body.append(row);
    });
    table.append(head, body);
    document.getElementById('table-meta').textContent = metricMeta[state.metric].label;
  }

  function bindSegments(id, stateKey) {
    const container = document.getElementById(id);
    container.addEventListener('click', event => {
      const button = event.target.closest('button[data-value]');
      if (!button) return;
      state[stateKey] = button.dataset.value;
      container.querySelectorAll('button').forEach(item => item.classList.toggle('active', item === button));
      if (stateKey === 'metric') renderLegend();
      renderChart();
      renderTable();
    });
  }

  const benchmark = data.benchmark;
  document.getElementById('subtitle').textContent = `${benchmark.world.toUpperCase()} | ${benchmark.frames} frames | ${benchmark.width}x${benchmark.height} | draw ${benchmark.draw_distance} | ${benchmark.render_type}`;
  document.getElementById('scale-count').textContent = data.scales.length;
  document.getElementById('engine-count').textContent = data.systems.length;
  document.getElementById('parity').textContent = data.all_parity_ok ? 'Frame parity verified' : 'Parity warning';
  if (!data.all_parity_ok) document.getElementById('parity').style.color = '#f4896d';
  bindSegments('metric-controls', 'metric');
  bindSegments('x-controls', 'xScale');
  bindSegments('y-controls', 'yScale');
  renderLegend();
  renderChart();
  renderTable();
  let resizeTimer;
  window.addEventListener('resize', () => {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(renderChart, 80);
  });
})();
</script>
</body>
</html>
'''


def render_html(payload: dict[str, Any]) -> str:
    data = json.dumps(payload, separators=(",", ":")).replace("</", "<\\/")
    return HTML.replace("__DOOMQL_DATA__", data)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "inputs",
        nargs="*",
        type=Path,
        help="compatible scale result JSON files (defaults to Episode 1 scale results)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=RESULTS / "episode1-scale-curves.html",
    )
    parser.add_argument(
        "--data-output",
        type=Path,
        default=RESULTS / "episode1-scale-curves.json",
    )
    args = parser.parse_args()
    paths = args.inputs or default_inputs()
    try:
        payload = load_curve_payload(paths)
    except ValueError as exc:
        parser.error(str(exc))

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.data_output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(render_html(payload), encoding="utf-8")
    args.data_output.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(
        f"Wrote {args.output} and {args.data_output} "
        f"({len(payload['scales'])} scales, {len(payload['systems'])} engines)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
