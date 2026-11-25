# Ink ↔ Python IPC v1

The Ink process is the terminal frontend. It spawns one Python core with
`python -u -m excudo.ipc`, without a shell. The core owns the existing Harness,
PermissionGate, tools and Session objects. The frontend cannot invoke a tool
directly or supply a permission classification. No TCP listener is introduced.

## Framing and startup

Each frame is one UTF-8 JSON object followed by LF; CRLF input is accepted.
`v` must be integer `1`. A frame is limited to 1 MiB on both sides. Python closes
the connection on an oversized or incomplete EOF frame. Invalid JSON receives
`protocol_error` without dispatching a command. Python stdout contains only frames;
diagnostics go to stderr, which the frontend drains without displaying raw values.

The Python process emits `ready` once initialized:

```json
{"v":1,"type":"ready","workspace":"/work/project","session_id":"...","auto_approve":false,"tools":[{"name":"read_file","title":"ファイル読取","description":"...","args":{},"example":"..."}]}
```

The frontend waits for ready before enabling chat. Startup failure, child exit,
unsupported frames or a broken channel produce a visible connection error.
The source-tree Python environment is the default; `--python`/`EXCUDO_PYTHON`
select another interpreter. The workspace is explicitly passed in the child's
environment, independently from the core's installation directory.

## Requests and acknowledgements

```json
{"v":1,"id":"17","method":"chat","params":{"text":"質問"}}
{"v":1,"type":"response","id":"17","ok":true,"session_id":"..."}
```

Request IDs are nonempty strings up to 64 characters. The last 4096 IDs are
remembered and duplicates refused. IDs correlate acknowledgements, not authorization.
Responses use `ok:false,error:"..."` on failure; invalid IDs are returned as null.
An acknowledgement for chat means accepted, not completed or persisted.

| Method | Parameters | Result |
|---|---|---|
| `chat` | `text`: nonblank string, ≤65536 Python characters | Start one worker |
| `cancel` | none | Request cancellation, deny pending approval |
| `approval.respond` | `approval_id`, boolean `allow`, optional `confirmation:"yes"` | One matching pending approval |
| `session.new` | none | Reset Harness, create session ID |
| `session.list` | none | Masked titles, IDs, update times |
| `session.load` | `session_id` (ID or unique prefix) | Sanitized user/assistant text, not raw system/tool history |
| `session.delete` | `session_id` | Refuse the active session, including its prefix |
| `auto.set` | boolean `enabled` | Existing file-modification-only auto policy |
| `shutdown` | none | Deny approval, request stop, close channel |

While a turn is active, only cancel, approval.respond and shutdown are accepted.
Python processes control frames on its reader thread while Harness runs on one
worker. A second worker is never started concurrently on the same Harness.

## Events and completion

All turn events have `request_id`, equal to the accepted chat request's ID:

```json
{"v":1,"type":"answer_delta","request_id":"17","text":"こんにちは"}
{"v":1,"type":"done","request_id":"17","reason":"completed","session_id":"..."}
```

Events are `reasoning_delta`/`answer_delta` (`text`), `tool_call` (`name`, masked
`arguments`, bounded masked `result`), `error`/`warning` (`content`),
`approval_request`, and terminal `done` (`reason`). Reasoning stays in frontend
memory, not the saved conversation. Tool notifications describe completed calls.
Session saving is attempted before done. Failure emits a warning but retains
the in-memory conversation. UI messages retain at most 100 entries; the canonical
conversation and compaction remain Python responsibilities.

## Approval boundary

```json
{"v":1,"type":"approval_request","request_id":"17","approval_id":"random-one-use-id","name":"write_file","arguments":"{\"path\":\"a.txt\",\"content\":\"new\"}","risk":"modify","requires_confirmation":false,"untrusted":false,"outbound_findings":[]}
{"v":1,"id":"18","method":"approval.respond","params":{"approval_id":"random-one-use-id","allow":false}}
```

Arguments displayed to the user are masked. Python keeps the original arguments
privately. The frontend shows the core's classification, untrusted-content warning
and outbound findings. Long arguments can be scrolled. Empty input denies; only
`y`/`yes` permits ordinary approval. If the gate requires confirmation after
untrusted content, Python additionally requires literal `confirmation:"yes"`.
Stale, mismatched, duplicate or nonboolean responses cannot authorize a tool.
PermissionGate still independently refuses sensitive/outside/dangerous operations.

Stop, stdin EOF, shutdown and output disconnection deny pending approval. Stop
wakes the approval worker and marks Harness cancelled before another model reply
can begin. A cancellation watcher also covers Stop during generation, including
the startup/reset race. The next turn waits for the preceding worker and its
cancellation call to finish. Cancellation retains existing core limits: it is a
request, not a guarantee of immediate network/process termination, and it does
not roll back completed file changes. Frontend shutdown waits a bounded interval
then terminates an unresponsive Python child; this is not a new OS sandbox.

## Terminal and noninteractive input

Only Ink renders the terminal. Model/file text is stripped of VT and control
sequences before display. Bracketed pasted newlines do not act as Enter keys.
In pipe mode each line waits for done, and all required approvals are denied.
The client rejects invalid/oversized Python frames rather than rendering them.

## Verification

- `tests/test_ipc.py`: real Harness/gate, frame validation, stream/save/resume,
  masked approvals, full confirmation, Stop, EOF and replay refusal.
- `cli/test/integration.test.js`: actual Node ↔ Python pipes, Unicode, saving,
  approvals/denial, pending-approval disconnect, cancellation and subsequent turn.
- `cli/test/ui.test.js`: real Ink rendering/input, default denial, Stop,
  bracketed paste, session switching, terminal-sequence filtering.

The existing Python renderer remains available with `python main.py`;
its tests remain compatibility tests, not evidence about the Ink renderer.
Image attachment and Playwright tools are separate tasks and are not added here.

## Additive integration fields

- `tool_progress` events carry the shared tool's `stage`, `current`, `completed`, `total`, `name` and optional `message`. They never enter saved/model history.
- `attachment.add` accepts a quoted path string under the workspace; `attachment.paste`, `attachment.list`, `attachment.clear` and `attachment.remove` (1-based `index`) use main's attachment validation. Responses contain filename metadata only. An empty chat is accepted only when images are pending.
- `approval_request.warning` describes irreversible deletion; RESTORE uses main's human-readable preview. Additional confirmation remains server-enforced.
- Resumed image messages show filenames, never raw image bytes or data URLs.
