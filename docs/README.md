# nanobot Documentation

Use these docs to get a working agent first, then open a task guide only when you need the next capability. Source-level design and extension details are kept in the contributor section.

Repository docs follow the current source tree and can be newer than the latest package release. For published release docs, visit [nanobot.wiki](https://nanobot.wiki/docs/latest/getting-started/nanobot-overview).

## Start Here

| Your situation | Read this | You are done when... |
|---|---|---|
| Terminals, Python, or API keys are new to you | [Beginner walkthrough](./start-without-technical-background.md) | The browser can send `Hello!` and receive a reply |
| You are comfortable running commands | [Install and Quick Start](./quick-start.md) | `nanobot status` is healthy and the WebUI or CLI can get one reply |
| Something already failed | [Troubleshooting](./troubleshooting.md) | You have isolated the problem to install, config, model, gateway, channel, or tool access |
| Continue v2.1.3 development on a desktop | [v2.1.3 desktop quick-start](./v213-desktop-e2e-runbook.md) | The correct branch, dependencies and local services are ready |
| Inspect every Pipeline LLM input/output and current failure point | [LLM stage contracts](./llm-stage-contracts/README.md) | The current code-level model boundary is clear |

Current auto-cut architecture documents are deliberately limited to the live design set:

- [Ark Responses SDK integration](./ark-responses-sdk-integration-design.md)
- [VLM v4 video support](./vlm-video-support-v4-design.md)
- [WindowContextPack](./vlm-window-context-pack-design.md)
- [Selective stage/episode recompute](./pipeline-selective-recompute-design.md)
- [Pipeline LLM stage contracts](./llm-stage-contracts/README.md)
- [PC semantic Pipeline runbook](./pc-semantic-pipeline-run.md)
- [Desktop branch/runtime runbook](./v213-desktop-e2e-runbook.md)

The recommended first-run path is:

1. Install nanobot.
2. Let the installer open `nanobot webui` on a fresh local desktop.
3. Configure a provider and model in **Settings → Models**.
4. Send `Hello!` before configuring anything else.

Most people do not need to edit JSON for the first run. The WebUI handles the initial provider, model, and local browser settings. SSH, headless, existing-config, and older-release installs retain `nanobot onboard --wizard` as a terminal fallback. After the WebUI opens, use **Settings** for models and built-in capabilities, **Settings → Channels** for chat apps, and **Apps** for Agent Plugins, CLI Apps, and MCP integrations.

## Add One Capability

Pick the row that matches what you want to accomplish next:

| Goal | Guide |
|---|---|
| Learn the browser workbench | [WebUI](./webui.md) |
| Choose a hosted, OAuth, company, or local model | [Provider Cookbook](./provider-cookbook.md) |
| Add model fallbacks | [Configure Model Fallback](./guides/configure-model-fallback.md) |
| Enable web search | [Configure Web Search](./guides/configure-web-search.md) |
| Manage Agent Plugins, CLI Apps, or MCP integrations | [WebUI Apps](./webui.md#apps) |
| Add an MCP tool server | [Configure MCP Tools](./guides/configure-mcp-tools.md) |
| Run nanobot continuously | [Deployment](./deployment.md) |
| Run separate bots or workspaces | [Multiple Instances](./multiple-instances.md) |
| Call nanobot from Python | [Python SDK](./python-sdk.md) |

## Operate nanobot

| Need | Read |
|---|---|
| Commands and flags | [CLI Reference](./cli-reference.md) |
| In-chat slash commands | [In-Chat Commands](./chat-commands.md) |
| Config, workspace, gateway, sessions, tools, and memory in plain language | [Concepts](./concepts.md) |
| Provider/model matching and selection | [Providers and Models](./providers.md) |
| Setup and runtime diagnosis | [Troubleshooting](./troubleshooting.md) |

## Reference

Use reference pages to look up an exact option after you know what you are trying to configure:

| Area | Reference |
|---|---|
| Every configuration field and default | [Configuration](./configuration.md) |
| Provider and model behavior | [Providers and Models](./providers.md) |
| Python SDK classes, events, sessions, and hooks | [Python SDK](./python-sdk.md) |

Configuration examples are usually snippets to merge into `~/.nanobot/config.json`, not complete replacement files. The docs use camelCase because nanobot writes config that way. Keep real API keys, bot tokens, and passwords out of issues and public logs.

## Extend or Contribute

These pages explain implementation and extension points. You do not need them to install or operate nanobot.

| Goal | Read |
|---|---|
| Understand source ownership and runtime flow | [Architecture](./architecture.md) |
| Follow repository contribution rules | [CONTRIBUTING.md](../CONTRIBUTING.md) |
| Build the WebUI source | [WebUI Development](../webui/README.md) |

If a command or screen no longer matches these docs, please [open an issue](https://github.com/HKUDS/nanobot/issues) with your nanobot version, operating system, and the page that needs correction.
