# Excudo core adversarial testing — 2026-10-16

Scope: Excudo core only (Harness, permission checks, secret redaction, file reads and checked storage). No PR, production-code changes, live LLM requests, external network requests or Keichan business tests were added. All secrets and file contents are fictional. The shared test fixtures still initialize the project's normal isolated workspace.

## Results

- New matrix: **109 cases; 87 passed, 22 failed**.
- Existing selected core suite: **212 passed, 1 skipped** (Windows symlink permissions).
- Failures remain ordinary failing assertions, not hidden behind xfail/skip.
- 22 failures represent eight grouped defects, not 22 independent vulnerabilities.

## Confirmed defects

| Priority | Defect | Evidence and impact | Suggested repair |
| --- | --- | --- | --- |
| P1 | JSON Unicode escapes bypass redaction in ordinary files | A password encoded with JSON `\uXXXX` escapes survives both redaction and Harness output. Decoding the JSON recovers the fake credential. | Mask decoded structure and cover the original escaped representation; preserve ordinary source formatting only when safe. |
| P1 | Partial reads of ordinary config files lose credential context | A whole `credentials` object is masked; requesting only its `value` line leaks the fake credential in both valid JSON and YAML files. | Analyze/mask the full bounded configuration before applying offset/limit, regardless of filename. |
| P1 | Unquoted KEY = VALUE secrets mistaken for code references | `DB_PASSWORD = EXCUDO_CANARY_...` in an ordinary configuration file is left intact by the code-reference heuristic. Confirmed through Harness. | Distinguish configuration from source code; do not infer a variable reference solely from spaces around `=`. |
| P1 | Multiline string credentials survive ordinary-file masking | Triple-quoted password values in TOML and Python remain in Harness output. | Use bounded TOML/source-literal analysis without executing code; fail closed where a credential literal cannot be safely masked. |
| P1 | Indented Markdown examples become tool calls | A four-space-indented example is parsed as an action. The integration test records an actual handler invocation using a harmless recorder. | Recognize Markdown indented code or remove ambiguous text-to-tool execution. |
| P1 | Longer Markdown fences can close prematurely | A four-backtick code block containing three-backtick lines exposes the enclosed example to the tool-call parser. Confirmed with the recorder integration test. | Match opening fence character and length, and only accept a valid closing fence. |
| P2 | CR-only line endings break read_file ranges | ASCII, Japanese and Vietnamese files using `\r` return out-of-bounds for a valid second line. | Recognize universal newlines while maintaining bounded reads. |
| P2 | Unterminated final line is omitted from line counts | A two-line file without a final newline is reported as having one line when requesting an out-of-bounds offset. | Count the final nonempty line independently of a trailing newline. |

P1 denotes a fix priority here, not a CVSS score. Tool-example failures do **not** bypass PermissionGate: the recorder tests explicitly approve the harmless tool. Secret tests show disclosure into the agent pipeline, not exfiltration to an attacker. Network, authentication and production deployment were not assessed in this test addition.

## Reproduce

```powershell
$env:PYTHONUTF8 = '1'
.\.venv\Scripts\python.exe -m pytest tests/test_excudo_adversarial_matrix.py -q --tb=short
```

Test groups cover structured secrets and aliases, escaped values, partial reads through Harness, malformed model tool calls, Markdown examples, Unicode/newline boundaries, checked-open modes, exact byte limits and traversal attempts under auto-approval. There are no commands that access real company data or start shell actions inside the new test matrix.

## Evidence

JUnit XML artifacts are stored outside the repository in the task visualization directory:

- `excudo-oct16-final.xml`: the complete new matrix.
- `excudo-oct16-baseline.xml`: the selected existing core suite.

Production source was deliberately left unchanged: this task requested tests and bug discovery. The newly added matrix makes the full suite red until the listed defects are fixed.

## Follow-up review of current working tree

