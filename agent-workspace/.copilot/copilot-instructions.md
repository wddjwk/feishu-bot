# FeishuBot agent workspace

You are working inside the isolated `agent-workspace` for a Feishu AI assistant bot.

Rules:

- Do not write credentials to source files.
- If you modify server code, validate it before reporting success.
- If a change requires service restart, write `pending_restart.json` in the project root and run `../manager deferred-restart 5`; never run direct restart from inside an AI response.
- Keep user-downloaded files under `download/`.
- Prefer small, reversible edits and mention any Feishu developer-console permissions or publishing steps that are required.
- For timer/scheduled-task requests, use `.claude/skill/feishu-scheduler/SKILL.md` and operate the local scheduler API instead of guessing JSON formats.
