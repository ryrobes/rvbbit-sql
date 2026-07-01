## The experience

Using Flint through MCP is a simple loop: connect the server, ask for the chart you want, and work with a visualization with dynamic widgets provided by the MCP server.

Agent chat

Show me quarterly revenue by region as a grouped bar chart.

AI

Here's an interactive Flint chart view — tweak it and send the spec back when it looks right.

called `create_chart_view`

Flint ChartMCP App

![Grouped bar chart: quarterly revenue by region](https://microsoft.github.io/flint-chart/assets/mcp-chart-preview-BSVczjrr.svg)

Corner radius2SortNone ▾Show values

Copy spec to chat

1. **Connect Flint MCP server.** Add the stdio server to your MCP client. The agent can chart local CSV, TSV, or JSON files by default.
2. **Ask for a chart.** The agent turns your request into one Flint spec, chooses the chart type and fields, then calls the MCP server to validate and render it.
3. **Review the interactive result.** In hosts with MCP Apps, the preferred tool opens a live SVG preview with chart options. When an artifact is needed, Flint can return a PNG, SVG, or compiled backend spec instead.

## What it provides

The server keeps the tool surface small: one preferred interactive tool, supporting tools for static render and validation, plus resources that teach the agent Flint's chart vocabulary.

tool · preferred

`create_chart_view`

Opens the interactive MCP App: live SVG preview plus chart options. Use this whenever the user wants to see a chart.

tool

`render_chart`

Returns a static PNG or SVG. Use it when the host has no App UI, or when the user asks for an image artifact.

tool

`compile_chart`

Returns the backend-native spec JSON for Vega-Lite, ECharts, or Chart.js, along with assembly warnings.

tool

`validate_chart`

Checks whether a spec is valid, reports warnings or errors, and returns the computed chart size.

tool

`list_chart_types`

Lists chart types and encoding channels, optionally scoped to one backend.

resource

`flint://agent-skill`

Bundled authoring instructions for producing valid ChartAssemblyInput specs.

resource

`flint://chart-types`

A browsable catalog of chart types and encoding channels across all backends.

prompt

`author_flint_chart`

Loads the Flint authoring skill in prompt-aware clients before chart tool calls.

## Install & configure

For manual setup, the server speaks **stdio** and runs zero-install with `npx`. Point your MCP client at the package:

```
{
  "mcpServers": {
    "flint": {
      "command": "npx",
      "args": ["-y", "flint-chart-mcp"]
    }
  }
}
```

Tool calls can embed rows directly with `data.values`. The agent can also chart a local CSV, TSV, or JSON file by `data.url` out of the box. Remote URLs are never fetched. For an untrusted deployment, pass `--disable-file-reference` to reject local file references and accept only inline `data.values`:

```
{
  "mcpServers": {
    "flint": {
      "command": "npx",
      "args": ["-y", "flint-chart-mcp", "--disable-file-reference"]
    }
  }
}
```

## Reference

The docs cover the full MCP workflow. The package README is the shortest reference for tool inputs, CLI flags, and client config.

[Read setup docs](https://microsoft.github.io/flint-chart/#/documentation/setup-flint-mcp) [GitHub README](https://github.com/microsoft/flint-chart/tree/main/packages/flint-mcp)