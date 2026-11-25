# Security integrations

## Markdown tool boundary

The text-tool fallback uses markdown-it-py (declared directly and locked) to
identify CommonMark code blocks, inline code, block quotes, lists and HTML examples.
Tool tags overlapping these regions are kept as text. Ambiguous inline source
mapping suppresses the containing paragraph. Native structured tool calls are
unchanged; all actual calls still pass through PermissionGate.

Run the integration tests with:

```powershell
uv run --locked python -m pytest tests/test_markdown_tool_boundary.py tests/test_harness_loop.py tests/test_agent_core_hardening.py -q
```

## CI checks

`security-checks.yml` adds independent jobs on pushes and pull requests:

- Gitleaks v8.30.1 scans all fetched Git history with the built-in rules extended
  by `.gitleaks.toml`. Output is fully redacted; tests are not globally excluded.
  Historical synthetic credentials may need individual review. Do not hide real
  findings or add a blanket baseline to make the job green.
  `.gitleaksignore` lists ten reviewed historical synthetic-test fingerprints,
  scoped to exact commit/path/rule/line; it does not exempt any entire directory.
- Semgrep CE 1.181.0 uses local `.semgrep.yml` rules to reject dynamic Python
  execution, explicitly disabled TLS verification, and selected unsafe YAML loads.
  These are targeted checks, not a complete security audit. Static fixtures in
  `.semgrep-tests/` verify rule detections before scanning application source.
  Metrics and version checks are disabled; no hosted Semgrep account is required.

The scanner jobs use Linux runners, with read-only repository permission and
version-pinned tools. Gitleaks mounts the checkout read-only and scans with its
container network disabled. Container image retrieval still needs runner network.
CI is activated when the workflow is pushed; required branch checks must be set
in repository settings separately.

These changes do not install Presidio/Tesseract, OPA, mTLS, or a command sandbox.
Those are separate integrations with additional runtime/model requirements.

## Verification on 2026-10-16

- Full Python suite with temporary workspaces outside the application tree and
  loopback/hard-link support: **1099 passed, 2 failed, 2 skipped**. The initial
  sandboxed run was environment-limited and is not the readiness verdict.
- Remaining failures: `test_huge_single_line_file_is_read_with_bounded_memory`
  observed roughly 40 MB peak allocation for a 20 MB single line (expected under
  2 MB); `test_code_is_not_masked_by_mistake` masks `api_key = SETTINGS_KEY` in
  Python source as though the reference were a literal credential.
- All 146 selected regression/parser tests passed; all 4 JavaScript tests passed.
- Gitleaks scanned 186 commits: zero unignored findings after reviewing the ten
  synthetic fixture detections. No real credential values are stored in the
  exception file or printed in scanner logs (`--redact=100`).
- Semgrep rule fixtures: **3/3 passed**. The workflow targets the fixture file
  explicitly to avoid directory-to-config mapping ambiguity in Semgrep's test runner.
  The initial application scan found zero matches with the three targeted rules;
  this is not a claim of comprehensive SAST coverage. Application scanning uses
  explicit source paths and `--no-git-ignore` to include newly added source files.
- Project lock validation passed offline. Scanner binaries are installed only in
  the ignored project cache; they are not application runtime dependencies.

That initial run was not fully green. The later selective integration with
tool-security repaired both Python regressions: **1248 passed, 2 skipped,
0 failed**, plus 4 passing JavaScript tests. See `TOOL_SECURITY_COMPARISON.md`
for source revisions, selected controls, additional tools and deferred components.
Raw local reports are under `.pytest_cache/` and are not committed.
