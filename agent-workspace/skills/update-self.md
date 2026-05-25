# Self update

When asked to update FeishuBot itself:

1. Modify files under `../feishu-server` or project configuration as needed.
2. Validate syntax and relevant behavior.
3. If the directory is a Git repo, commit with a subject starting `vX.Y.Z `.
4. Write `../pending_restart.json` with the user-facing completion message and target receive ID when available.
5. Run `../manager deferred-restart 5`.

Do not run `../manager restart` directly.
