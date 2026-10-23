---
name: ast-grep
description: "Ultra-fast structural code search, linting, and AST rewriting via tree-sitter. Use when searching for specific syntax patterns, function declarations, method calls, or refactoring code structurally."
---

# ast-grep (`sg`) Skill

`ast-grep` is a fast code structural search, lint, and rewriting tool powered by Tree-sitter. It operates on the AST instead of naive text lines.

## Location & Resolution
- Canonical wrapper: `python tools/diagnostics.py search '<pattern>' <path>`
- Binary discovery: Automatically resolved across `.venv\Scripts\`, `..\.venv\Scripts\`, and `PATH` by `tools/diagnostics.py`
- Python tool in Jarvis runtime: `ast_search_codebase(pattern, path, lang, max_lines)`
- Project invariant rules: `.agents/rules/ast-grep/` (configured via `sgconfig.yml`)

## CLI Usage (Windows PowerShell)

```powershell
# Unified, portable diagnostic search (no hardcoded paths)
python tools/diagnostics.py search 'def $FUNC($$$ARGS): $$$BODY' jarvis/core/safety.py

# Search for class definitions across skills
python tools/diagnostics.py search 'class $NAME: $$$BODY' jarvis/skills/

# Search for method invocations
python tools/diagnostics.py search '$OBJ.remember_fact($$$ARGS)' .

# Run project architectural invariant rules (.agents/rules/ast-grep)
python tools/diagnostics.py scan <path>

# Direct native CLI execution (when ast-grep or sg is on PATH)
ast-grep run -p 'def $FUNC($$$ARGS): $$$BODY' jarvis/core/safety.py
```

## Pattern Syntax Rules
1. In PowerShell, always enclose patterns in single quotes `'...'` so that `$VAR` and `$$$VARS` are not evaluated as shell variables.
2. Single wildcard: `$NAME` matches a single AST node (e.g., an identifier, literal, or expression).
3. Multi wildcard: `$$$ARGS` matches zero or more AST nodes (e.g., arguments, parameters, or statements in a body).
4. For Python blocks, always match the body with `$$$BODY`:
   - `def $FUNC($$$ARGS): $$$BODY`
   - `class $NAME($$$PARENTS): $$$BODY`
   - `try: $$$TRY except $ERR: $$$EXCEPT`
