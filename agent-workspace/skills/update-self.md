# Self update

For FeishuBot self-update, repair, upgrade, or restart requests, use:

```text
.claude/skill/feishu-self-update/SKILL.md
```

The short rule is: validate first, commit with a `vX.Y.Z description` subject when appropriate, write `../pending_restart.json`, then run `../manager deferred-restart 5`. Do not run `../manager restart` directly from an AI response.
