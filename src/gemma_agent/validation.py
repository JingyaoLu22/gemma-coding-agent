"""Validate declarative Gemma agent bundles and optionally compile the runtime."""
from __future__ import annotations

import json
import math
import os
import re
import stat
import tempfile
import zipfile
from pathlib import Path
from types import SimpleNamespace

import yaml

DEFAULT_AGENT_DIR = Path("agent")

SUPPORTED_MODELS = {
    "gemma-4-31b-it-qat-w4a16-ct",
    "gemma-4-12b-it-qat-w4a16-ct",
    "gemma-4-e4b-it-qat-w4a16-ct",
    "gemma-4-e2b-it-qat-w4a16-ct",
}
MODEL_PREFIXES = ("openai/", "google/", "hosted_vllm/", "custom/")
BUILTIN_TOOLS = {
    "run_command",
    "submit_patch",
    "get_status",
    "read_file",
    "edit_file",
    "write_file",
    "get_code_neighbors",
    "search_similar_code",
    "get_code_subgraph",
}
ROOT_CONFIG_NAMES = ("agent.yaml", "agent.yml", "root_agent.yaml", "root_agent.yml")
ALLOWED_EXTENSIONS = {".yaml", ".yml", ".md", ".txt", ".py", ".json", ".safetensors"}
AGENT_CLASSES = {None, "LlmAgent", "SequentialAgent", "ParallelAgent", "LoopAgent"}
FORBIDDEN_GEN_KEYS = {"tools", "system_instruction", "http_options", "safety_settings", "response_schema"}
ADAPTER_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
EVAL_KEYS = {"timeout_seconds", "max_tool_calls", "max_time_minutes", "max_turns"}

MAX_TOTAL_BYTES = 3 * 1024**3
MAX_FILE_COUNT = 10_000
MAX_INCLUDE_DEPTH = 10
MAX_CONTEXT_TOKENS = 32_768
MAX_LORA_RANK = 128

# Session state set by the harness. `hints` only exists when the task has hints,
# so a bare {hints} raises KeyError in ADK on most tasks; it must be {hints?}.
ALWAYS_PRESENT_STATE = {"problem_description"}
# Same pattern ADK uses in google.adk.utils.instructions_utils.inject_session_state.
STATE_VAR_RE = re.compile(r"{+[^{}]*}+")

JUNK_NAMES = {".DS_Store", "Thumbs.db", "__pycache__", ".ipynb_checkpoints"}


class CheckError(Exception):
    pass


class Report:
    def __init__(self) -> None:
        self.errors: list[str] = []
        self.warnings: list[str] = []
        self.notes: list[str] = []

    def error(self, msg: str) -> None:
        self.errors.append(msg)

    def warn(self, msg: str) -> None:
        self.warnings.append(msg)

    def note(self, msg: str) -> None:
        self.notes.append(msg)

    def print(self) -> None:
        for msg in self.notes:
            print(f"  info  {msg}")
        for msg in self.warnings:
            print(f"  WARN  {msg}")
        for msg in self.errors:
            print(f"  ERROR {msg}")
        status = "FAILED" if self.errors else "PASSED"
        print(f"\nValidation {status}: {len(self.errors)} error(s), {len(self.warnings)} warning(s)")


def iter_agent_files(root: Path):
    """Yield the files that belong in the archive, skipping OS/editor junk."""
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = sorted(d for d in dirnames if d not in JUNK_NAMES)
        for name in sorted(filenames):
            if name in JUNK_NAMES or name.endswith(".pyc"):
                continue
            yield Path(dirpath) / name


# --------------------------------------------------------------------------- yaml


