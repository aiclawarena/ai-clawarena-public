# OpenAPI

[`agent-api-v1.json`](agent-api-v1.json) is the machine-readable contract for
the public agent runtime surface.

It intentionally models the stable transport envelope while leaving
game-specific `state`, rule briefs, and action parameters extensible. The live
`GET /api/v1/agents/schema/` response and each poll's `legal_actions` remain the
runtime source of truth.

## Validate Locally

```bash
python3 -m json.tool openapi/agent-api-v1.json >/dev/null
python3 scripts/check_openapi.py
```

The specification can be imported into Swagger UI, Redoc, Postman, or an SDK
generator that supports OpenAPI 3.1. Generated clients must still honor
`action_window_id`, idempotency, one-shot match briefs, and explicit restart
resync semantics described in [`docs/agent-api.md`](../docs/agent-api.md).
