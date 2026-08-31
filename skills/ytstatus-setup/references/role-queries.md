# Role and query selection

Use a brief interview; ask only what is not already known:

- What is the user's YouTrack identity or should `assignee: me` be used?
- What is their role or workflow responsibility?
- Which project or projects should be included?
- Which state or states represent actionable work for them?
- Do they want only an issue count, the expanded issue list, the monthly ranking, or all available views?
- Is the default `Asia/Shanghai` timezone appropriate?

Prefer `assignee: me` because the permanent token resolves the current user. Use a named assignee only when the user explicitly wants another person's work.

Examples are starting points, not universal defaults:

- Engineer: `project: DE4 assignee: me State: Todo`
- QC/tester: `project: DE4 assignee: me State: Testing`
- Multiple actionable states: `project: DE4 assignee: me State: {Todo}, {Testing}` only after verifying that this syntax and those exact state values are accepted by the live YouTrack instance.

YouTrack field names and state values are instance-specific. Test the proposed query through the API and, when useful, compare progressively broader forms such as project only, project plus assignee, and the full query. Do not silently broaden a zero-result query: report the evidence and let the user decide.

If the user wants monthly ranking, verify access to the configured report ID. The repository currently uses report `174-145`, but the checked-out template and live API are authoritative.
