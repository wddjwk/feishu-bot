---
name: feishu-scheduler
description: 用户要求创建、查看、修改、运行或删除 FeishuBot 定时任务时使用。通过本地调度器 API 管理 script 与 agent 两类任务。
---

# FeishuBot 定时任务

调度器地址通常为 `http://127.0.0.1:8066`。通过 API 操作任务，不要手改 `feishu-server/data/tasks.json`。

## 先选任务类型

- 优先选择 `script`：对于比较固定的任务，如一些定时提醒、检查清理等，优先思考是否能用scirpt完成。因为scirpt更稳定可靠，更快，不消耗token。
- 再考虑使用 `agent`: 如果任务需要推理、归纳、搜索、调研等需要AI能力的操作，则需要你撰写可靠的、专业的、对口的prompt，去驱动这个定时任务的顺利准确完整。

## 任务结构

公共字段：

```json
{
  "id": "lowercase-id",
  "name": "任务名称",
  "type": "script 或 agent",
  "cron": "分 时 日 月 周",
  "enabled": true,
  "timeout_seconds": 120,
  "target": {"receive_id_type": "open_id", "receive_id": "ou_xxx"},
  "payload": {}
}
```

Cron 支持 `*`、`*/N`、`N-M`、`N,M,K`、`N`；例如 `0 9 * * *` 表示每天 09:00。

## Script 任务

脚本必须位于 `agent-workspace/scheduler/<task-id>/` 内、含 shebang 且可执行。每个任务只使用自己的目录；先创建并验证脚本，再创建任务：

```json
{
  "type": "script",
  "payload": {
    "script": "scheduler/disk-check/disk-check.sh",
    "args": ["/home/shuaikai"]
  }
}
```

不要使用 `payload.command`、管道、重定向、`&&` 或 `shell` 类型；把固定逻辑写入 `scheduler/<task-id>/` 下受审查的脚本。旧 `shell` 任务会迁移为 `scheduler/<task-id>/legacy.sh`。

## Agent 任务

`payload.prompt` 要写清：目标、输入范围、完成标准、输出格式和禁止事项。不要写“处理一下”“看看情况”这类模糊 prompt。

```json
{
  "type": "agent",
  "timeout_seconds": 300,
  "payload": {
    "prompt": "汇总今日项目变更。仅依据指定数据源，按项目输出：进展、风险、待办；没有数据时明确说明。"
  }
}
```

未指定时，任务固定使用 `model.json` 的 `default_cli` 和该 CLI 的 `default_model`，不受聊天中临时 `/cli`、`/model` 切换影响。

如需固定 CLI 或模型，在 payload 中记录。`tool` 必须是已配置 CLI；`model` 可用该 CLI 的别名或模型 ID：

```json
{
  "payload": {
    "prompt": "生成工作日晨报，使用三条要点。",
    "tool": "claude",
    "model": "max"
  }
}
```

`POST /api/tasks` 创建；`PUT /api/tasks/<id>` 可局部修改 `payload.prompt`、`payload.tool`、`payload.model`，未提供的嵌套字段会保留。用 `GET /api/tasks/<id>` 确认保存后的实际配置。

## API

```text
GET    /api/health
GET    /api/tasks
POST   /api/tasks
GET    /api/tasks/<id>
PUT    /api/tasks/<id>
DELETE /api/tasks/<id>
POST   /api/tasks/<id>/run
```

创建或修改后，必要时运行 `POST /api/tasks/<id>/run` 验证。最终报告任务 ID、自然语言时间、类型、目标用户、CLI/模型（Agent 时）和是否已手动运行。

## 安全

- 不在名称、prompt、参数或输出中放入密钥。
- 脚本路径不得离开工作区。
- 自更新不能走定时任务；修改机器人后按 `feishu-self-update` 技能延迟重启。
