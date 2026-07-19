---
name: feishu-self-update
description: 修改、升级、修复、重启或部署 FeishuBot 自身时使用。规定验证、提交和延迟重启流程。
---

# FeishuBot 自更新

项目根目录是工作区的上一级：`../`。机器人正在当前 Agent 进程中运行，直接重启会中断回复；自更新只能使用延迟重启。

```bash
../manager start|stop|restart|deferred-restart|status|log
```

本会话中只允许：

```bash
../manager deferred-restart 5
```

## 流程

1. 阅读相关代码、配置或技能，做必要且局部的修改。
2. 验证修改：

   ```bash
   cd ../feishu-server
   PYTHONPATH=. python -m compileall -q .
   PYTHONPATH=. python -m unittest discover -s tests -q
   python -m json.tool config.json >/dev/null
   python -m json.tool model.json >/dev/null
   cd ..
   bash -n manager hooks/commit-msg
   ```

3. 检查 Git 状态。不得提交 `.env`、`.venv/`、`.server.pid`、`pending_restart.json`、`feishu-server/data/`、`feishu-server/logs/`、`agent-workspace/workfolder/`、`agent-workspace/scheduler/`、`agent-workspace/memory/`。
4. 若当前目录是 Git 仓库，提交版本化 commit：

   ```text
   vX.Y.Z concise change

   Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>
   ```

5. 服务会使用 `.env` 的 `MAINTAINER_OPEN_ID` 发送每次启动通知。自更新时如需追加更新说明，写入 `../pending_restart.json`：

   ```json
   {"reply_text":"FeishuBot 已更新并完成延迟重启。"}
   ```

6. 执行 `../manager deferred-restart 5`。只有验证通过才重启。

## 禁止事项

- 不要在回复过程中运行 `restart`，不要直接 kill 服务进程。
- 不要用定时任务作为重启捷径。
- 验证失败时停止并报告错误，不要重启。
- 不要把密钥、token 或用户私密数据写入源码、日志示例或 commit。
