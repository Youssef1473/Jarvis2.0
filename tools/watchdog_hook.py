"""PreToolUse Safety Watchdog Hook for Jarvis 2.0.

Provides conservative, pre-execution repository policy enforcement to block
unintended mutations, broad staging, destructive commands, and unconfined
file writes before Antigravity dispatches the corresponding tool calls.

Decision Contract:
  - "allow": Safe operation allowed to execute automatically.
  - "deny": Prohibited operation hard-blocked before execution.
  - "force_ask": Sensitive or ambiguous operation requiring explicit human confirmation.

All diagnostics and errors are emitted to stderr. Stdout is reserved exclusively
for the single JSON decision payload: {"decision": "<allow|deny|force_ask>", "reason": "..."}
"""
from __future__ import annotations

import json
import shlex
import sys
from pathlib import Path
from typing import Any

# Tools monitored for mutating shell execution
COMMAND_TOOLS = {"run_command"}

# Tools monitored for mutating filesystem operations
FILE_EDIT_TOOLS = {
    "edit_file",
    "write_file",
    "replace_file_content",
    "write_to_file",
    "multi_replace_file_content",
}

# Parameter keys normalized to canonical path extractors
PATH_KEYS_CANONICAL = {"targetfile", "path", "filepath"}
MULTI_PATH_KEYS_CANONICAL = {"files", "paths"}

# Protected safety configuration files requiring explicit confirmation (force_ask)
PROTECTED_SAFETY_FILES = {
    "agents.md",
    "hooks.json",
    "sgconfig.yml",
}

PROTECTED_SAFETY_DIRS = {
    Path(".agents/rules"),
    Path(".agents/agents"),
}

# Protected credential and sensitive configuration filenames / extensions
PROTECTED_CREDENTIAL_FILES = {
    ".env",
    "config.yaml",
}

PROTECTED_CREDENTIAL_EXTENSIONS = {
    ".pem",
    ".key",
    ".p12",
    ".pfx",
}

# Windows and cross-platform destructive command executables
DESTRUCTIVE_COMMAND_BINARIES = {
    "format",
    "diskpart",
    "mkfs",
}


def _safe_stderr(msg: str) -> None:
    """Writes sanitized diagnostic message to stderr."""
    try:
        sys.stderr.write(f"[SafetyWatchdog] {msg}\n")
        sys.stderr.flush()
    except (OSError, UnicodeEncodeError):
        pass


def _emit_decision(decision: str, reason: str | None = None) -> int:
    """Emits single JSON decision object to stdout."""
    payload: dict[str, str] = {"decision": decision}
    if reason:
        payload["reason"] = reason
    try:
        sys.stdout.write(json.dumps(payload) + "\n")
        sys.stdout.flush()
    except (OSError, UnicodeEncodeError):
        pass
    return 0


def extract_target_paths_from_args(args: dict[str, Any]) -> list[str]:
    """Extracts target file path(s) from tool call arguments in a case- and delimiter-insensitive manner."""
    candidates: list[str] = []

    for raw_key, val in args.items():
        norm_key = str(raw_key).lower().replace("_", "")
        if norm_key in PATH_KEYS_CANONICAL and isinstance(val, str) and val.strip():
            candidates.append(val.strip())
        elif norm_key in MULTI_PATH_KEYS_CANONICAL and isinstance(val, list):
            for item in val:
                if isinstance(item, str) and item.strip():
                    candidates.append(item.strip())
                elif isinstance(item, dict):
                    for nested_key, nested_val in item.items():
                        norm_nested = str(nested_key).lower().replace("_", "")
                        if norm_nested in PATH_KEYS_CANONICAL and isinstance(nested_val, str) and nested_val.strip():
                            candidates.append(nested_val.strip())

    return list(dict.fromkeys(candidates))


def _split_command_segments(command_line: str) -> list[list[str]]:
    """Tokenizes command line into separate statements separated by chaining operators.

    Separators: &&, ||, ;, &, |, and newlines.
    Preserves quoted strings intact.
    """
    clean_cmd = command_line.strip()
    if not clean_cmd:
        return []

    try:
        raw_tokens = list(shlex.shlex(clean_cmd, posix=False, punctuation_chars=True))
    except (ValueError, SyntaxError):
        # Unclosed quotes or syntax error in shell string; split by whitespace as fallback
        raw_tokens = clean_cmd.split()

    statements: list[list[str]] = []
    current_statement: list[str] = []

    for tok in raw_tokens:
        if tok in ("&&", "||", ";", "&", "|", "\n"):
            if current_statement:
                statements.append(current_statement)
                current_statement = []
        else:
            current_statement.append(tok)

    if current_statement:
        statements.append(current_statement)

    return statements


