---
name: repo-tools
description: Locate Python definitions and candidate callers/tests, make exact syntax-checked edits, remember task progress, and run targeted verification in the coding-task sandbox.
---

Run `run_skill_script` with `skill_name="repo-tools"` and `file_path="scripts/setup.py"` once. Omit `args` entirely: malformed arrays can hang the model's tool-call parser. The installer copies the helper into task-local git metadata; subsequent operations use the single-string `run_command` tool.

Use `python .git/swe_agent.py` from `/workspace`:

| Command | Result |
| --- | --- |
| `find IDENTIFIER 'error phrase'` | Ranked source definitions, short excerpts, candidate call sites and related tests; saves locations automatically. Call sites are name matches, not resolved data flow. |
| `show pkg/module.py Class.method` | Exact qualified definition with line numbers; a long definition includes `shown_end` and `more`. |
| `show pkg/module.py 80-130` | Source window. Displayed line numbers are not part of the file. |
| `edit pkg/module.py` with blocks on stdin | All blocks must match uniquely; validates Python syntax before atomic write; saves file hash automatically. |
| `memory` | Recent locations, ruled-out hypotheses, next actions, edits and checks. Read after context loss or before repeating a search. |
| `memory rejected 'Hypothesis disproved by observation ...'` | Record a concrete rejected hypothesis; may share a shell call with another operation. |
| `memory next 'Implement ... then check ...'` | Save the next action; this does not execute it. |
| `check tests/test_module.py::test_name --timeout 60` | Source diff, syntax and whitespace checks, targeted pytest with timeout and persisted test output. |
| `check` | Source diff and syntax checks only; tests explicitly report `not_run`. |

Example edit through one `run_command`:

```bash
python .git/swe_agent.py edit pkg/module.py <<'EOF_EDIT'
<<<<<<< SEARCH
    return value
=======
    return value.strip()
>>>>>>> REPLACE
EOF_EDIT
```

Include more unchanged context when SEARCH matches multiple locations. Whitespace is exact; no fuzzy replacements. Multiple blocks for one file are all validated before writing. An empty SEARCH creates a new file only; it never appends to an existing one. Source encoding and permissions are preserved. Test, runner config, metadata and symlink writes are rejected.

Memory is bounded and isolated by workspace identity in the temporary directory, never in the source patch. It holds observations, not instructions, and does not transfer across tasks. Check records contain source hashes; repeat verification after further edits. For long investigations, record only a rejected hypothesis or next action that will prevent repeated work. Avoid a separate memory call after every command.

`check` only runs explicitly named test files (up to three), never the full suite. It returns `passed`, `test_failure`, `dependency_or_import_error`, `collection_or_runner_error`, `timeout`, or `no_tests_or_unverified`. Import failures do not prove an environment problem. No tests collected or all tests skipped do not count as a verified fix. Full pytest output is in the task-local temporary state directory; summaries are bounded for the harness output limit.

If a result says `truncated`, request a narrower source window. A successful syntax check is not semantic verification. Helpers never submit a patch, restore files, install packages, or access the network. Check the final source diff, then use `submit_patch` as the final tool action. If installation fails, use the built-in tools directly rather than spend repeated calls on setup.
