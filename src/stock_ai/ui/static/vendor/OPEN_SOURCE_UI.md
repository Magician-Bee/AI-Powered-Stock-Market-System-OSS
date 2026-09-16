# Open Source UI Assets

This static UI uses local files copied or adapted from downloaded repositories under `external/`.

## liquidGL

- Repository: https://github.com/naughtyduk/liquidGL.git
- Local path: `external/liquidGL`
- Verified HEAD: `2cef983b7fe593d3e0878dc78e5b79b47038a953`
- License: MIT
- Used files:
  - `external/liquidGL/scripts/liquidGL.js` -> `src/stock_ai/ui/static/vendor/liquidGL.js`
  - `external/liquidGL/scripts/html2canvas.min.js` -> `src/stock_ai/ui/static/vendor/html2canvas.min.js`
- Runtime use:
  - `src/stock_ai/ui/static/index.html` loads both scripts.
  - `src/stock_ai/ui/static/js/shell/glass.js` calls liquidGL for navigation, search, and control lenses.
  - The snapshot is a synchronized full-document clone of the active page plus the optical backdrop. Fixed glass chrome and foreground labels are excluded; scrolling changes WebGL UV coordinates every animation frame instead of waiting for another DOM capture.
  - Canvas charts are embedded in the document snapshot and later redraws update only their texture rectangle through `gl.texSubImage2D`, avoiding a full-page recapture.
  - The desktop Ultra profile renders at up to 3x device scale and extends the pinned shader with edge chromatic dispersion and pointer-responsive specular light.
  - The Settings view persists theme, language, density, quality, opacity, refraction, motion, and backdrop preferences locally and reapplies them before WebGL initialization.
  - Dedicated background lens nodes cover navigation and controls; their text and icons remain sharp above the WebGL canvas.
  - The original WebGL canvas is clipped directly to every lens; no 2D canvas copy is used.

## Glin UI

- Repository: https://github.com/glincker/glinui.git
- Local path: `external/glinui`
- Verified HEAD: `2e19376efbb601f915239ed9571b1c9a3fa9fb7e`
- License: MIT
- Referenced source files:
  - `external/glinui/packages/ui/src/components/glass-card.tsx`
  - `external/glinui/packages/ui/src/components/liquid-button.tsx`
  - `external/glinui/packages/ui/src/lib/use-liquid-glass.tsx`
- Runtime use:
  - `src/stock_ai/ui/static/vendor/glinui-theme.css` maps Glin UI glass-card and liquid-button tokens into this static app.
  - `src/stock_ai/ui/static/liquid-glass-system.css` consumes those tokens through reusable `GlassSurface`, `GlassButton`, `GlassGroup`, and `GlassNavigation` class contracts.
  - Glin UI's SVG filter port is retained as source reference but is not loaded at runtime; liquidGL is the only active refraction engine.

## DaftPlug Pure CSS iOS 26 Liquid Glass

- Collection: https://freefrontend.com/css-liquid-glass/
- Original CodePen: https://codepen.io/daftplug/pen/QwbaYGO
- Title: `Pure CSS iOS 26 Liquid Glass Effect`
- License reported by the collection: MIT
- Source technique:
  - `feTurbulence` with `baseFrequency="0.008 0.008"`, two octaves, and seed `92`.
  - A very small `feGaussianBlur` feeding `feDisplacementMap`.
  - The original displacement values are retained; the project adapter does not reconstruct them.
- Runtime integration:
  - All 16 exact full-page outputs are retained under `vendor/freefrontend-liquid-glass/pens/`.
  - `css/shell/freefrontend-liquid-glass.css` contains copied upstream layer, button and slider values.
  - `js/shell/freefrontend-liquid-glass.js` only attaches those complete effects to existing semantic UI.
  - The persistent top Agent composer is marked `data-liquid-preserve` and is intentionally excluded.
  - The refraction backdrop is the user's local
    `assets/liquid-glass-floral-background.png`; it is not copied from CodePen
    and does not require attribution or a network request.