def _strip_quotes(tok: str) -> str:
    """Strips leading and trailing single or double quotes from token."""
    if len(tok) >= 2 and (
        (tok.startswith('"') and tok.endswith('"')) or (tok.startswith("'") and tok.endswith("'"))
    ):
        return tok[1:-1]
    return tok


def _normalize_binary_name(tok: str) -> str:
    """Extracts base command name, stripping path prefixes and Windows executable extensions."""
    unquoted = _strip_quotes(tok)
    # Extract filename from path if path was provided
    base = Path(unquoted).name
    lower = base.lower()
    for ext in (".exe", ".cmd", ".bat", ".ps1", ".sh"):
        if lower.endswith(ext):
            lower = lower[: -len(ext)]
            break
    return lower


def evaluate_command_statement(tokens: list[str]) -> tuple[str, str | None]:
    """Evaluates a single command statement.

    Returns:
        (decision, reason) where decision is 'allow', 'deny', or 'force_ask'.
    """
    if not tokens:
        return "allow", None

    # Strip quotes from binary
    bin_name = _normalize_binary_name(tokens[0])
    raw_args = [_strip_quotes(t) for t in tokens[1:]]

    # Handle shell wrappers: cmd /c <inner>, powershell -c <inner>, sh -c <inner>
    if bin_name in ("cmd", "powershell", "pwsh", "sh", "bash"):
        # Check for encoded PowerShell commands
        full_statement_lower = " ".join(tokens).lower()
        if any(enc_flag in full_statement_lower for enc_flag in ("-encodedcommand", "-enc", "frombase64string")):
            return "force_ask", "Encoded PowerShell command execution requires explicit confirmation."
        if any(eval_kw in full_statement_lower for eval_kw in ("invoke-expression", "iex ")):
            return "force_ask", "Dynamic expression evaluation (Invoke-Expression) requires explicit confirmation."

        # If wrapped command is passed with /c, -c, -command
        for idx, arg in enumerate(raw_args):
            arg_lower = arg.lower()
            if arg_lower in ("/c", "-c", "-command") and idx + 1 < len(raw_args):
                inner_cmd = " ".join(raw_args[idx + 1 :])
                inner_segments = _split_command_segments(inner_cmd)
                if inner_segments:
                    worst_decision = "allow"
                    worst_reason: str | None = None
                    for seg in inner_segments:
                        dec, rsn = evaluate_command_statement(seg)
                        if dec == "deny":
                            return "deny", rsn
                        if dec == "force_ask" and worst_decision == "allow":
                            worst_decision = "force_ask"
                            worst_reason = rsn
                    if worst_decision != "allow":
                        return worst_decision, worst_reason

    # Prohibited standalone destructive system binaries
    if bin_name in DESTRUCTIVE_COMMAND_BINARIES:
        return "deny", f"Destructive system command '{bin_name}' is unconditionally prohibited."

    # Windows recursive directory deletion: rmdir /s, rd /s
    if bin_name in ("rmdir", "rd"):
        lower_args = [a.lower() for a in raw_args]
        if any(flag in lower_args for flag in ("/s", "-s")):
            return "deny", "Destructive recursive directory removal ('rmdir /s') is prohibited."

    # Unix recursive removal: rm -rf
    if bin_name == "rm":
        lower_args = [a.lower() for a in raw_args]
        has_r = any("r" in a.lstrip("-") for a in lower_args if a.startswith("-") and not a.startswith("--"))
        has_f = any("f" in a.lstrip("-") for a in lower_args if a.startswith("-") and not a.startswith("--"))
        if (has_r and has_f) or "--recursive" in lower_args:
            return "deny", "Destructive recursive file removal ('rm -rf') is prohibited."

    # Mass file deletion: del /s *
    if bin_name in ("del", "erase"):
        lower_args = [a.lower() for a in raw_args]
        if any(flag in lower_args for flag in ("/s", "-s")) and any("*" in a for a in lower_args):
            return "deny", "Destructive bulk file deletion ('del /s *') is prohibited."

    # Git command analysis
    if bin_name == "git":
        return _evaluate_git_command(raw_args)

    return "allow", None


