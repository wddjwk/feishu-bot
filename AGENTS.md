# FeishuBot

FeishuBot 是一个通过飞书消息驱动本地 AI CLI 的个人工作助手。它负责飞书侧的事件、会话、文件、卡片和调度；AI CLI 只处理经过整理的问题、上下文和附件，不应感知飞书 API 细节。

**特别注意**：

server设计了一套记忆系统（SOUL.md, LONG_TERM, SHORT_TERM），它们是**agent工作起来之后！！**产生的记忆，关于server本身的修改，**不要**去更新记忆。因为这不是工作助手的记忆，这只是服务器的开发过程，不需要，也不应该被记录到助手的记忆中！！！

## 核心设计

- **边界清晰**：`feishu-server` 管消息路由、持久化、鉴权、文件收发和任务调度；`agent-workspace` 是 AI 的实际工作目录。
- **会话优先**：普通消息开启新会话；只有话题消息通过已保存的映射使用原 CLI、模型和 `--resume` 续接。
- **最小 prompt**：传给 AI 的是 JSON 信封，只含用户 ID、用户输入、必要上下文与 `workfolder`；不传飞书原始事件、消息 ID 映射或服务内部状态。
- **文件隔离**：每个消息使用 `agent-workspace/workfolder/<root-msg-id>` 存附件和产物。AI 真实 cwd 仍是 `agent-workspace`，但必须将用户文件产物限制在 prompt 指定的 `workfolder`。
- **配置驱动**：模型选择与 CLI 启动参数分离，运行期命令切换不写回配置文件。

## 主链路

```text
飞书长连接事件
  -> app.py
  -> MessageHandler
  -> PromptBuilder / SessionStore / Messenger
  -> AIRunner（本地 CLI）
  -> 卡片、文件或错误回复
```

`MessageHandler` 是消息策略入口：去重、过滤机器人消息、群聊仅响应 @机器人、添加/移除 OnIt 表情、选择新建或续接会话，并保存用户消息、机器人回复和话题之间的映射。

`PromptBuilder` 负责富消息解析、回复链/群话题上下文、附件下载和最小 JSON prompt。私聊话题依赖 CLI 会话恢复；群话题还需补充未 @ 机器人的群内消息上下文。

`AIRunner` 是 CLI 适配层：依据配置组装命令、注入模型环境变量、管理进程与超时、解析输出并记录关键日志。输出解析采用类注册机制——`BaseOutputParser` 处理公共逻辑（事件迭代、错误检测、结果拼装），各 CLI 子类（Claude、Pi、Copilot）按协议差异提取思考、工具调用、token 用量和最终回复，统一产出 `AIResult`。新增 CLI 只需在 `ai_runner.py` 添加 parser 子类并在 `config.json` 注册。正常消息的 `--session-id` 由服务生成 UUID；话题使用持久化会话 ID 的 `--resume`。

## 消息与文件

- 私聊响应所有用户消息；群聊必须在结构化 `mentions` 中命中机器人。
- 独立文件消息不触发 AI；用户回复文件后才下载并作为 `@绝对路径` 传入 prompt。
- 富文本、图片、音频、视频和文件均需保留语义；资源优先通过 `lark-cli` 下载，失败才回退飞书 API。
- AI 要交付文件时，在最终回复中输出 `[[FEISHU_FILE:/绝对路径]]`。服务先发送文件或图片，再回复该文件说明完成情况。
- `utils/lark_cli_wrapper.py` 是服务调用 lark-cli 的统一入口，使用专用 bot profile 并从 `.env` 初始化，不要在其他模块拼装 lark-cli 命令。

## 状态、配置与命令

- `config.json`：飞书、服务、日志、工作目录、lark-cli 与各 CLI 的启动参数；`features` 节控制功能开关（如 `show_token_usage_on_card`）。
- `model.json`：默认 CLI、每个 CLI 的默认模型、模型列表、别名和环境变量。
- `/reload` 热加载两份配置，但保留当前进程内通过 `/cli`、`/model` 做出的选择；重启后恢复配置默认值。
- `cards.py` 统一管理 Card JSON 2.0；卡片结构为标题（工具图标+模型名）、正文（Markdown，长代码块自动折叠）、token 用量（状态栏风格小字）、分析过程折叠块。卡片宽度和工具图标由配置控制。
- 用户命令仅维护 `/help`、`/running`、`/kill`、`/timer`、`/timer-rm`、`/reload`、`/status`、`/cli`、`/model`。
- `data/session_map.json` 保存会话与飞书消息/话题关联；它是可清理的运行数据，不是业务数据库。

## 定时任务

调度器监听本地 `127.0.0.1` API，任务状态保存在 `data/tasks.json`。

- `script`：固定、确定、不需要推理的工作优先使用。脚本必须位于 `agent-workspace/scheduler/<task-id>/`，以 argv 直接执行。
- `agent`：仅用于需要推理、研究、归纳或撰写的任务。未指定时固定使用 `model.json` 默认 CLI/模型；可在任务 payload 固定 `tool` 与 `model`。
- 定时任务的使用规范见 `agent-workspace/.claude/skills/feishu-scheduler/SKILL.md`。

## 开发与运维

- 凭据只放 `.env`；运行数据、日志、`workfolder/`、`scheduler/` 和记忆目录均不提交。
- 修改服务后按针对性测试验证；常用验证为 `python -m unittest discover -s tests`、`compileall` 及两个 JSON 校验。
- 使用 `./manager start|stop|status|log` 管理服务。AI 自更新只能使用 `./manager deferred-restart 5`，禁止在回复过程中直接重启。
- 飞书后台需启用机器人能力、订阅 `im.message.receive_v1`、配置消息/资源/表情权限并发布版本；缺权限或接口字段不确定时优先查官方文档。

## 维护原则

新增功能应沿现有边界扩展：飞书交互放在服务层，模型差异放在配置和 `AIRunner`，用户工作产物放在对应工作目录。保持消息处理幂等、文件路径受限、任务脚本无 shell 拼接，并避免让 AI 承担服务内部路由或持久化职责。
