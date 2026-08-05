# Warehouse appearance assets

`warehouse-theme.src.js` is the readable source. `warehouse-theme.js` is its
checked-in browser bundle, including `node-vibrant`; the Warehouse image has no
JavaScript build step at runtime.

`adaptive-artifact.js` is intentionally dependency-free and unbundled. It is
loaded by every hosted artifact shim but exits before touching the DOM unless
that immutable artifact manifest names a Design Profile whose
`behavior.theme_source` is `viewer`. Direct pages read the sanitized
`rvbbit-warehouse-adaptive-theme-v1` snapshot; sandboxed Calliope frames receive
the same shape through the validated parent bridge.

From the `rvbbit-sql` repository root, with the sibling `rvbbit-lens` checkout
installed:

```bash
NODE_PATH="$PWD/../rvbbit-lens/node_modules" \
  npx --yes esbuild@0.25.8 \
  services/warehouse-mcp/theme/warehouse-theme.src.js \
  --bundle --platform=browser --format=iife --target=es2022 --minify \
  --outfile=services/warehouse-mcp/theme/warehouse-theme.js
```

The 76 images under `images/` are intentionally standalone derivatives of the
Lens wallpaper library, not a live mirror. Full images fit within 1920×1080 and
thumbnail tiles are center-cropped to 420×260; both are stripped WebP files.
This keeps the copied library around 19 MB instead of shipping the roughly
171 MB source set. `VIBRANT-LICENSE` covers the bundled extractor.
