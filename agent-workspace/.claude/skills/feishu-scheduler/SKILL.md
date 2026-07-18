---
name: feishu-scheduler
description: Use this skill whenever the user asks this FeishuBot agent to create, list, inspect, run, update, or delete scheduled tasks/timers/reminders. It defines the local scheduler REST API, supported cron syntax, and the distinct agent and script task schemas.
---

# FeishuBot scheduler tasks

FeishuBot exposes a local scheduler API, normally at `http://127.0.0.1:8066`. Use the API rather than editing `feishu-server/data/tasks.json` by hand.

## Task selection

There are exactly two task types:

- `script`: Executes one existing executable script directly with argument-vector execution (`shell=False`). **Prefer this for fixed, simple, deterministic work**, such as checks, reports, file transforms, or reminders with known content.
- `agent`: Calls an AI CLI with a prompt. Use it only when the task needs reasoning, drafting, research, or other open-ended work.

The scheduler does not accept `shell` tasks or arbitrary command strings. This prevents command interpolation and keeps direct execution distinct from AI prompting.

## Safety and routing

- Verify scheduler health with `GET /api/health` before changing tasks when its state is uncertain.
- Do not put secrets in task names, prompts, script arguments, or task output.
- Script paths must be relative to `agent-workspace`, must resolve inside that workspace, and must be executable.
- For server self-updates, use the self-update workflow and `../manager deferred-restart 5`; never use a scheduler task as a restart shortcut.
- Send results to the Feishu user's `open_id` when it is available.

## Cron syntax

The scheduler evaluates five-field cron expressions once per minute:

```text
minute hour day_of_month month day_of_week
```

Supported field forms:

- `*`
- `*/N`
- `N-M`
- `N,M,K`
- `N`

Examples:

```text
0 9 * * *      every day at 09:00
*/30 * * * *   every 30 minutes
0 18 * * 1-5   weekdays at 18:00
15 10 1 * *    monthly on day 1 at 10:15
```

## REST API

```text
GET    /api/health
GET    /api/tasks
POST   /api/tasks
GET    /api/tasks/<id>
PUT    /api/tasks/<id>
DELETE /api/tasks/<id>
POST   /api/tasks/<id>/run
```

## Creating an AI task

Use `agent` only when a prompt needs AI reasoning:

```bash
curl -sS -X POST http://127.0.0.1:8066/api/tasks \
  -H 'Content-Type: application/json' \
  -d '{
    "id": "daily-summary",
    "name": "每日总结",
    "type": "agent",
    "cron": "0 9 * * *",
    "enabled": true,
    "timeout_seconds": 300,
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

`payload.tool` and `payload.model` are optional; omitting them uses the current AI configuration.

## Creating a script task

Create and validate the script first. It must live under `agent-workspace`, have a shebang, and be executable:

```bash
mkdir -p scripts
cat > scripts/disk-check.sh <<'SH'
#!/usr/bin/env bash
set -euo pipefail
df -h "$@"
SH
chmod 700 scripts/disk-check.sh
```

Then register it as a `script` task. The scheduler invokes the script directly; it never passes the payload through a shell:

```bash
curl -sS -X POST http://127.0.0.1:8066/api/tasks \
  -H 'Content-Type: application/json' \
  -d '{
    "id": "disk-check",
    "name": "磁盘检查",
    "type": "script",
    "cron": "0 */6 * * *",
    "enabled": true,
    "timeout_seconds": 120,
    "target": {
      "receive_id_type": "open_id",
      "receive_id": "ou_xxx"
    },
    "payload": {
      "script": "scripts/disk-check.sh",
      "args": ["/home/shuaikai"]
    }
  }'
```

Do not use `payload.command`, pipes, redirects, `&&`, or a `shell` task type. Put any required fixed shell logic inside the reviewed script instead.

## Listing, updating, running, and deleting

```bash
curl -sS http://127.0.0.1:8066/api/tasks
curl -sS http://127.0.0.1:8066/api/tasks/daily-summary
curl -sS -X POST http://127.0.0.1:8066/api/tasks/daily-summary/run
curl -sS -X DELETE http://127.0.0.1:8066/api/tasks/daily-summary
```

`PUT /api/tasks/<id>` accepts a partial update and preserves unspecified nested `target` and `payload` values:

```bash
curl -sS -X PUT http://127.0.0.1:8066/api/tasks/daily-summary \
  -H 'Content-Type: application/json' \
  -d '{
    "cron": "30 9 * * 1-5",
    "payload": {
      "prompt": "请总结工作日早晨需要关注的项目事项。"
    }
  }'
```

Legacy `shell` tasks are migrated once at scheduler startup into executable scripts under `agent-workspace/.scheduler-legacy/`. Review the generated script before relying on it.

After creating or changing a task, report its ID, schedule in plain language, type (`script` or `agent`), target recipient, and whether a manual run was performed.
