---
name: ytstatus-setup
description: Install the HBWuChang/ytstatus SwiftBar plugin on a company macOS device, guide secure YouTrack token creation, tailor issue queries to the user's role, and verify the menu-bar result. Use for new installations, onboarding, or reconfiguring this specific company YouTrack status plugin.
---

# YouTrack Status Setup

Install and configure the company YouTrack status-bar plugin from
`https://github.com/HBWuChang/ytstatus`.

## Start with the destination

Before cloning or installing anything, ask the user where the repository should be installed. Wait for an explicit path. Expand and resolve the path, but do not reinterpret it as a different location.

This workflow supports macOS because SwiftBar is a macOS application. Verify the operating system before proceeding; stop with a clear explanation on other platforms.

If the destination already exists, inspect it first. Reuse a clean clone of the same repository when safe. Do not overwrite files, replace another repository, discard changes, or delete a directory without explicit authorization.

## Install the repository and SwiftBar

1. Clone `https://github.com/HBWuChang/ytstatus` into the chosen destination. If the repository is already present, confirm its remote and preserve local changes.
2. Inspect the checked-out README, configuration template, `.gitignore`, and plugin before configuring them; repository behavior may have evolved since this Skill was authored.
3. Install SwiftBar with Homebrew when it is absent: `brew install --cask swiftbar`. If Homebrew is unavailable, explain that dependency and ask before installing Homebrew itself.
4. Ensure `plugins/youtrack-count.1m.py` is executable.

Do not silently replace an existing SwiftBar plugin directory. Read `defaults read com.ameba.SwiftBar PluginDirectory` first:

- If no plugin directory exists, set it to the cloned repository's `plugins` directory.
- If it already points there, keep it.
- If it points elsewhere, preserve it and offer to create a symlink to `youtrack-count.1m.py` inside that directory. Inspect for a name collision before creating the symlink and ask before replacing any existing entry.

## Guide token creation

Open or direct the user to:
`https://yt.utui.cc/users/me?tab=account-security`

Guide the user to perform these security-sensitive UI actions themselves:

1. Open **Tokens**.
2. Select **New token...**.
3. Enter a recognizable name, such as `ytstatus-<device-or-user>`.
4. Select all scopes, as required by this company workflow.
5. Select **Create**, copy the token, and provide it for configuration.

Treat the token as a secret. Never repeat it back, print it in command output, put it in a shell command line, include it in a report, or commit it. Store it only in the repository's ignored `config.json`, restrict that file to mode `600`, and verify that Git does not track it. If a secret-safe write mechanism is unavailable, have the user paste the token directly into `config.json` rather than exposing it through tools or logs.

## Tailor the displayed data

At a natural point after the repository is available—while the user is creating the token is fine—ask what identity, role, project, workflow state, and data they want shown. Do not assume that every user is an engineer or that one team's state names apply everywhere.

Read [references/role-queries.md](references/role-queries.md) for the short interview and query examples. Confirm the final query with the user when their answer is ambiguous or combines multiple projects/states.

Read [references/configuration.md](references/configuration.md) before writing `config.json` or validating the installation.

## Enable and verify

1. Launch SwiftBar and allow the user to complete any first-launch macOS prompts.
2. Refresh the plugin after configuration. The filename `.1m.` requests a one-minute refresh interval.
3. Run the plugin directly once in a GUI-like minimal environment. Confirm it exits successfully and emits valid SwiftBar menu text without exposing the token.
4. Verify the configured YouTrack query against the API. A result of zero can be valid; distinguish that from authentication, permission, field-name, or query-syntax errors.
5. Verify individual issue links, expiration ordering when present, and the monthly report when the user wants those features.
6. Confirm SwiftBar is running and the status item is visible. Ask the user to confirm the visual result when the environment cannot inspect the menu bar.

Finish by reporting the install path, active query, SwiftBar plugin directory, validation result, and any manual action still required. Do not include the token. If the token was exposed beyond the intended private exchange, recommend revoking it and creating a replacement.
