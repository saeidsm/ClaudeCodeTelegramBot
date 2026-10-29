# Local ChatGPT provider candidate

This candidate is not activated. Configuration and release approval do not prove
upstream entitlement, model behavior, or ChatGPT client discovery.

## Explicit routing

The candidate provider is `chatgpt-local`, executable
`/root/.local/bin/claude-gpt`, env_file
`/root/.claude-gpt-bridge/bridge.env`. Default/sonnet read BRIDGE_MODEL;
opus reads BRIDGE_OPUS_MODEL; haiku reads BRIDGE_FAST_MODEL.
Reviewed model IDs are gpt-6.1-terra, gpt-6.1-sol and gpt-6-luna.
Readiness pins the reviewed wrapper digest, validates trusted file modes, literal
settings and port 8317, then checks the complete bounded local model catalog.
HTTP proxy environment variables and redirects are disabled. No paid fallback
is used. Catalog success is not proof of inference or subscription entitlement.

Provider identity is persisted per conversation. A created conversation with an
unknown or different provider cannot silently resume on this new provider. Start
a new conversation; do not relabel an existing conversation to bypass this guard.

## Existing MCP contract (not a new public endpoint)

Private URL: http://172.17.0.1:9092/mcp/rpc. Authentication uses the existing
session API Bearer token; no token belongs in this document, a URL, or Git.
JSON-RPC POST: initialize (protocolVersion 2025-06-18), then
notifications/initialized, then tools/list. Discover the authoritative schemas.
Tool invocation: tools/call with name and arguments.

- projects_list, models_list, sessions_list: no arguments.
- session_new: label, project; optional engine=claude and model.
- send: label, message, nonce. Existing session resumes.
- ask: label, project, message, nonce; optional model. Creates and sends once.
- status: label; optional full. Returns state, output and model metadata.
- answer: label, question_id, message, nonce, bound to a worker-emitted question.
- report: label. Builds a ZIP and therefore is not a read-only health probe.
- cancel: label. Only queued work can be cancelled.

Default project scope is Rag4Engineers and the bridge is Claude-only. Use
models_list to select aliases or IDs. Nonces increase strictly; never replay an
uncertain operation. A known ask 429 permits the documented identical-payload
retry. Status/answer bind to actual worker output, not fabricated question IDs.

## Remaining integration boundary

The separate NikBot authenticated facade currently exposes submit_job/resume_job
with a claudecode target and routes to its own queue. Its runner configuration
still points to the bare Claude executable; changing the Telegram provider does
not alter it. The investigating conversation does not expose native tools for
the private bridge. A skill documents usage; it does not create authentication
or register a connection. Do not expose the private endpoint or distribute its
credential to work around this gap.

## Activation and rollback requirements

Review/merge the immutable candidate, run release tests, establish model and
subscription readiness, and coordinate the existing NikBot executor before
changing its runtime. Drain active work without interrupting it. Back up provider
configuration, runner configuration, immutable release references and publisher
pin. Preserve old sessions and quarantine. Activate only the approved scope.
Restore matching code/configuration and publisher pin together on failure;
verify service health and original timer state. Do not restore a stale queue DB
after newly admitted jobs. Verify Telegram and MCP independently with real
request/result/continuation evidence before declaring end-to-end completion.