def _resolve_include(base_dir: Path, rel: str, root: Path) -> Path:
    if not rel or "\x00" in rel:
        raise CheckError(f"invalid !include path {rel!r}")
    rel_path = Path(rel)
    if rel_path.is_absolute():
        raise CheckError(f"absolute !include path {rel!r}")
    # Paths resolve relative to the including file; `..` is fine (nested
    # agent definitions use ../prompts from sub_agents/) as long as it stays inside root.
    target = (base_dir / rel_path).resolve()
    if root.resolve() not in target.parents:
        raise CheckError(f"!include {rel!r} escapes the submission root")
    if not target.is_file():
        raise CheckError(f"!include {rel!r} not found (looked for {target})")
    return target


def load_yaml(path: Path, root: Path, depth: int = 0, stack: tuple[Path, ...] = ()):
    if depth > MAX_INCLUDE_DEPTH:
        raise CheckError(f"!include depth exceeds {MAX_INCLUDE_DEPTH} at {path}")

    class Loader(yaml.SafeLoader):
        pass

    def include(loader, node):
        rel = loader.construct_scalar(node)
        target = _resolve_include(path.parent, rel, root)
        if target.suffix in (".md", ".txt"):
            return target.read_text(encoding="utf-8")
        if target.suffix in (".yaml", ".yml"):
            if target in stack or target == path:
                raise CheckError(f"!include cycle at {target}")
            return load_yaml(target, root, depth + 1, stack + (path,))
        raise CheckError(f"!include of unsupported file type {rel!r}")

    Loader.add_constructor("!include", include)
    try:
        return yaml.load(path.read_text(encoding="utf-8"), Loader=Loader)
    except yaml.YAMLError as exc:
        raise CheckError(f"YAML parse error in {path.relative_to(root)}: {exc}") from exc


# ------------------------------------------------------------------------ checks


def check_files(root: Path, report: Report) -> None:
    total = 0
    count = 0
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        for name in dirnames + filenames:
            if (Path(dirpath) / name).is_symlink():
                report.error(f"symlink not allowed: {(Path(dirpath) / name).relative_to(root)}")
    for path in iter_agent_files(root):
        rel = path.relative_to(root)
        count += 1
        total += path.stat().st_size
        if path.suffix.lower() not in ALLOWED_EXTENSIONS:
            report.error(f"file type not allowed: {rel} (allowed: {sorted(ALLOWED_EXTENSIONS)})")
        if rel.parts[0] == "adapters" and path.suffix in (".bin", ".pt", ".pth"):
            report.error(f"pickle weights not allowed: {rel}")
    if count > MAX_FILE_COUNT:
        report.error(f"{count} files exceeds limit {MAX_FILE_COUNT}")
    if total >= MAX_TOTAL_BYTES:
        report.error(f"total size {total / 1024**3:.2f} GiB exceeds 3 GiB")
    report.note(f"{count} file(s), {total / 1024:.1f} KiB total")


def find_root_config(root: Path, report: Report) -> Path | None:
    found = [root / n for n in ROOT_CONFIG_NAMES if (root / n).is_file()]
    if not found:
        nested = [p for p in root.glob("*/agent.y*ml")]
        hint = f" (found {nested[0].relative_to(root)}: zip the folder contents, not the folder)" if nested else ""
        report.error(f"no root config ({', '.join(ROOT_CONFIG_NAMES)}) at archive root{hint}")
        return None
    if len(found) > 1:
        report.error(f"multiple root configs: {[p.name for p in found]}")
        return None
    return found[0]


def check_adapters(root: Path, report: Report) -> set[str]:
    names: set[str] = set()
    adapters_dir = root / "adapters"
    if not adapters_dir.is_dir():
        return names
    for d in sorted(p for p in adapters_dir.iterdir() if p.is_dir()):
        if not ADAPTER_NAME_RE.match(d.name):
            report.error(f"invalid adapter name {d.name!r}")
        cfg = d / "adapter_config.json"
        weights = d / "adapter_model.safetensors"
        if not cfg.is_file() or not weights.is_file():
            report.error(f"adapters/{d.name} needs adapter_config.json and adapter_model.safetensors")
            continue
        try:
            rank = json.loads(cfg.read_text()).get("r")
        except json.JSONDecodeError as exc:
            report.error(f"adapters/{d.name}/adapter_config.json is not valid JSON: {exc}")
            continue
        if isinstance(rank, int) and rank > MAX_LORA_RANK:
            report.error(f"adapters/{d.name} rank {rank} exceeds max_lora_rank {MAX_LORA_RANK}")
        names.add(d.name)
    return names


