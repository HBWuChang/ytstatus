# Repository configuration and validation

Inspect the checked-out repository before applying these details. At the time this Skill was authored, the tracked template is named `（配置token后删除括号部分）config.json`; copy it to the ignored `config.json` and preserve the template.

## Configuration keys

- `base_url`: company YouTrack base URL, normally `https://yt.utui.cc`
- `token`: user's permanent token; secret and never committed
- `query`: role-specific YouTrack search query
- `report_id`: monthly ranking report, currently `174-145`
- `ranking_limit`: number of ranking rows shown, normally `10`
- `expiration_field`: optional; defaults in code to `Expiration time`
- `timezone`: optional; defaults in code to `Asia/Shanghai`

Write valid JSON. Apply mode `600` to `config.json`, confirm `.gitignore` excludes it, and use `git status --short` to ensure neither the token nor generated private configuration is staged or tracked. Never display the token while checking the file; inspect key names or redacted values only.

## SwiftBar integration

The executable is `plugins/youtrack-count.1m.py`. It resolves `config.json` relative to its real path, so a symlink from an existing SwiftBar plugin directory is preferable to copying the script away from the repository.

When SwiftBar has no existing plugin directory, it can be configured with:

```sh
defaults write com.ameba.SwiftBar PluginDirectory -string "/absolute/path/to/ytstatus/plugins"
open -a SwiftBar
```

For an existing different plugin directory, preserve that preference and add a non-conflicting symlink only with user authorization.

## Verification invariants

- Direct execution returns SwiftBar-formatted output rather than a traceback.
- HTTP 401/403 is treated as an authentication or permission problem, not as zero issues.
- The displayed issue count equals the paginated query result.
- Expanded issue entries link to their own `idReadable` URLs.
- Issues with `Expiration time` are ascending; issues without it appear last.
- “Open all” uses the same live query and issue set.
- Monthly ranking failure does not invalidate a working issue query; report it separately.
- SwiftBar points to the intended plugin or symlink and is running after setup.
