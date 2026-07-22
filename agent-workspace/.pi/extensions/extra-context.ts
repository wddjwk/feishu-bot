/**
 * Extra Context Files Extension
 *
 * pi 默认不会钻进 .pi/ 子目录加载 AGENTS.md / rules，
 * 这个扩展补上：每 turn 读取 .pi/AGENTS.md 和 .pi/rules/**.md
 * 追加到系统提示词。
 *
 * 安装：放到 ~/.pi/agent/extensions/ 或 项目 .pi/extensions/，然后 /reload。
 */

import * as fs from "node:fs";
import * as path from "node:path";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

function findMarkdownFiles(dir: string, basePath = ""): string[] {
  if (!fs.existsSync(dir)) return [];
  const results: string[] = [];
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    const rel = basePath ? `${basePath}/${entry.name}` : entry.name;
    if (entry.isDirectory()) {
      results.push(...findMarkdownFiles(path.join(dir, entry.name), rel));
    } else if (entry.isFile() && entry.name.endsWith(".md")) {
      results.push(rel);
    }
  }
  return results;
}

export default function extraContextExtension(pi: ExtensionAPI) {
  pi.on("before_agent_start", async (event) => {
    const cwd = (event as any).systemPromptOptions?.cwd ?? process.cwd();
    const blocks: string[] = [];

    const agentsPath = path.resolve(cwd, ".pi/AGENTS.md");
    if (fs.existsSync(agentsPath)) {
      blocks.push(`### .pi/AGENTS.md\n\n${fs.readFileSync(agentsPath, "utf-8").trim()}`);
    }

    const rulesDir = path.resolve(cwd, ".pi/rules");
    const ruleFiles = findMarkdownFiles(rulesDir);
    for (const rel of ruleFiles) {
      const content = fs.readFileSync(path.join(rulesDir, rel), "utf-8").trim();
      blocks.push(`### .pi/rules/${rel}\n\n${content}`);
    }

    if (blocks.length === 0) return;
    return {
      systemPrompt: event.systemPrompt + `\n\n## Extra Context Files\n\n${blocks.join("\n\n---\n\n")}\n`,
    };
  });
}

