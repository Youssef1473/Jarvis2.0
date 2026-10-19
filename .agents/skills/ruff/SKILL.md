---
name: ruff
description: "Ultra-fast Python linter and code formatter written in Rust. Use for instantaneous compiler/syntax diagnostics, undefined name checking, and safe import/code fixes."
---

# Ruff Skill

`ruff` is an extremely fast Python linter and formatter, written in Rust. It replaces Flake8, isort, pyupgrade, and bandit with sub-20ms execution times.

## Location
- Binary: `.venv\Scripts\ruff.exe`
- Python tool in Jarvis: `check_code_diagnostics(target_path, select, fix, max_lines)`

## CLI Usage (Windows PowerShell)

```powershell
# Check diagnostics on a single file (concise, no colors)
& "C:\Users\Youssef\Projects\.venv\Scripts\ruff.exe" check --color never --output-format concise jarvis\core\safety.py

# Check only syntax errors and undefined variables
& "C:\Users\Youssef\Projects\.venv\Scripts\ruff.exe" check --color never --select E9,F821 <path>

# Check entire directory or module
& "C:\Users\Youssef\Projects\.venv\Scripts\ruff.exe" check --color never --output-format concise jarvis\skills\

# Automatically apply safe fixes
& "C:\Users\Youssef\Projects\.venv\Scripts\ruff.exe" check --fix <path>
```

## When to Use in Workflow
- **Before running unit tests:** Run `ruff check --select E9,F821 <modified_file>` to verify no syntax errors or unresolved names exist before launching python test processes.
- **After code edits:** Run `check_code_diagnostics` to ensure clean imports and code hygiene.
