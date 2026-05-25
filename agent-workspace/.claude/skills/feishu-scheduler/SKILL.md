---
name: feishu-scheduler
description: Use this skill whenever the user asks this FeishuBot agent to create, list, inspect, run, update, or delete scheduled tasks/timers/reminders. It explains the local scheduler REST API, cron syntax, agent-vs-shell task shapes, and safe operational rules, so the agent can operate FeishuBot timers correctly instead of guessing endpoint formats.
---

# FeishuBot scheduler tasks

This FeishuBot exposes a local scheduler HTTP API, normally on `http://127.0.0.1:8066`. Use it for user requests like “每天早上提醒我”, “定时跑这个 prompt”, “查看所有 timer”, “删除某个定时任务”, or “手动运行这个任务”.

## Safety and routing

- Prefer the REST API over editing `feishu-server/data/tasks.json` by hand.
- Use `GET /api/health` before changes if you are not sure the scheduler is running.
- Do not create tasks with secrets in prompts, names, or shell commands.
- For code changes made by the agent itself, use the project’s self-update workflow and `./manager deferred-restart 5`; do not use scheduler shell tasks as a restart shortcut.
- Send task results to the user’s Feishu `open_id` when available. If the request does not include a target user and the current message context has one, use that.

## Cron syntax

The scheduler checks once per minute. Supported crontab fields:

```text
minute hour day_of_month month day_of_week
```

Supported expressions in each field:

- `*` matches every value.
- `*/N` matches every N units, for example `*/10 * * * *`.
- `N-M` matches a range.
- `N,M,K` matches a list.
- Plain `N` matches one value.

Examples:

```text
0 9 * * *      every day at 09:00
*/30 * * * *   every 30 minutes
0 18 * * 1-5   weekdays at 18:00
15 10 1 * *    monthly on day 1 at 10:15
```

## REST endpoints

Base URL:

```text
http://127.0.0.1:8066
```

Endpoints:

```text
GET    /api/health
GET    /api/tasks
POST   /api/tasks
GET    /api/tasks/<id>
PUT    /api/tasks/<id>
DELETE /api/tasks/<id>
POST   /api/tasks/<id>/run
```

## Creating an agent task

Agent tasks run the configured AI CLI with a prompt, then send the answer back to Feishu.

```bash
curl -sS -X POST http://127.0.0.1:8066/api/tasks \
  -H 'Content-Type: application/json' \
  -d '{
    "id": "daily-summary",
    "name": "每日总结",
    "type": "agent",
    "cron": "0 9 * * *",
    "enabled": true,
    "target": {
      "receive_id_type": "open_id",
      "receive_id": "ou_xxx"
    },
    "payload": {
      "prompt": "请总结今天需要关注的项目事项。",
      "tool": "claude",
      "model": "deepseek-v4-flash"
    }
  }'
```

Required fields:

- `id`: stable unique ID, lowercase letters/digits/dashes recommended.
- `type`: `agent`.
- `cron`: five-field cron expression.
- `target.receive_id_type`: usually `open_id`.
- `target.receive_id`: Feishu recipient ID.
- `payload.prompt`: task prompt.

Optional fields:

- `name`: display name.
- `enabled`: defaults to true.
- `payload.tool`: `claude`, `qoder`, `copilot`, or `codebuddy`; omit to use current config.
- `payload.model`: concrete model or configured alias.

## Creating a shell task

Shell tasks run a local command and send stdout/stderr summary back to Feishu.

```bash
curl -sS -X POST http://127.0.0.1:8066/api/tasks \
  -H 'Content-Type: application/json' \
  -d '{
    "id": "disk-check",
    "name": "磁盘检查",
    "type": "shell",
    "cron": "0 */6 * * *",
    "enabled": true,
    "target": {
      "receive_id_type": "open_id",
      "receive_id": "ou_xxx"
    },
    "payload": {
      "command": "df -h /home/shuaikai/projects/FeishuBot"
    }
  }'
```

Keep shell commands narrow and non-destructive. Do not use broad kill commands, global package installation, or commands that mutate unrelated directories.

## Listing and inspecting tasks

```bash
curl -sS http://127.0.0.1:8066/api/tasks
curl -sS http://127.0.0.1:8066/api/tasks/daily-summary
```

In Feishu chat, `/timer` also lists tasks.

## Updating a task

Use `PUT` with the full updated fields you want to change:

```bash
curl -sS -X PUT http://127.0.0.1:8066/api/tasks/daily-summary \
  -H 'Content-Type: application/json' \
  -d '{
    "cron": "30 9 * * 1-5",
    "enabled": true,
    "payload": {
      "prompt": "请总结工作日早晨需要关注的项目事项。",
      "tool": "claude",
      "model": "deepseek-v4-flash"
    }
  }'
```

## Manually running a task

```bash
curl -sS -X POST http://127.0.0.1:8066/api/tasks/daily-summary/run
```

Manual runs are useful for verifying a new task before waiting for its cron time.

## Deleting a task

```bash
curl -sS -X DELETE http://127.0.0.1:8066/api/tasks/daily-summary
```

In Feishu chat, `/timer-rm daily-summary` also deletes a task.

## Response expectations

After any change, summarize:

1. The task ID.
2. The schedule in plain language.
3. The target recipient.
4. Whether a manual verification run was performed.
