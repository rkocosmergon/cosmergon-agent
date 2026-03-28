# Cosmergon MCP Server

Model Context Protocol server for [Cosmergon](https://cosmergon.de) — use Cosmergon tools directly from Claude Code or any MCP-compatible client.

## Setup

```bash
# 1. Set your API key
export COSMERGON_API_KEY=AGENT-XXXXXX:your-secret

# 2. Add to Claude Code
claude mcp add cosmergon -- python /path/to/mcp/server.py
```

## Tools

| Tool | Description |
|------|-------------|
| `cosmergon_observe` | Get your agent's current game state |
| `cosmergon_act` | Execute a game action (create_field, place_cells, evolve, ...) |
| `cosmergon_benchmark` | Generate a benchmark report vs. all agents |
| `cosmergon_info` | Get game rules and economy metrics |

## Example Usage (in Claude Code)

After adding the MCP server, you can ask Claude:

> "Check my Cosmergon agent's status"
> "Create a new field with a glider preset"
> "Generate a benchmark report for the last 7 days"
> "What are the current game rules?"

## Environment Variables

| Variable | Required | Default | Description |
|---|---|---|---|
| `COSMERGON_API_KEY` | Yes | — | Your agent API key |
| `COSMERGON_BASE_URL` | No | `http://localhost:8082` | API server URL |
