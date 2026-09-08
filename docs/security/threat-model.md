# Stock AI production threat model

Status: implementation baseline; production security sign-off is still required.

## Scope and data flow

```text
User / Computer Use
        |
        v
Local UI -> API boundary -> Agent runtime -> Host capability validator
   |             |                  |                 |
   v             v                  v                 v
market data   durable stores    model provider    paper/broker gateway
```

The trust boundaries are the user-to-UI boundary, UI-to-API boundary, API-to-
Agent boundary, Agent-to-provider boundary, and Agent-to-broker boundary.
Market and news payloads are data-only and never gain instruction authority.

## STRIDE register

| Threat | Asset / boundary | Mitigation in this repository | Residual evidence |
| --- | --- | --- | --- |
| Spoofing | API and provider identity | scoped credentials, signed automation callbacks, capability checks | production service identity and rotation receipt |
| Tampering | orders, model/data receipts, audit history | durable stores, content hashes, immutable audit records, release provenance payload | off-host immutable sink and signed release receipt |
| Repudiation | Agent decisions and order lifecycle | durable run events, trace context, order/audit lineage | production reconstruction drill |
| Information disclosure | provider credentials and user/account context | macOS Keychain, redacted bounded runtime history, secret scanning | production vault/HSM and hosted scan receipt |
| Denial of service | API, providers, broker and scheduler | rate limits, circuit breakers, timeouts, kill switches | staging fault-injection and on-call receipt |
| Elevation of privilege | Agent tool and order boundary | Host-only authorization, explicit execution permission, broker route permission | production RBAC identity review |

## Release conditions

The system remains fail-closed for live execution until the residual evidence in
the table is attached to the release ledger and reviewed by the security owner.
