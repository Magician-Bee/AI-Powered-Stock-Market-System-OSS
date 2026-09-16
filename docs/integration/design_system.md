# Open Stock AI Design System

This project uses a single local UI layer under `src/stock_ai/ui/static`.

The requested design references are integrated as design principles and component
contracts instead of separate runtime apps:

- `shadcn/ui`: compact panels, 8px radius, strong borders, predictable form controls.
- `Magic UI`: shimmer, pulse, and scanning motion for active system states.
- `Aceternity UI`: dark depth, grid surface, and high-contrast landing-style polish without turning the app into a marketing page.
- `21st.dev Agent Elements`: pipeline steps, agent status cards, evidence rails, and explicit execution state.
- `Tailwind CSS`: tokenized spacing, colors, rings, and responsive grid behavior mirrored through CSS custom properties.
- `FreeFrontend / CodePen`: complete MIT source outputs are retained locally; production uses the original layer, button, and slider values rather than a visual approximation.

## Current implementation

- `index.html`
  - `design-strip`
  - `openStockFlowBox`
  - Open Stock AI workspace controls and evidence panes

- `css/core/`、`css/features/`、`css/shell/`
  - `--tw-*` local tokens
  - `.panel`, `.mini-card`, `.event`
  - `.magic-border`, `.pulse`, `.agent-step`
  - responsive `trade-grid` and `agent-flow`
  - one FreeFrontend source adapter for Web, Windows browser launchers, and the macOS WKWebView
  - the original `vEOpqMa` four-layer surface, `QwbaYGO` button displacement, and `VYLQJoy` range control

The persistent top Agent composer is a compatibility contract and carries
`data-liquid-preserve`; the FreeFrontend source adapter must not rewrite its current
glass appearance or its controls.

The optical backdrop follows the active interface theme: exchange night,
graphite, clarity blue, terminal green, aurora purple, pearl, or daylight.
Two project-owned 16:9 finance wallpapers provide flowing market ribbons,
chart rhythm, data nodes, highlights, and quiet negative space. Dark themes
reuse `market-intelligence-dark.jpg` through restrained color treatments;
light themes use `market-intelligence-light.jpg`. The optional soft-focus,
prism, depth-glow, chromatic-sheen, or spotlight treatment uses only broad
transparent light fields—never dots, grids, stripes, or tile overlays. This
gives transparent glass real optical detail
to refract without turning the interface into a decorative poster. The same
composed backdrop is included in the live sampling document for Web, Windows
launchers, and the macOS WKWebView.

All 16 complete CodePen full-page outputs from the referenced collection are
retained under `static/vendor/freefrontend-liquid-glass/pens/`. Project
selectors and platform constraints stay outside the vendored directory.

- `js/features/quant-research.js`
  - `renderOpenStockDecision`
  - `renderOpenStockFlow`
  - Storage and decision replay cards that display the active Open Stock AI
    schema contracts.
  - `loadOpenStockAIView`

## Audit contract

`GET /api/open-stock-ai/integration-audit` includes
`open_stock_ai.design_system_contract.v1`. The contract verifies that the
single local UI under `src/stock_ai/ui/static` contains the requested design
references:

- `shadcn/ui`: panel/card controls and compact bordered surfaces.
- `Magic UI`: `magic-border`, pulse, and shimmer motion.
- `Aceternity UI`: ambient grid/depth and polished dark surfaces.
- `21st.dev Agent Elements`: Open Stock AI flow steps and agent evidence rails.
- `Tailwind CSS`: local `--tw-*` tokens, grid tracks, and responsive rules.

## Integration rule

The UI must remain one system. New components should reuse these local tokens
and should not introduce a second front-end app, a separate design runtime, or
an external project that owns the screen flow.
