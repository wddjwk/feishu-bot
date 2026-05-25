---
name: feishu-self-update
description: Use this skill whenever the user asks the FeishuBot agent to modify, upgrade, fix, restart, redeploy, or update the bot itself. It gives the exact safe workflow for editing server code, validating changes, committing with the required version format, writing pending_restart.json, and using deferred restart so the Feishu reply is delivered before the process exits.
---

# FeishuBot self-update and safe restart

This skill is for changes to FeishuBot itself: server code, runtime config, cards, prompt building, AI runner behavior, scheduler behavior, manager script, workspace instructions, or skills. The bot is running the agent as a subprocess, so an immediate restart can kill the response before Feishu receives it. Use the delayed restart workflow.

## Mental model

The project root is one level above this workspace:

```text
../
├── manager
├── pending_restart.json
├── feishu-server/
└── agent-workspace/
```

The server process is managed by `../manager`. It supports:

```text
../manager start
../manager stop
../manager restart
../manager deferred-restart 5
../manager status
../manager log
```

For self-updates from inside a Feishu AI response, use only `../manager deferred-restart 5`.

## Safe update workflow

1. Understand the requested behavior and inspect the relevant files.
2. Make surgical edits under `../feishu-server`, `../manager`, `../AGENTS.md`, or `.` as needed.
3. Validate the changed behavior:
   - Python syntax: `cd ../feishu-server && PYTHONPATH=. python -m compileall -q .`
   - Unit tests if present: `cd ../feishu-server && PYTHONPATH=. python -m unittest discover -s tests -q`
   - JSON config: `cd ../feishu-server && python -m json.tool config.json >/dev/null`
   - Shell scripts: `cd .. && bash -n manager hooks/commit-msg`
4. Check Git status and avoid committing secrets or runtime data:
   - `.env`, `.venv/`, `.server.pid`, `feishu-server/data/`, `feishu-server/logs/`, `agent-workspace/download/`, and `pending_restart.json` must stay untracked.
5. Commit the update if the directory is a Git repo.
6. Write `../pending_restart.json` with the Feishu notification target if available.
7. Run `../manager deferred-restart 5`.
8. Return a concise Feishu answer explaining what changed and that a delayed restart was scheduled.

## Commit format

Commit subjects must start with a semantic version:

```text
v1.2.3 short description
```

Examples:

```text
v1.0.1 fix topic resume routing
v1.1.0 add scheduler task API
v2.0.0 change prompt protocol
```

Use a patch version for bug fixes and doc-only changes, minor for compatible features, major for breaking behavior.

If committing from an assistant environment that requires attribution, include:

```text
Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>
```

## pending_restart.json

Create this file in the project root before scheduling the deferred restart:

```json
{
  "receive_id_type": "open_id",
  "receive_id": "ou_xxx",
  "reply_text": "FeishuBot 已更新并完成延迟重启。"
}
```

Use the current Feishu sender/open_id when the prompt context provides it. If no reliable recipient is available, omit `receive_id`; the server may fall back to the configured maintainer if available.

Keep the message short because it will be sent as a restart-complete card after the new process starts.

## Restart rules

- Use `../manager deferred-restart 5` for self-updates.
- Do not run `../manager restart` from inside the AI response path.
- Do not kill the Python server process directly.
- Do not use scheduler shell tasks as a restart shortcut.
- If validation fails, do not restart; report the failing command and the relevant error.

## Quick command template

Use this shape after edits:

```bash
cd ..
cd feishu-server
python -m json.tool config.json >/dev/null
PYTHONPATH=. python -m compileall -q .
PYTHONPATH=. python -m unittest discover -s tests -q
cd ..
bash -n manager hooks/commit-msg
git status --short
git add <changed-files>
git diff --cached --check
git commit -m "v1.0.1 describe change" -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
printf '%s\n' '{"reply_text":"FeishuBot 已更新并将在几秒内完成重启。"}' > pending_restart.json
./manager deferred-restart 5
```

Replace the version and message with the actual change. Use `apply_patch` or normal editor tooling for file edits; do not create secrets in source files.

## Final answer shape

When done, report:

1. What changed.
2. What validation passed.
3. The commit version/hash if committed.
4. Whether deferred restart was scheduled.