The historical results above describe the original run. The current working tree already
contains edits to `harness.py`, `security.py` and `tools.py`; this follow-up did not edit
production source.

New reproducers: `tests/test_review_followup.py` — **5 failed, 1 passed**.
These are three additional failure behaviors plus a regression test for the previously
reported inaccessible range after 50 KB (not five independent defects):

- **P1 — Invalid Markdown fence closure executes an example.** Both backtick and tilde
  blocks close on a line with trailing text, such as `~~~still_inside_code`, because
  `_EXAMPLE_RE` permits arbitrary non-newline suffixes after a closing fence. The harmless
  recorder is actually invoked through Harness. Require only spaces/tabs after a closing
  fence. PermissionGate still applies; this is not a permission bypass.
- **P2 — A complete small range is falsely labelled truncated.** Reading line 1 with
  `limit=1` from `first\n` followed by more than 50 KB returns the complete line plus a
  truncation notice. Decide truncation from the requested range, not total file size.
- **P2 — A valid UTF-8 line near the byte boundary becomes U+FFFD.** A 49,998-byte ASCII
  line followed by a newline and `日\n`, read with `offset=2, limit=1`, returns the replacement
  character instead of `日`. The file prefix is cut inside the character before selecting
  the range. This shares the prefix-budget implementation with the inaccessible-offset
  regression; do not count these as independent root causes.

Reproduce with:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_review_followup.py -q --tb=short --basetemp=.pytest_review_followup_run
```

Only synthetic files and a harmless approved recorder are used. The passing control
confirms a genuine text tool call still parses. Failing tests are intentionally visible.

## Wider project review

The existing Python test suite was run across `tests` (excluding the prior follow-up
file and debug script): **951 passed, 74 failed, 32 skipped, 9 setup errors** in 93 seconds.
Do not interpret this as 83 application defects. Many failures are environment-limited:
loopback connections/processes and hard-link creation return permission errors, Git
fixtures cannot push to their temporary local remote, and project-switch tests use
temporary directories inside the app installation tree (which the app refuses).
The run used `--basetemp=.pytest_deep_review`; that placement also changes the expected
Git error for a directory with a parent repository. These cases need a suitable environment
before a product diagnosis. No live LLM testing was performed.

Independent new reproducers in `tests/test_deep_review.py`: **5 failed, 1 passed**.
The two existing JavaScript test files were also run with Node's test runner:
**4 passed**, covering upload routing and receipt-event ordering.

| Priority | Confirmed defect | Reproduction / impact | Repair direction |
| --- | --- | --- | --- |
| P1 | Valid patch corrupts the preceding line | Original bytes `a\nold`, with a standard no-final-newline marker on the deleted `old` line and an added `new\n`, become `anew\n`. Tool reports success. | A no-newline marker on a deleted line must not strip the preceding output/context line's newline. |
| P2 | edit_file destroys invalid UTF-8 bytes outside the edit | `keep=\xff\nold\n` edited from `old` to `new` replaces `\xff` with UTF-8 U+FFFD and reports success. | Decode strictly and refuse unsupported text before writing, as apply_patch already does. |
| P2 | Multi-file registration exceeds the change-record cap | With the cap scaled to 1, a single two-file call records 2 changes. `register` checks only existing records and extends them after the loop. | Include pending records when checking the cap. |
| P2 | Multi-file snapshots exceed the total memory budget | With total budget scaled to 4 bytes, two 3-byte originals retain 6 bytes. `snapshot` does not reserve room between files. | Deduct pending snapshots from the shared remaining budget. |
| P2 | Opening a damaged saved chat raises AttributeError | A valid session JSON with `history: [null]` passes the file-level checks; `Api._open_session` calls `.get` on the raw entry, even though Harness sanitizes its own copy. | Reject malformed entries or use the sanitized history throughout the app. |

Scaled limits test the production accounting with tiny synthetic files, without allocating
50 MB or creating hundreds of records. The passing patch control covers ordinary
newline-terminated edits. No production source was changed during this review.

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_deep_review.py -q --tb=short --basetemp=.pytest_deep_repro_run
```

