<!-- agents:start -->
## graphify

This project has a knowledge graph at graphify-out/ with god nodes, community structure, and cross-file relationships.

When the user types `/graphify`, use the installed graphify skill or instructions before doing anything else.

Rules:
- For codebase questions, first run `graphify query "<question>"` when graphify-out/graph.json exists. Use `graphify path "<A>" "<B>"` for relationships and `graphify explain "<concept>"` for focused concepts. These return a scoped subgraph, usually much smaller than GRAPH_REPORT.md or raw grep output.
- Dirty graphify-out/ files are expected after hooks or incremental updates; dirty graph files are not a reason to skip graphify. Only skip graphify if the task is about stale or incorrect graph output, or the user explicitly says not to use it.
- If graphify-out/wiki/index.md exists, use it for broad navigation instead of raw source browsing.
- Read graphify-out/GRAPH_REPORT.md only for broad architecture review or when query/path/explain do not surface enough context.
- After modifying code, run `graphify update .` to keep the graph current (AST-only, no API cost).

## Code style

- Follow the codebase's existing conventions: mimic the style, use the same libraries and utilities, and follow the patterns already in place.
- Never assume a library is available — check the codebase first (package.json, pyproject.toml, or imports in neighboring files) before using it.
- When creating a new component, study existing components first and match their framework choice, naming, typing, and conventions.
- Do not add code comments unless asked.
- Follow security best practices: never log, print, or commit secrets or keys.

## Verification before completion

- Before declaring work done, run the project's tests and any lint/typecheck commands and confirm the output — do not claim success without seeing it.
- Find the correct command from the README or existing config rather than guessing the framework.
- A failing test is an open drain: fix it (or explicitly record it out of scope) before moving on, and keep the suite green at the end of the session.
<!-- agents:end -->
