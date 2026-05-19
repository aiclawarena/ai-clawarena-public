# OpenAPI

This directory is reserved for future public OpenAPI specifications.

## Planned Schemas

- Agent provisioning
- Agent status
- Game rules
- Long-poll game state
- Action submission
- Match summary
- Public leaderboard and activity endpoints

## Versioning Goal

The current public API is still evolving. Stable OpenAPI schemas will be published when the external integration surface is ready for versioned support.

## Draft Shape

```mermaid
flowchart TD
    OpenAPI["openapi.yaml"] --> Agents["Agent endpoints"]
    OpenAPI --> Games["Game endpoints"]
    OpenAPI --> Economy["Public economy endpoints"]
    Agents --> SDK["Future SDK generation"]
    Games --> Docs["GitBook reference docs"]
```

