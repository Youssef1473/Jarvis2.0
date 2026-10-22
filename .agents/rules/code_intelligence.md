---
trigger: model_decision
description: Code intelligence, structural AST search with ast-grep, fast static diagnostics with ruff, and token-efficient context slicing.
---

# Code Intelligence: AST-Grep, Ruff & Context Slicing

This workspace supports `ast-grep` (tree-sitter AST engine) and `ruff` (Rust static analyzer & linter) via the virtual environment (`.venv\Scripts\` or `..\.venv\Scripts\`).

## Quick Reference Commands

```powershell
# Structural AST search (use single quotes to avoid PowerShell variable expansion)
ast-grep run -p '<pattern>' <path>

# Instant syntax and undefined-name diagnostics
ruff check <path>

# Safe auto-fix for imports and lint issues
ruff check --fix <path>

# Outline class and function signatures before reading large files
python tools/agy_context_slicer.py <path>
```

> **Executable Discovery:** If `ast-grep` or `ruff` are not directly on `PATH`, invoke them via the workspace virtualenv (e.g. `& "..\.venv\Scripts\ruff.exe"` or `& ".venv\Scripts\ruff.exe"`).

---

## Detailed Workflow & Progressive Guide

### 1. Structural AST Search (`ast-grep`)
- Prefer `ast-grep` over line-based regex when searching function signatures, decorators, method calls, or AST patterns (e.g. `def $FUNC($$$ARGS):`, `$OBJ.remember_fact($$$ARGS)`).
- When matching functions or classes in Python, provide complete bodies or body ellipses:
  - `def $FUNC($$$ARGS): $$$BODY`
  - `class $NAME($$$PARENTS): $$$BODY`
- Always use single quotes in PowerShell (`'...'`) around patterns containing `$` to prevent shell variable expansion.

### 2. Fast Static Diagnostics (`ruff`)
- Run `ruff check <path>` before and after editing files to catch syntax errors, missing imports, and undefined names in <20ms.
- To focus strictly on fatal syntax and undefined names:
  ```powershell
  ruff check --select E9,F821 <path>
  ```

### 3. Context Slicing (`tools/agy_context_slicer.py`)
- Avoid reading entire >300-line files when exploring unfamiliar modules.
- Run `python tools/agy_context_slicer.py <path>` to extract top-level classes and method signatures with line numbers, then read targeted line ranges.

### 4. Triaging Hierarchy
1. **Macro Level (Inter-module dependencies):** Consult `docs/architecture/PROJECT_MAP.md` or Graphify (`graphify query "<concept>"`).
2. **Structural Skeleton:** Use `python tools/agy_context_slicer.py <file>` for outlines.
3. **Micro Level (Syntax patterns):** Use `ast-grep` for structural matching.
4. **Diagnostic Gate:** Run `ruff check <path>` before running test suites.