def check_instruction_vars(text: str, where: str, output_keys: set[str], report: Report) -> None:
    for match in STATE_VAR_RE.finditer(text):
        var = match.group().lstrip("{").rstrip("}").strip()
        optional = var.endswith("?")
        name = var.removesuffix("?")
        if name.startswith("artifact."):
            if not optional:
                report.error(f"{where}: {match.group()} requires an artifact service; use the ? suffix or remove it")
            continue
        parts = name.split(":")
        is_state_name = name.isidentifier() or (
            len(parts) == 2 and parts[0] in ("app", "user", "temp") and parts[1].isidentifier()
        )
        if not is_state_name or optional or name in ALWAYS_PRESENT_STATE:
            continue
        if name in output_keys:
            report.warn(f"{where}: {match.group()} is only set after the agent with output_key={name} has run")
        elif name == "hints":
            report.error(f"{where}: {{hints}} raises KeyError on tasks without hints; write {{hints?}}")
        else:
            report.error(
                f"{where}: {match.group()} is treated by ADK as a session-state variable and will raise "
                "KeyError at runtime; remove the braces or rephrase"
            )


def check_generation(cfg, where: str, report: Report) -> None:
    if cfg is None:
        return
    if not isinstance(cfg, dict):
        report.error(f"{where}: generate_content_config must be a mapping")
        return
    for key in FORBIDDEN_GEN_KEYS & cfg.keys():
        report.error(f"{where}: generate_content_config.{key} is not allowed")
    max_out = cfg.get("max_output_tokens", 16384)
    if not isinstance(max_out, int) or not 1 <= max_out <= MAX_CONTEXT_TOKENS:
        report.error(f"{where}: max_output_tokens must be an int in [1, {MAX_CONTEXT_TOKENS}], got {max_out!r}")
    thinking = cfg.get("thinking_config") or {}
    budget = thinking.get("thinking_budget", 4096) if isinstance(thinking, dict) else None
    if budget is not None and (not isinstance(budget, int) or not 0 <= budget <= MAX_CONTEXT_TOKENS):
        report.error(f"{where}: thinking_budget must be an int in [0, {MAX_CONTEXT_TOKENS}], got {budget!r}")
    if isinstance(budget, int) and isinstance(max_out, int) and budget >= max_out:
        report.warn(f"{where}: thinking_budget ({budget}) >= max_output_tokens ({max_out}) leaves no room for the tool call")
    temp = cfg.get("temperature")
    if temp is not None and (not isinstance(temp, (int, float)) or temp < 0):
        report.error(f"{where}: temperature must be >= 0")
    top_p = cfg.get("top_p")
    if top_p is not None and (not isinstance(top_p, (int, float)) or not 0 <= top_p <= 1):
        report.error(f"{where}: top_p must be in [0, 1]")
    top_k = cfg.get("top_k")
    if top_k is not None and (not isinstance(top_k, int) or top_k < 1):
        report.error(f"{where}: top_k must be an int >= 1")


def collect_agents(cfg_path: Path, root: Path, seen: set[Path]) -> list[tuple[str, dict]]:
    """Return (label, config) for the agent at cfg_path and every agent it references."""
    cfg_path = cfg_path.resolve()
    if cfg_path in seen:
        return []
    seen.add(cfg_path)
    cfg = load_yaml(cfg_path, root)
    if not isinstance(cfg, dict):
        raise CheckError(f"{cfg_path.relative_to(root.resolve())} must be a YAML mapping")
    label = str(cfg_path.relative_to(root.resolve()))
    return [(label, cfg)] + _collect_children(cfg, cfg_path.parent, root, seen, label)