## Persistence, skills and dev-status follow-up

`tests/test_review_persistence_and_skills.py`: **6 failed, 1 passed**, representing
four additional defects (three malformed skill cases share one defect).
The existing session, skills and workspace suites still pass: **28 passed**.

| Priority | Confirmed defect | Evidence / impact | Repair direction |
| --- | --- | --- | --- |
| P1 | Auto-generated chat titles persist recognized credentials | A fake AWS access-key ID is masked in saved message history, but remains verbatim in the saved title produced by `Api._run_turn`. `session._masked` only masks history, while titles are copied from the first user message. | Mask title metadata too, including legacy titles when listing/displaying sessions. |
| P2 | A malformed skill breaks registry construction | Missing closing delimiter, YAML list metadata, or invalid YAML each raises from `_build_load_skill_tool`. This function is called at module import when constructing REGISTRY, so one bad installed skill can prevent startup. | Validate delimiters, require mapping metadata and valid string fields, and isolate parse failures per skill. |
| P2 | Failed dev tools display an OK status | The real dev `_wrap` serializes `ToolResult(success=False)` as JSON. `_tool_summary` only recognizes text starting with `Error`, `Unknown tool` or `Action rejected`, and returns `ok` for this JSON. The app uses that status for its tool event/error flag. | Preserve structured success information across the wrapper/UI boundary or recognize its JSON result schema. |
| P2 | Editor validation happens after an invalid overwrite | `Workspace.save` accepts NUL in content, writes it, records it, then raises from `read` because that method rejects binary content. The original file has already been overwritten even though the save reports failure. `create` already checks NUL before writing. | Apply the same content validation before save writes or updates the journal. |

The title test runs the app turn with MockModel and reads the actual isolated session
JSON; no live model or external service is used. The skill tests use a temporary skill
directory, and the dev test uses a side-effect-free handler through the production
wrapper. A passing control verifies valid skills still build and plain-text errors are
recognized as failures.

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_review_persistence_and_skills.py -q --tb=short --basetemp=.pytest_cache/review_persistence_run
```

## Rollback continuity and result serialization

`tests/test_review_rollback_and_results.py`: **2 failed, 1 passed**.
Existing patch/undo tests: **13 passed, 1 skipped** (hard-link availability).

- **P1 — Rollback silently removes intervening manual edits.** Start with `original`,
  record agent edit to `agent-one`, manually replace the content with `manual-user-edit`,
  then record another agent edit to `agent-two`. `rollback` reports success and writes
  `original`, discarding the intervening user work too. It compares the current state
  only with the last after-state, without checking that successive records form a
  continuous chain (`previous.after == next.before`). Reject the discontinuity before
  writing; individual undo of the latest edit can still restore the captured manual
  version. The control confirms continuous agent-only edits roll back normally.
- **P2 — Oversized ToolResult metadata can cause an infinite truncation loop.**
  `ToolResult(True, stdout='x', metadata={'large': 'y' * 25000}).to_model_message()`
  never finishes. After stdout becomes the truncation marker, the loop repeatedly
  replaces it with the same nonempty marker. The `if not value` fallback is never
  reached, while the oversized metadata remains. Require progress on every iteration
  and bound metadata/error fields as well as stdout/stderr. A separate subprocess
  printed READY before entering the serializer and was killed after a 5-second timeout;
  the same oversized metadata with empty stdout completes through the fallback.

The serializer defect is confirmed at the shared helper boundary; no claim is made
that every built-in dev operation produces this metadata size (several have their own
smaller limits). The subprocess is terminated by the test timeout, so no infinite-loop
process is left behind. All file operations use synthetic isolated files.

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_review_rollback_and_results.py -q --tb=short --basetemp=.pytest_cache/review_rollback_run
```
