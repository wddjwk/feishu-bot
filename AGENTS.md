# FeishuBot

FeishuBot is a self-built Feishu application robot. It receives `im.message.receive_v1` over the official long-connection SDK, builds context-aware prompts from direct messages, reply chains, topics, and attached files, invokes a configured AI CLI, then replies with Feishu Card JSON 2.0.

## Conversation semantics

- Direct messages and normal reply-chain messages reply back to the current conversation with `reply_in_thread=false`; they start a fresh AI session.
- Only messages that already have `thread_id` are topic follow-ups. Those messages resolve the previous AI `session_id` from the topic/root bot card mapping, call the CLI with `--resume`, and reply with `reply_in_thread=true`.
- Bot reply message IDs are persisted so a user-created topic on that card can resume later; thread IDs are persisted only after an actual topic follow-up.

## Feishu configuration

- Enable bot capability for the self-built app.
- Subscribe to `im.message.receive_v1`.
- Grant message send/read/history and reaction permissions needed by the APIs used here.
- Ensure target users are in app availability, and publish the app version after capability or permission changes.
- Put credentials in `.env`, never in tracked files.

## Runtime

Use `./manager start|stop|restart|deferred-restart|status|log`. `manager start` creates `.venv`, installs Python dependencies with a China PyPI mirror, installs the commit hook if the directory becomes a Git repo, and starts the server with `setsid`.

Self-updates must use `./manager deferred-restart 5`, not direct restart, so the AI response can be sent before the process exits.

Default runtime configuration is `claude + deepseek-v4-flash` with a 36000 second analysis timeout. AI subprocesses run from `agent-workspace`, not from the project source directory.

Scheduler usage is documented for agents in `agent-workspace/.claude/skill/feishu-scheduler/SKILL.md`.

## Official docs checked

- Server SDK overview and Python SDK preparation.
- Bot overview.
- Receive message event `im.message.receive_v1`.
- Send/reply message APIs.
- Get/list message APIs.
- Message resource download API.
- Add/delete message reaction APIs.
- Feishu Card JSON 2.0 structure, markdown, plain text, divider, collapsible panel.
