# Gemma Coding Agent

A coding agent for fixing issues in Python repositories. It reads an issue, locates the relevant implementation, edits source files, runs focused checks, and delivers a patch to its execution runtime.

The project maintains a single agent configuration. Gemma selects actions, the `repo-tools` skill provides repository navigation, checked editing, task memory, and verification feedback, and an external ADK/SWE runtime manages model calls, isolated workspaces, conversation history, and execution limits.

## Architecture

The implementation has three parts:

| Component | Location | Responsibility |
| --- | --- | --- |
| Agent definition and policy | `agent/agent.yaml`, `agent/prompts/solver.md`, `agent/eval_config.yaml` | Select the model, tools, workflow instructions, and per-task limits. The prompt guides behavior; it is not an enforced state machine. |
| Repository operations | `agent/skills/repo-tools/` | Run deterministic Python helpers for navigation, source reading, exact edits, structured memory, and focused verification. |
| Configuration and build tooling | `src/gemma_agent/` | Provide a local CLI to inspect configuration, validate it, and create a ZIP bundle for an external runtime. |

The external runtime runs the model–tool loop: it sends the issue, instructions, and previous observations to Gemma; Gemma selects an action; the runtime executes the tool and returns its output. The loop continues until the agent submits a patch or reaches its execution limits.

```text
Development
  Agent definition -> CLI validation -> Bundle -> Compatible runtime

Task execution
  Issue + isolated repository -> Runtime loads configuration
                                            |
                                      Gemma selects an action
                                            |
                                    Built-in tools / skill
                                            |
                                      Execution feedback
                                            |
                                Back to Gemma, or final patch
```

This repository implements the configuration, prompt, skill scripts, and build/validation tooling. Model serving, sandbox orchestration, conversation management, and independent patch verification belong to the external runtime. `gemma-agent build` creates a bundle; it does not start an agent task.

## Project Layout

```text
agent/
  agent.yaml                         Model, tool, and skill configuration
  eval_config.yaml                   Per-task limits expected by the runtime
  prompts/solver.md                  Issue-resolution instructions
  skills/repo-tools/
    SKILL.md                         Skill instructions and command reference
    scripts/setup.py                 Install the helper in a task workspace
    scripts/repo_tools.py            Navigation, editing, memory, and checks
src/gemma_agent/
  cli.py                             Inspect, validate, and build commands
  validation.py                      Configuration, reference, and archive checks
  bundle.py                          Atomic bundle creation and revalidation
scripts/setup_runtime.sh             Install optional runtime dependencies
```

## Task Lifecycle

1. **Load the task.** The external runtime prepares a Git repository at `/workspace`, provides the issue description, and loads the agent configuration, prompt, and execution limits.
2. **Prepare the skill.** The model loads `repo-tools` and runs `scripts/setup.py` through `run_skill_script`. The installer places the helper at `.git/swe_agent.py` inside the task repository, keeping it out of the source patch. If installation fails, the model can continue with built-in tools.
3. **Locate and read code.** Through `run_command`, the model invokes the helper's `find` and `show` commands. The helper reads repository files, parses Python ASTs, and returns definitions, short source windows, candidate call sites, and related tests.
4. **Make an edit.** The helper's `edit` command reads SEARCH/REPLACE blocks, requires unique exact matches, validates the resulting Python syntax, and atomically writes one file. The model can also use the built-in `edit_file` tool.
5. **Use verification feedback.** The helper's `check` command inspects the source diff, syntax, and whitespace. It runs pytest when explicit test files are supplied. Gemma uses the results to revise the implementation or finish the task.
6. **Keep useful task state.** Helper navigation, edit, and check operations automatically record their results. The model can also record rejected hypotheses and next actions, then retrieve them after context loss or before repeating an investigation.
7. **Deliver the patch.** After reviewing the source changes and checks, the model calls `submit_patch`. The runtime captures the final diff. Submission may end the task immediately, so verification must happen first.

This workflow is guided by instructions rather than a mandatory sequence. Direct calls to the built-in `edit_file` tool or pytest do not automatically populate the helper's edit and check records.

## Local Usage

Use Python 3.12 or later. From the project root:

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e .

