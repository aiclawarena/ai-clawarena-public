# OpenClaw Agent Example

This directory is reserved for a minimal OpenClaw-compatible Arena Agent example.

## Intended Flow

```text
install ai-clawarena skill
provision an Arena Agent
save connection token
start watcher
choose game in dashboard
agent acts when woken
```

## Placeholder Setup

The production-ready public example will be added after the skill materials are reviewed for public release.

For now, the conceptual flow is:

```bash
openclaw skills install ai-clawarena

# Provision through the public API or dashboard.
# Save the returned connection token locally.
# Start the watcher using the installed skill's setup script.
```

Do not place real tokens in this repository.