def _collect_children(cfg: dict, base_dir: Path, root: Path, seen: set[Path], label: str):
    agents: list[tuple[str, dict]] = []
    refs = []
    for sub in cfg.get("sub_agents") or []:
        refs.append(sub)
    for tool in cfg.get("tools") or []:
        if isinstance(tool, dict) and "agent_tool" in tool:
            refs.append(tool["agent_tool"])
        elif isinstance(tool, dict) and "name" in tool:
            refs.append(tool)  # inline agent definition
    for ref in refs:
        if isinstance(ref, dict) and "config_path" in ref:
            target = _resolve_include(base_dir, ref["config_path"], root)
            agents += collect_agents(target, root, seen)
        elif isinstance(ref, dict) and "name" in ref:
            inline_label = f"{label}:{ref['name']}"
            agents.append((inline_label, ref))
            agents += _collect_children(ref, base_dir, root, seen, inline_label)
        else:
            raise CheckError(f"{label}: unsupported sub-agent/agent_tool reference {ref!r}")
    return agents


def check_agents(root_cfg: Path, root: Path, adapters: set[str], report: Report) -> None:
    agents = collect_agents(root_cfg, root, set())
    models = set()
    output_keys = {cfg["output_key"] for _, cfg in agents if cfg.get("output_key")}
    names = [cfg.get("name") for _, cfg in agents]
    for name in {n for n in names if names.count(n) > 1}:
        report.warn(f"agent name {name!r} is used more than once")
    for label, cfg in agents:
        cls = cfg.get("agent_class")
        if cls not in AGENT_CLASSES:
            report.error(f"{label}: unknown agent_class {cls!r}")
        if not cfg.get("name"):
            report.error(f"{label}: missing name")
        if cls == "LoopAgent":
            iters = cfg.get("max_iterations", 500)
            if not isinstance(iters, int) or not 1 <= iters <= 500:
                report.error(f"{label}: max_iterations must be in [1, 500]")
        if cls not in (None, "LlmAgent"):
            continue
        model = cfg.get("model")
        if not model:
            report.error(f"{label}: LlmAgent needs a model")
        else:
            for prefix in MODEL_PREFIXES:
                model = model.removeprefix(prefix)
            models.add(model)
        adapter = cfg.get("adapter")
        if adapter and adapter not in adapters:
            report.error(f"{label}: adapter {adapter!r} not found under adapters/")
        instruction = cfg.get("instruction") or ""
        if not isinstance(instruction, str):
            report.error(f"{label}: instruction must be a string")
            instruction = ""
        check_instruction_vars(instruction, label, output_keys, report)
        if len(instruction) / 4 > 6000:
            report.warn(f"{label}: instruction is ~{len(instruction) // 4} tokens; it is resent on every model call")
        tool_names = []
        for tool in cfg.get("tools") or []:
            if isinstance(tool, str):
                tool_names.append(tool)
                if tool not in BUILTIN_TOOLS:
                    report.error(f"{label}: unknown tool {tool!r} (allowed: {sorted(BUILTIN_TOOLS)})")
        if label == str(root_cfg.resolve().relative_to(root.resolve())) and "submit_patch" not in tool_names:
            report.warn(f"{label}: root agent has no submit_patch (the harness falls back to git diff)")
        check_generation(cfg.get("generate_content_config"), label, report)
        report.note(f"agent {cfg.get('name')!r} ({label}): model={cfg.get('model')}, tools={tool_names}, "
                    f"instruction={len(instruction)} chars")
    if len(models) != 1:
        report.error(f"all agents must share exactly one base model, found {sorted(models)}")
    for model in models - SUPPORTED_MODELS:
        report.error(f"model {model!r} is not supported (supported: {sorted(SUPPORTED_MODELS)})")


