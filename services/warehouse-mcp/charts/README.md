# Optional RVBBIT chart runtime

`rvbbit-tanstack-charts-0.3.1.js` is a pinned, self-contained browser bundle.
It exposes `window.RVBBIT_CHARTS` for artifacts that explicitly opt in. The
default Warehouse dashboard template continues to use Chart.js and does not
load this file.

The bundle contains TanStack Charts, the D3 scales/curves used by the starter,
and a thin `mountRvbbitChart()` adapter. The adapter leaves native TanStack
definitions intact while adding stable DOM metadata and selection events for
Artifact Lens. See `../examples/tanstack-charts-dashboard.html` for a complete
framework-free example.

Rebuild after changing `charts-runtime/src/entry.ts`:

```sh
cd services/warehouse-mcp/charts-runtime
npm ci
npm run typecheck
npm run build
```

The filename is versioned intentionally. Existing artifact versions must keep
rendering against the runtime they were authored with.