def _evaluate_git_command(git_args: list[str]) -> tuple[str, str | None]:
    """Evaluates git subcommand and arguments."""
    if not git_args:
        return "allow", None

    # Skip global git flags (e.g., -C <path>, -c <key=val>, --git-dir=<path>)
    subcommand_idx = 0
    while subcommand_idx < len(git_args):
        tok = git_args[subcommand_idx]
        if tok.startswith("-"):
            if tok in ("-C", "-c") and subcommand_idx + 1 < len(git_args):
                subcommand_idx += 2
            else:
                subcommand_idx += 1
        else:
            break

    if subcommand_idx >= len(git_args):
        return "allow", None

    subcommand = git_args[subcommand_idx].lower()
    sub_args = git_args[subcommand_idx + 1 :]

    # 1. Broad Git Staging: git add . / git add -A / git add --all
    if subcommand == "add":
        # Check for broad staging tokens
        for arg in sub_args:
            arg_clean = arg.strip()
            # Exact standalone dot or dot with trailing slash
            if arg_clean in (".", "./", ".\\"):
                return "deny", "Broad Git staging ('git add .') is prohibited. Stage explicit file paths."
            if arg_clean in ("-A", "--all"):
                return "deny", "Broad Git staging ('git add -A / --all') is prohibited. Stage explicit file paths."
            if arg_clean in ("*", ":/", ".:/"):
                return "deny", "Broad/wildcard Git staging ('git add *') is prohibited. Stage explicit file paths."
        return "allow", None

    # 2. Destructive Git Reset: git reset --hard
    if subcommand == "reset":
        for arg in sub_args:
            if arg.lower() in ("--hard", "-hard"):
                return "deny", "Destructive 'git reset --hard' is prohibited. Use soft reset or explicit path restore."
        return "allow", None

    # 3. Destructive Git Clean: git clean -f / git clean --force
    if subcommand == "clean":
        lower_sub = [a.lower() for a in sub_args]
        has_f = any("f" in a.lstrip("-") for a in lower_sub if a.startswith("-") and not a.startswith("--"))
        if has_f or "--force" in lower_sub:
            return "deny", "Destructive 'git clean --force' is prohibited."
        return "allow", None

    # 4. Destructive Git Checkout / Restore of working tree
    if subcommand in ("checkout", "restore"):
        lower_sub = [a.lower() for a in sub_args]
        # Rejects 'git checkout .' or 'git checkout -- .' or 'git restore .'
        if "." in lower_sub or "./" in lower_sub or ".\\" in lower_sub:
            return "deny", f"Destructive working tree discard ('git {subcommand} .') is prohibited."
        return "allow", None

    # 5. Destructive Force Branch Deletion: git branch -D
    if subcommand == "branch":
        if "-D" in sub_args:
            return "deny", "Force branch deletion ('git branch -D') is prohibited. Use -d for safe deletion."
        return "allow", None

    # 6. Force-Push: git push --force / git push -f
    if subcommand == "push":
        lower_sub = [a.lower() for a in sub_args]
        if any(f in lower_sub for f in ("--force", "-f", "--force-with-lease", "--force-if-includes")):
            return "deny", "Force-pushing to remote repository is prohibited."
        return "allow", None

    # 7. Direct Git Commit: requires human review and confirmation of pre-commit verification
    if subcommand == "commit":
        return "force_ask", "Direct git commit requires explicit confirmation and verification gate review."

    return "allow", None


def evaluate_command_payload(payload: dict[str, Any]) -> tuple[str, str | None]:
    """Evaluates run_command tool payload against watchdog policy."""
    args = payload.get("toolCall", {}).get("args", {})
    if not isinstance(args, dict):
        return "deny", "Invalid tool call arguments."

    command_line = args.get("CommandLine") or args.get("command") or args.get("commandLine")
    if not isinstance(command_line, str) or not command_line.strip():
        return "deny", "Command line argument is missing or empty."

    statements = _split_command_segments(command_line)
    if not statements:
        return "deny", "Empty command statement."

    worst_decision = "allow"
    worst_reason: str | None = None

    for statement in statements:
        decision, reason = evaluate_command_statement(statement)
        if decision == "deny":
            return "deny", reason
        if decision == "force_ask" and worst_decision == "allow":
            worst_decision = "force_ask"
            worst_reason = reason

    return worst_decision, worst_reason


def _is_contained(target: Path, root: Path) -> bool:
    """Checks whether target path is strictly contained within root."""
    try:
        target.relative_to(root)
        return True
    except ValueError:
        return False