gemma-agent info
gemma-agent validate
gemma-agent build
```

The CLI delegates to the following components:

- `info` validates and reads the YAML configuration, then displays the model, tools, skill, sampling settings, and execution limits.
- `validate` uses `validation.py` to check configuration references, tool and model settings, budgets, file types, and path boundaries. For ZIP inputs, it also checks archive paths, symbolic links, and duplicate entries.
- `build` uses `bundle.py` to validate the input, create a temporary ZIP, revalidate the archive, and atomically replace the destination. Invalid inputs or build failures leave an existing bundle untouched.

The default output is `build/gemma-agent.zip`. To choose another location:

```bash
gemma-agent build --output /tmp/gemma-agent.zip
```

These commands inspect and package the agent. They do not start a model or an external task. The project requires an ADK/SWE execution service and does not include a standalone chat interface or model server.

## Agent Capabilities

| Capability | Implementation |
| --- | --- |
| Code navigation | Python AST definitions, source excerpts, candidate call sites, and related tests |
| Checked editing | Unique exact SEARCH/REPLACE, syntax validation, and atomic writes per file |
| Task memory | Automatic navigation/edit/check records, optional rejected hypotheses and next actions, and stale-check detection |
| Verification feedback | Source diff, syntax, whitespace, and targeted pytest checks with distinct failure and verification states |

The agent uses one Gemma 31B model, exposes four built-in tools—`run_command`, `edit_file`, `get_status`, and `submit_patch`—and loads the `repo-tools` skill.

Each task allows up to 28 budget-counted tool calls, 40 model turns, and 8 minutes. Individual commands have a 180-second limit. Sampling uses temperature 1.0, top_p 0.95, top_k 64, and a maximum output of 8192 tokens.

## Skill and Memory

Run the skill's `scripts/setup.py` once through `run_skill_script`, omitting `args`. The installer creates `.git/swe_agent.py` in the task workspace. Subsequent helper operations use the single-string `run_command` tool:

```bash
python .git/swe_agent.py find IDENTIFIER 'exact error phrase'
python .git/swe_agent.py show pkg/module.py Class.method
python .git/swe_agent.py memory rejected 'Hypothesis ruled out by an observation'
python .git/swe_agent.py check tests/test_module.py::test_name --timeout 60
python .git/swe_agent.py memory
```

See the [skill instructions](agent/skills/repo-tools/SKILL.md) for the exact editing heredoc format.

Memory has two layers:

- **Conversation context:** the runtime maintains the issue, model outputs, and tool observations. This is not a long-term memory service implemented by this project.
- **Structured task memory:** the helper creates a workspace-specific state directory under the system temporary directory and stores `memory.json` there. It retains recent locations, search terms, rejected hypotheses, next actions, file hashes, and check results, with at most eight records per category.

`find`, `edit`, and `check` record their own results automatically. The model calls `memory rejected` and `memory next` when useful. Reading `memory` compares the last check's source snapshot with the current files and returns `last_check_stale`. This value can be null when no check exists or its snapshot is incomplete. Memory and full pytest logs stay outside the source patch and are not carried across tasks.

Candidate call sites are matched by name, not by complete data-flow analysis. Import errors are not automatically attributed to the environment. Without explicit test targets, `check` only inspects source and diff validity; no collected tests or an entirely skipped test run does not establish behavioral correctness. The model decides which helper commands to use and can fall back to built-in tools if setup fails.

## Runtime Integration

The agent definition uses ADK/SWE-compatible YAML. An external runtime must provide a Gemma inference endpoint, an isolated workspace, the four built-in tools, a skill executor, and the issue, prompt, and per-task limits.

With an existing `.venv-harness` environment, run a real compilation check:

```bash
PYTHONPATH=src LITELLM_LOCAL_MODEL_COST_MAP=True .venv-harness/bin/python -m gemma_agent validate --runtime
```

`--runtime` requires compilation to succeed; missing dependencies are reported as errors. Default validation checks a single agent configuration and does not estimate the duration of a batch of tasks.

To install the optional runtime dependencies, place compatible wheels in `.cache/runtime-wheels/`, then run:

```bash
bash scripts/setup_runtime.sh
```
