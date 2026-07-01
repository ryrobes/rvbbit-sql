# Flint: A Visualization Language for the AI Era

A Microsoft Research project

Flint is a visualization intermediate language that lets AI agents reliably create expressive, good-looking charts from simple, human-editable chart specs. Instead of requiring verbose low-level parameters such as scales, axes, spacing, and layout, the Flint compiler derives optimized chart settings from the data, semantic types, chart type, and encodings. Flint supports 46 chart types, and it supports rendering in Vega-Lite, ECharts, and Chart.js.

> Install Flint with [npm](https://microsoft.github.io/flint-chart/#/documentation/getting-started#javascript-typescript) (TypeScript / JavaScript).

> To use Flint in agent workflows, check the [MCP server](https://microsoft.github.io/flint-chart/#/mcp).

> Explore 46 chart types and 83 examples in the [gallery](https://microsoft.github.io/flint-chart/#/gallery).

[Explore Gallery](https://microsoft.github.io/flint-chart/#/gallery) [Get MCP Server](https://microsoft.github.io/flint-chart/#/mcp) [Visit GitHub](https://github.com/microsoft/flint-chart)

Flint spec [Open in editor →](https://microsoft.github.io/flint-chart/#/editor?g=Omni%3A%20Line&i=0 "Open this example in the editor")

```
{
  "data": {...},
  "semantic_types": {
    "period": "YearMonth",
    "totalUsers": "Quantity",
    "gameType": "Category",
    "region": "Category"
  },
  "chart_spec": {
    "chartType": "Line Chart",
    "encodings": {
      "column": "region",
      "x": "period",
      "y": "totalUsers",
      "color": "gameType"
    },
    "baseSize": {
      "width": 300,
      "height": 600
    }
  }
}
```

Compiled chart

Vega-LiteEChartsChart.js

**Faceted line chart.** Monthly active users by region, laid out as small multiples over time.

# How it works?

Flint starts with a compact spec: the data, semantic types, and the chart spec. From there, the compiler produces a complete backend-native spec (shown here in Vega-Lite) filling with the necessary low-level details and renders a good-looking chart.

[Read the docs](https://microsoft.github.io/flint-chart/#/documentation/overview)

Flint spec

```
{
  "data": {...},
  "semantic_types": {
    "game": "Category",
    "period": "YearMonth",
    "newUsers": "Profit"
  },
  "chart_spec": {
    "chartType": "Heatmap",
    "encodings": {
      "x": "period",
      "y": "game",
      "color": "newUsers"
    },
    "chartProperties": {
      "colorScheme": "redblue"
    }
  }
}
```

Compiled spec (Vega-Lite)

```
{
  "data": {...},
  "mark": {
    "type": "rect",
    "width": 47
  },
  "height": {
    "step": 29
  },
  "encoding": {
    "x": {
      "field": "period",
      "type": "temporal",
      "scale": {
        "nice": false,
        "domain": [\
          "2024-12-16T19:38:10.909Z",\
          "2025-12-16T04:21:49.090Z"\
        ]
      }
    },
    "y": {
      "field": "game",
      "type": "nominal",
      "sort": null
    },
    "color": {
      "field": "newUsers",
      "type": "quantitative",
      "scale": {
        "scheme": "redblue",
        "domain": [\
          -84108,\
          84108\
        ],
        "domainMid": 0
      }
  ... // 16 more lines
```

Visualization

Flint spec

```
{
  "data": {...},
  "semantic_types": {
    "game": "Category",
    "period": "YearMonth",
    "newUsers": "Profit"
  },
  "chart_spec": {
    "chartType": "Heatmap",
    "encodings": {
      "x": "period",
      "y": "game",
      "color": "newUsers"
    },
    "chartProperties": {
      "colorScheme": "redblue"
    }
  }
}
```

Compiled spec (Vega-Lite)

```
{
  "data": {...},
  "mark": {
    "type": "rect",
    "width": 47
  },
  "height": {
    "step": 29
  },
  "encoding": {
    "x": {
      "field": "period",
      "type": "temporal",
      "scale": {
        "nice": false,
        "domain": [\
          "2024-12-16T19:38:10.909Z",\
          "2025-12-16T04:21:49.090Z"\
        ]
      }
    },
    "y": {
      "field": "game",
  ... // 30 more lines
```

Visualization

## 1.Specify with semantic types

Flint uses semantic types to capture meanings of data fields (e.g., Rank, YearMonth, Delta, Temperature), and uses them to infer the low-level chart configuration like parsing, scale, axes, formatting and color schemes.

Compiled chart

Flint spec

```
{
  "semantic_types": {
    "game": "Category",
    "period": "YearMonth",
    "newUsers": "Profit"
  },
  "chart_spec": {
    "chartType": "Heatmap",
    "encodings": {
      "x": "period",
      "y": "game",
      "color": "newUsers"
    },
    "chartProperties": {
      "colorScheme": "redblue"
    }
  }
}
```

For this heatmap of net new users gains by game and month, Flint determines the temporal value parser, axis formatting, and diverging color scheme and midpoint based on the semantic types of the fields.

## 2.Automatic layout optimization

Flint optimizes the chart layout based on an elastic layout model and banking principles. The compiler dynamically manages sizing, spacing, and arrangement so the chart nicely fits into the canvas.

Dense · 22 × 3

Sparse · 5 × 3

As the grouped bar chart number increases, Flint stretches the canvas and reduces the band width so the dense version still fits the canvas nicely, similar to how springs settle into an expandable container.

## 3.Easy to generate and adapt

Without fragile low-level parameters, Flint specs can be easily generated and adapted by users. Changing a chart design requires only switching the chart type and rebinding visual encodings, and the compiler cascades the new encoding choices to the low-level settings.

Pyramid

Faceted bar

The user can easily turn a faceted bar chart of the 2000 U.S. Census population distribution by gender and age into a pyramid chart by switching the chart type. The compiler handles the rest.

## 4.Render with different backends

Flint supports 46 chart types across Vega-Lite, ECharts, and Chart.js, with 83 backend-specific examples in the gallery. Despite their different APIs and programming models, Flint hides them behind a unified interface. The user can easily switch to different backends and leverage their unique features.

ECharts sunburst

Vega-Lite faceted bar

Vega-Lite has no native sunburst, but the user can easily switch to ECharts. The sunburst chart is a better alternative than the grouped bar chart for visualizing the hierarchy of region × gameType × game.

## Start building with Flint.

Open source and ready to use. Start from GitHub or browse examples in the gallery.

[View on GitHub](https://github.com/microsoft/flint-chart) [Browse the gallery](https://microsoft.github.io/flint-chart/#/gallery)