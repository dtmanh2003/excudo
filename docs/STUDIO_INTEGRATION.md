# Excudo Studio integration

This checkout uses reviewed main (`a0b9aae`) plus Keichan #52/#53 (`fc6aade`) as its backend. The previous prototype remains on local branch `zun` (`c1ea272`); the working integration is branch `excudo_future_finished`.

## Look from kodama

The visual files in `excudo/app/ui/` are the same as on branch `kodama`: `style.css`, `theme.js`, `logo.js`, `logo-watermark.svg`, `layout.js`, `utilities.js`, `responsive.js`, `branch-workspace.js`, `file-navigator.js` and `project-map.js`.

`app.js`, `studio.js`, `index.html`, `kodama-compat.js` and `studio-integration.js/.css` stay on this branch's versions, because kodama's `app.js` is written for kodama's backend:

- Kodama expects other stream events (`answer_delta`, `reasoning_delta`, `status`, `receipt`) than this backend sends.
- Kodama's approval button does not send the extra confirmation required after external content, so those approvals would never complete.
- Kodama's receipt upload calls the old Keichan functions; this branch uses `excudo/keichan/` through the `領収書フォルダー` button.
- `index.html` keeps `'unsafe-eval'` in its CSP: pywebview needs it to build the JavaScript bridge (`tests/test_desktop_csp.py`).

## Preserved from zun

- Studio file tree, text editor, tabs, safe manual saves and conflict detection.
- Local HTML/CSS/JavaScript preview, document/image previews, flexible docked/floating panels and branding.
- Manual change journal and release notes. Agent changes and undo/rollback use main's conversation-scoped tools, not the old persistent undo engine.
- Selected project test/lint/format/run-file and Git workflows, rebuilt using main's Tool/approval/cancellation contract. Every project/Git command requires approval; native execution is not an OS sandbox.
- Workspace switching keeps the editor, model prompt, file tools, approvals, attachments and session storage on the same root. Switching while busy is refused; unsaved editor changes require the existing UI confirmation.
- Local launcher settings in `.excudo/launcher-env.json` (`EXCUDO_*` keys only). Existing Keichan shortcuts now open the same Excudo application.

## Reviewed behavior retained

Main's permission classes, deletion warning, secret masking, tool preparation, cancellation, file-change tracking and session/image handling are retained. Keichan uses `excudo/keichan/` and the same configured model, with human-readable approval details, summarized skips, completed-image progress, configurable account categories and monthly Excel export.

No old standalone Keichan application or duplicate receipt tool set is registered. `keichan_data/` and other local data are retained untouched and ignored by Git; their format is not silently converted to `receipt_data/`. Choose the source images explicitly when processing in the new workflow.

## Run

Double-click `Excudo.bat`, or:

```powershell
uv sync --locked --dev --extra desktop --extra keichan --extra studio
uv run --locked --extra desktop --extra keichan --extra studio python -m excudo.app
```

Select the working folder in Studio. Use `+` > `領収書フォルダー` for receipts. Review the target month and applicant name in the conversation. Exports are under `receipt_data/exports/` in the selected workspace.

The tools of branch `zun_loc` were ported later onto main's Tool, PermissionGate and cancellation contract, without its permission/runtime stack: code intelligence, `secret_scan`, `get_logs`, `inspect_image`, `search_docs`, `check_command`, Git remote (`git_fetch`/`git_pull`/`git_push`/`git_revert`) and CI checks are always available; host terminals only with `EXCUDO_ALLOW_HOST_TERMINAL=1` (Windows); the Docker / SQLite / API / dependency / security-scan utility (`excudo/utilities/dev`) only with `EXCUDO_UTILITIES=dev`. Not ported: zun_loc's planning tools (they rely on its harness verification layer) and its QEMU VM terminal and sandbox backend; `run_command` and `grep_search` are covered by `bash_exec` and `grep`. The browser tools (open, read, click, fill) are included.

## Validation

Run the full Python suite and `node --test tests/test_desktop_rendering.js`. Studio workspace, viewer and preview-server tests were ported from zun. Additional integration tests verify main's gate, editor context, workspace switching and project execution. Local browser QA connects the real Python StudioApi and checks file edits, preview, receipt selection and tool catalog. Native desktop and real-model checks must be distinguished from this test bridge.

## React / Ink CLI

The frontend comes from `pr/ink-unified` (`48b8d56`). Run `Excudo-Ink.bat`, or `uv run --locked --extra keichan python -m excudo.launcher --app ink`. Install its pinned frontend dependencies using `pnpm --dir cli install --frozen-lockfile`.

Ink uses the same current Harness, Tool registry, PermissionGate, sessions and Keichan as Studio. The JSON-lines bridge forwards common tool progress and human-readable receipt approvals, keeps deletion warnings red, and retains the explicit confirmation rules. `+` and GUI file picking belong to Studio; Ink uses `/attach "path"`, `/paste`, `/attachments` and `/detach`. A question can follow `/attach` on the next line. Thinking text is hidden unless `/think` is enabled. Pipes never grant approval.
