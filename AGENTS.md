# FeishuBot

FeishuBot is a self-built Feishu application robot. It receives `im.message.receive_v1` over the official long-connection SDK, builds context-aware prompts from direct messages, reply chains, topics, and attached files, invokes a configured AI CLI, then replies with Feishu Card JSON 2.0.

## Conversation semantics

- Direct messages and normal reply-chain messages reply back to the current conversation with `reply_in_thread=false`; they start a fresh AI session.
- Only messages that already have `thread_id` are topic follow-ups. Those messages resolve the previous AI `session_id` from the topic/root bot card mapping, call the CLI with `--resume`, and reply with `reply_in_thread=true`.
- Every new AI request receives a generated session ID through the configured CLI `session_args`; its user message and bot card message IDs are persisted with the session's CLI and model.
- Bot reply message IDs are persisted so a user-created topic on that card can resume later; thread IDs are persisted only after an actual topic follow-up.
- The bot responds to every direct message. In groups, it only responds when the structured `mentions` payload targets its own open ID. Set `FEISHU_BOT_OPEN_ID` to avoid the bot-info lookup fallback.
- Session data is stored in a versioned JSON document with separate `sessions` and `links` indexes, atomic writes, one-time migration from the former flat entry list with a `.v1.bak` backup, and bounded retention. Tune its limits in `config.json`.

## Feishu configuration

- Enable bot capability for the self-built app.
- Subscribe to `im.message.receive_v1`.
- Grant message send/read/history and reaction permissions needed by the APIs used here.
- Ensure target users are in app availability, and publish the app version after capability or permission changes.
- Put credentials in `.env`, never in tracked files.

## Runtime

Use `./manager start|stop|restart|deferred-restart|status|log`. `manager start` creates `.venv`, installs Python dependencies with a China PyPI mirror, installs the commit hook if the directory becomes a Git repo, and starts the server with `setsid`.

Self-updates must use `./manager deferred-restart 5`, not direct restart, so the AI response can be sent before the process exits.

The active CLI and model are selected by `config.json` (currently `codebuddy + deepseek-v4-pro`) with a 36000 second analysis timeout. AI subprocesses run from `agent-workspace`, not from the project source directory.

AI tool command syntax stays in `config.json` because CLI flags differ by provider. `base_args` are common invocation flags, `session_args` and `resume_args` define conversation lifecycle flags, `prompt_transport` explicitly chooses `stdin` or a positional `argument`, and `output_parser` selects the supported stream format. `max_argument_prompt_bytes` prevents argument-mode CLIs from exceeding the OS argv limit. The runner logs the complete generated prompt, command, and raw output locally; logs rotate automatically.

Set `feishu.card_width` to `half` for Feishu's default 600px card width or `full` to use `width_mode: fill`. `/reload` applies width changes without a restart.

Scheduler usage is documented for agents in `agent-workspace/.claude/skills/feishu-scheduler/SKILL.md`. Safe self-update and deferred restart usage is documented in `agent-workspace/.claude/skills/feishu-self-update/SKILL.md`. The singular `agent-workspace/.claude/skill` path is kept as a compatibility symlink.

## Official docs checked

- Server SDK overview and Python SDK preparation.
- Bot overview.
- Receive message event `im.message.receive_v1`.
- Send/reply message APIs.
- Get/list message APIs.
- Message resource download API.
- Add/delete message reaction APIs.
- Feishu Card JSON 2.0 structure, markdown, plain text, divider, collapsible panel.