def evaluate_file_payload(payload: dict[str, Any], default_root: Path) -> tuple[str, str | None]:
    """Evaluates mutating file tool payload against watchdog path policy."""
    args = payload.get("toolCall", {}).get("args", {})
    if not isinstance(args, dict):
        return "deny", "Invalid tool call arguments."

    path_strings = extract_target_paths_from_args(args)
    if not path_strings:
        return "deny", "Target file path is missing or could not be determined."

    # Retrieve workspace roots from payload, fallback to default_root
    workspace_paths = payload.get("workspacePaths")
    permitted_roots: list[Path] = []
    if isinstance(workspace_paths, list) and workspace_paths:
        for wp in workspace_paths:
            try:
                permitted_roots.append(Path(wp).resolve())
            except (OSError, RuntimeError):
                pass
    if not permitted_roots:
        permitted_roots = [default_root.resolve()]

    worst_decision = "allow"
    worst_reason: str | None = None

    for raw_path_str in path_strings:
        clean_path = raw_path_str.strip()

        # Reject UNC paths
        if clean_path.startswith((r"\\", "//")):
            return "deny", f"UNC network paths are prohibited: {clean_path[:30]}"

        # Resolve path
        try:
            cand = Path(clean_path)
            # If relative, resolve against first permitted root
            resolved = cand.resolve() if cand.is_absolute() else (permitted_roots[0] / cand).resolve()
        except (OSError, RuntimeError) as err:
            return "deny", f"Cannot safely resolve path: {err}"

        # 1. Boundary check: must be inside at least one permitted workspace root
        if not any(_is_contained(resolved, root) for root in permitted_roots):
            return "deny", f"Path resolves outside permitted workspace roots: {resolved.name}"

        # 2. Unconditional .git directory protection
        for root in permitted_roots:
            git_dir = root / ".git"
            if resolved == git_dir or _is_contained(resolved, git_dir):
                return "deny", "Direct modification of repository internal .git state is unconditionally prohibited."

        # 3. Protected safety configuration files (force_ask)
        resolved_name = resolved.name.lower()
        if resolved_name in PROTECTED_SAFETY_FILES:
            return "force_ask", f"Modification to protected safety configuration requires explicit confirmation: {resolved.name}"

        for s_dir in PROTECTED_SAFETY_DIRS:
            for root in permitted_roots:
                target_safety_dir = root / s_dir
                if resolved == target_safety_dir or _is_contained(resolved, target_safety_dir):
                    return "force_ask", f"Modification to protected safety directory requires explicit confirmation: {resolved.name}"

        # 4. Protected credential and environment files (force_ask)
        if resolved_name in PROTECTED_CREDENTIAL_FILES or resolved_name.startswith(".env."):
            return "force_ask", f"Modification to sensitive configuration requires explicit confirmation: {resolved.name}"

        if resolved.suffix.lower() in PROTECTED_CREDENTIAL_EXTENSIONS:
            return "force_ask", f"Modification to certificate/key file requires explicit confirmation: {resolved.name}"

    return worst_decision, worst_reason


def process_hook_payload(raw_json: str, default_root: Path | None = None) -> int:
    """Parses hook stdin JSON and emits watchdog decision."""
    root = default_root if default_root is not None else Path(__file__).resolve().parent.parent

    if not raw_json.strip():
        # Empty input is not a safe state for mutating hooks
        return _emit_decision("deny", "Empty hook payload received.")

    try:
        payload = json.loads(raw_json)
        if not isinstance(payload, dict):
            return _emit_decision("deny", "Malformed hook payload: expected JSON object.")
    except (json.JSONDecodeError, UnicodeDecodeError) as err:
        return _emit_decision("deny", f"Malformed JSON in hook payload: {err}")

    tool_call = payload.get("toolCall")
    if not isinstance(tool_call, dict):
        return _emit_decision("deny", "Missing or invalid toolCall object.")

    tool_name = tool_call.get("name")
    if not isinstance(tool_name, str):
        return _emit_decision("deny", "Missing tool name in toolCall.")

    # Mutating command tools
    if tool_name in COMMAND_TOOLS:
        decision, reason = evaluate_command_payload(payload)
        if decision != "allow" and reason:
            _safe_stderr(f"Policy triggered for tool '{tool_name}': {reason}")
        return _emit_decision(decision, reason)

    # Mutating file tools
    if tool_name in FILE_EDIT_TOOLS:
        decision, reason = evaluate_file_payload(payload, root)
        if decision != "allow" and reason:
            _safe_stderr(f"Policy triggered for tool '{tool_name}': {reason}")
        return _emit_decision(decision, reason)

    # Unmonitored or read-only tools default to allow
    return _emit_decision("allow", None)


def main() -> int:
    """CLI entry point reading from stdin."""
    try:
        raw_input = sys.stdin.read()
    except (OSError, UnicodeDecodeError):
        raw_input = ""
    return process_hook_payload(raw_input)


if __name__ == "__main__":
    sys.exit(main())
