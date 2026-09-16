# Context Exposure Matrix

- Baseline commit: `94e8cb2d65c9f5d8f8de04717847d77c513cee27`
- Generated at: `2026-07-29T19:18:01.417729+00:00`
- Status: normative target plus confirmed baseline violations.

| Task kind | Allowed by default | Forbidden by default | Confirmed baseline risk |
|---|---|---|---|
| General | prompt, necessary conversation history | market, portfolio, Git, trading | keyword router and UI state can attach a stock symbol |
| Project | repository, Git, project tools | paper account, stock data | Host classifier is single-label and context is assembled centrally |
| Market information | explicit SymbolContext, read-only market tools | project files, execution | symbol fallback may inject `2330.TW` |
| Market decision | explicit Universe/Symbol, portfolio, risk | project tools | rule confidence and target/stop are produced before a model call |
| UI | UI state and UI tools | stock context without a referential phrase | selected symbol is promoted to active intent |
| Current information | current/web tools | paper account, project files | broad tool categories can be exposed through Host classification |

## Required controls

- Represent intent as multiple hypotheses with negative constraints.
- Represent every symbol as `SymbolContext`; `source=none` must remain valid.
- Treat UI selection as candidate context until the user refers to it.
- Move context selection into an auditable Context Broker.
- Record the context fields and tool capabilities exposed to each model invocation.