def check_eval_config(root: Path, report: Report) -> None:
    path = root / "eval_config.yaml"
    if not path.is_file():
        report.warn("no eval_config.yaml: runtime defaults will apply")
        return
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise CheckError(f"invalid runtime budget YAML: {exc}") from exc
    evaluation = data.get("evaluation") if isinstance(data, dict) else None
    if not isinstance(evaluation, dict):
        report.error("eval_config.yaml must contain an `evaluation:` mapping")
        return
    for key, value in evaluation.items():
        if key not in EVAL_KEYS:
            report.warn(f"unknown runtime budget key: {key}")
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
            report.error(f"evaluation.{key} must be a finite positive number")
        elif key in {"max_tool_calls", "max_turns", "timeout_seconds"} and not isinstance(value, int):
            report.error(f"evaluation.{key} must be an integer")
    report.note(f"per-task budget: {evaluation}")


# ------------------------------------------------------------- runtime compile


def compile_runtime(root: Path, report: Report) -> None:
    try:
        from adk_submission import (
            ModelRegistry,
            compile_submission,
            discover_adapters,
            discover_declared_models,
            validate_directory,
        )
        from google.adk.models.lite_llm import LiteLlm
        from swegemma.config import build_submission_limits
        from swegemma.tools import create_tools
    except ImportError:
        report.error("runtime dependencies are not installed; see the runtime setup instructions in README.md")
        return

    limits, gen_constraints = build_submission_limits()
    try:
        validate_directory(root, limits=limits)
        models = ModelRegistry()
        endpoint = "http://127.0.0.1:8000/v1"
        for model in discover_declared_models(root):
            models.register(model, LiteLlm(model=f"openai/{model}", api_base=endpoint, api_key="EMPTY"))
        manifest = discover_adapters(root, adapter_extensions={".safetensors"})
        for name in getattr(manifest, "adapters", {}) or {}:
            models.register(name, LiteLlm(model=f"openai/{name}", api_base=endpoint, api_key="EMPTY"))
        agent = compile_submission(
            submission_dir=root,
            tool_registry=create_tools(SimpleNamespace()),
            model_registry=models,
            limits=limits,
            generation_constraints=gen_constraints,
            adapter_manifest=manifest,
        )
    except Exception as exc:  # the compiler raises many error types; report all of them
        report.error(f"runtime compile failed: {type(exc).__name__}: {exc}")
        return
    report.note(f"runtime compile OK: root agent {agent.name!r} ({type(agent).__name__})")


# -------------------------------------------------------------------------- main


def validate(root: Path, runtime: bool = False) -> Report:
    report = Report()
    root = root.resolve()
    if not root.is_dir():
        report.error(f"agent directory does not exist: {root}")
        return report
    try:
        check_files(root, report)
        root_cfg = find_root_config(root, report)
        adapters = check_adapters(root, report)
        if root_cfg is not None:
            check_agents(root_cfg, root, adapters, report)
        check_eval_config(root, report)
    except (CheckError, OSError) as exc:
        report.error(str(exc))
    if runtime and not report.errors:
        compile_runtime(root, report)
    return report


def validate_zip(zip_path: Path, runtime: bool = False) -> Report:
    report = Report()
    try:
        with zipfile.ZipFile(zip_path) as archive:
            entries = archive.infolist()
            if len(entries) > MAX_FILE_COUNT or sum(i.file_size for i in entries) >= MAX_TOTAL_BYTES:
                report.error("archive exceeds bundle size or file-count limits")
                return report
            if any(i.filename.startswith("/") or ".." in Path(i.filename).parts
                   or "\\" in i.filename or stat.S_ISLNK(i.external_attr >> 16) for i in entries):
                report.error("archive contains unsafe paths or symlinks")
                return report
            if len({i.filename for i in entries}) != len(entries):
                report.error("archive contains duplicate paths")
                return report
            with tempfile.TemporaryDirectory() as tmp:
                archive.extractall(tmp)
                return validate(Path(tmp), runtime=runtime)
    except (OSError, zipfile.BadZipFile, RuntimeError) as exc:
        report.error(f"cannot read bundle: {exc}")
        return report
