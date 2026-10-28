# MCP bridge pilot

The bridge uses the existing authenticated console handlers, SessionManager,
queue, model selection, nonce high-water mark and rate budget. No new runner or
credential is introduced. The route remains gated by BOT_MCP_ENABLED. This
change does not deploy or activate it.

## Scope and operations

Projects are the intersection of BOT_MCP_PROJECTS and the registered project
list. Every label-resolving operation checks project and Claude-engine scope.
The list tools retain their existing behavior. Running cancellation remains
unavailable; queued cancellation uses the existing queue.

`ask` reserves the caller nonce and tracks one create-and-send operation.
After a known 429, retry the exact payload with the same nonce after the
advertised interval. Creation is not repeated after it succeeded. Internal
attempts use nonces above the shared high-water mark. An accepted turn cannot
be rerun; changed payloads and concurrent duplicates are refused. Uncertain
outcomes are not retried. Pending operation state is in memory, so retries
across bot restart are not guaranteed.

## Worker questions

An ask submission does not fabricate a pending question. A managed worker can
finish its turn with a line in its returned output:

    MCP_QUESTION {"id":"worker-chosen-id","question":"Which file?"}

Only returned worker output is parsed. Status exposes question IDs and text.
Answer binds to the original UUID, model, branch and worktree and submits a
continuation through the same queue and Claude --resume invocation. This is a
completed-turn clarification protocol, not a suspended CLI tool invocation.
Workers that do not emit this line have no correlated pending question. Old
question bindings are explicitly unknown after restart; foreign, already
answered and unavailable resume bindings are refused.

## Persistence and recovery

MCP-touched sessions are explicitly marked and the marker is persisted. Their
worktrees persist across turns. Unrelated sessions keep the ephemeral default
unless BOT_WORKTREE_REUSE=1 is explicitly configured. Disabling the route does
not enable captures or persistence for unrelated sessions; already-managed
sessions retain their recovery contract.

A managed resume must find its original worktree and transcript. If either is
missing, it explicitly refuses rather than rebuilding from a moving remote
branch or creating a new conversation. Automatic quarantine restoration is not
implemented.

Filtered and bounded report exports are separate from durable recovery.
`recovery_complete` is true only for a proven clean tree. Missing metadata,
failed status/enumeration and malformed records fail closed. Ignored content
also prevents a clean classification. Dirty managed trees remain on disk.

Quarantine resolves Git's actual gitdir, preserving index and HEAD plus a HEAD
bundle and binary staged delta before moving the raw tree. Unknown metadata,
locked/unmerged indexes and failed recovery copies refuse movement. Partial
recovery output is retained. Quarantine is outside report exports and may hold
secrets; it must never be published. Capture auto-pruning remains off by default.
GC skips unknown status or missing metadata, quarantines dirty trees and removes
clean registered trees only through successful Git removal, without rm fallback.

## Verification

Use /usr/bin/python3 on DevOps. Run pytest in a child shell, with an empty
environment, a temporary HOME and BOT_DATA_ROOT, a synthetic reports-token.env,
and `unshare --net`. No venv or package installation is necessary on the verified
host. The bridge fixture supplies its own registered projects, including a
registered project outside pilot scope. Tested selection:

    python3 -m pytest -p no:cacheprovider tests/test_mcp_bridge.py tests/test_mcp_recovery.py tests/test_worktree*.py -q

67 tests passed offline. No real-model or live-service validation was performed.

## Transactional installer and outstanding publisher integration

The installer now includes mcp_bridge.py, worktree-gc.py and the GC service
unit in its verified backup/restore manifest. The installed GC service runs
from the stable scripts directory, not the mutable Git checkout. It stops
the existing GC timer before bot quiescence, refuses an active or unknown GC
service, and restores the original timer activity after success or verified
rollback. Failed rollback leaves the timer stopped for investigation.

Do not update the current main checkout as a merge side effect: the currently
installed GC unit still points into it. Merge remotely and prepare the required
clean detached source independently; migrate the unit through this transaction
before updating that checkout. Preserve all existing untracked files and
quarantine directories.

The current production service uses a reviewed publisher-bootstrap.py and a
private application_sha256 pin. scripts/publisher-deploy.py accepts only the
inspected bootstrap digest, verifies trusted file ownership/modes and the old
application pin, and snapshots release-source identity. Unknown bootstrap
versions, configuration races and changed release/application bytes refuse.

The private publisher configuration joins the installer's backup manifest.
After installing the new application, only the digest field is atomically
replaced; all other bytes, ownership and mode are preserved. Before starting
the service, the actual publisher loader verifies the new bytes and constructs
the bot offline. The existing Codex-quiescence setting is preserved. Rollback
restores the old application and original configuration together, then verifies
them through the same loader before checking old-service health. The helper
never starts polling, IPC, lifecycle hooks or model calls during smoke checks.

The real-loader regression tests run in private mount/network namespaces with
synthetic files at the expected paths; they do not weaken the loader's trust
checks or touch live configuration. Production still requires valid GitHub PR
identity and the reviewed head/tree gates. No test result activates the bridge.

## Configured Claude readiness

The installer resolves the service PATH and uses the configured Claude executable.
Without gateway configuration it requires the existing subscription CLI's strict
loggedIn=true contract. A configured OpenRouter gateway must match the reviewed
wrapper digest and configuration paths; ownership/modes, endpoint, token routing
and models are checked before the same wrapper's version/help/auth probes.
GET /api/v1/key validates the existing gateway token without inference, rejects
redirects and exhausted explicit budgets, and never logs credentials or response
bodies. Only literal values and references to preceding literal variables are
accepted; shell commands and unresolved references refuse. Authentication failure
never triggers a subscription-to-gateway or gateway-to-subscription fallback.
