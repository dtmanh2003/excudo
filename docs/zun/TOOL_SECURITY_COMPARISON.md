# Selected integration: zun and tool-security

Compared `zun` at `f4883dd2ed72711c5c87d7b4b03b811036141901` with
`tool-security` at `2b6fffa489c248d9c6f1dc705380e5a3af9eb6d1`.
This is a selective source integration, not a wholesale Git merge. Changes are
local on zun; no new commit or push was performed for this integration.

## Architecture and selection

The branch-side change relative to the shared ancestor spans 113 files and about
35,000 added lines. It combines CLI workflow state, policy, source intelligence,
CI inspection, and execution isolation. Zun has the current desktop/editor,
Keichan utilities, CommonMark tool boundary, checked storage, and security CI.
Replacing core modules wholesale would replace contracts used by the desktop.
The dependency/database/container/API tools already substantially overlap with
the implementations under `excudo/utilities/dev`; duplicating them would create
two competing permission and execution paths.

| Area | Selected implementation | Reason |
| --- | --- | --- |
| Desktop, editor, Keichan, sessions | Keep zun | Preserve existing application and receipt flows. |
| Text tool parsing | Keep zun + markdown-it-py | Existing tested CommonMark source ranges protect examples; native tool dispatch still uses PermissionGate. |
| Tool argument validation | Adapt tool-security | Reject invalid types, required fields, enums, excessive nesting/items and nonfinite numbers before permission/handler dispatch. Preserve schema-defined additional-property behavior and validate its value schema. |
| Child-process overrides | Adapt tool-security's exact key/value allowlist | Prefix filters admitted arbitrary package-manager hook settings. Known safe values remain; UV environment paths must remain in the workspace. This does not make host execution a sandbox. |
| CI inspection | Port four read-only tools | Local GitHub/GitLab inspection, assertions, includes and workflow-risk findings; no provider requests or CI execution. |
| Source intelligence | Port six read-only tools and Python AST parser | Symbols, structure, definitions, references, call graph and index status without importing project code. Results report unresolved/dynamic limitations and bounded completeness. |
| Read-only path checks | Adapt to zun's existing checked storage | Refuse traversal, links, Windows device names and oversized reads without importing the other branch's global runtime configuration. |
| Untrusted provenance | Keep zun Harness and extend it | New CI/source results are local untrusted project data. They do not acquire system/user authority and suspend auto-approval like other untrusted results. |
| Redaction, patch/undo, security CI | Keep zun safeguards | Preserve previous regression fixes and Gitleaks/Semgrep integration. |
| Large single-line reads | Repair zun's streaming implementation | Bound readline fragments rather than reading a whole huge line before truncating. Retain offset, universal-newline and UTF-8 behavior. |
| Python constant references | Repair zun with source context | Preserve `api_key = SETTINGS_KEY` in Python code; unquoted configuration values still pass through ordinary secret masking. |

## Adaptations discovered while porting

- The other branch's `security.py` is a static scan tool module; zun's file with
  that name is the central redactor. The ports use `scans` explicitly rather
  than overwriting the central security module.
- CI redaction originally substituted a setting such as `LANG=en` inside every
  word, corrupting `backend`, `confidence`, and schema keys. Short literals now
  require word boundaries, structural keys remain intact, and recognizable
  credentials are still sanitized. Added a regression test.
- Source discovery excludes `.uv-cache`, personal agent metadata and scanner
  fixtures. It reports scope/limit failures rather than silently claiming a
  complete index. TypeScript and other unimplemented parsers remain unsupported.
- The desktop's active workspace is reflected through the existing dev config
  adapter. Adapted cache tests switch the same workspace binding as the app.
- Ported offline module tests replace the other branch's workflow/Harness
  imports. New integration tests cover registry, schema rejection before
  approval, workspace denial, environment-hook denial and untrusted history.

## Deliberately deferred components

- The QEMU/native/legacy-VM execution broker is not enabled or copied as a
  nominal sandbox. It requires a provisioned runtime, Windows token/storage
  policy and backend preflight; its defaults also change which commands can
  run. These need a separate end-to-end execution integration.
- Planner/repair-session state and verification-bound workflows are coupled to
  the other branch's large Harness and different Permission/PolicyDecision
  contracts. They were not used to replace the desktop conversation loop.
- The other LLM transport imports `httpx2` and depends on runtime configuration
  absent in zun. It was not spliced into the OpenAI client without transport
  compatibility tests. No HTTPS/mTLS or OPA integration is claimed here.
- No installer, invoice sample, stale phase document, or duplicate utility
  implementation was copied just because it existed on the other branch.

## Tools and activation

Available unique tools increase from **76 to 86**: core 40, dev 39, Keichan 4,
and change-history 3. Dev contains the original 29 plus:

- `ci_info`, `ci_workflows`, `ci_validate`, `ci_check`;
- `code_symbols`, `code_structure`, `code_definition`, `code_references`,
  `code_call_graph`, `code_index_status`.

Default activation is unchanged: **47 active tools** with Keichan. To expose
the dev group, configure `EXCUDO_UTILITIES=keichan,dev` and restart Excudo.
No local `.env` or credentials were modified.

## Validation

147 additional selected/adapter tests are included (133 ported offline cases,
14 integration cases). The original regression cases remain enabled.

- Full Python suite: **1248 passed, 2 skipped, 0 failed** in 140.78 seconds.
  Tests used an isolated temporary directory outside the application checkout,
  with loopback and hard-link support. The previously failing huge-line memory
  and Python-reference masking tests now pass.
- JavaScript: **4 passed**.
- Semgrep: **0 findings**, 3 local rules over 53 Python source files. This is
  targeted SAST coverage, not a general security certification.
- Gitleaks: **0 findings** on the current diff plus newly added source/tests.
- Python syntax and Git whitespace checks passed. No dependency was added.

JUnit: `.pytest_cache/tool_security_merged.xml`. Scanner output:
`.pytest_cache/tool_security_semgrep.json`. These ignored local reports are not
committed. The comparison and selective implementation remain local for review.
