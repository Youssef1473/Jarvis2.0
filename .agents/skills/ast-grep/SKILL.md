---
name: ast-grep
description: "Ultra-fast structural code search, linting, and AST rewriting via tree-sitter. Use when searching for specific syntax patterns, function declarations, method calls, or refactoring code structurally."
---

# ast-grep (`sg`) Skill

`ast-grep` is a CLI tool for code structural search, lint, and rewriting powered by Tree-sitter. It operates on the AST instead of naive text lines.

## Location
- Binary: `.venv\Scripts\ast-grep.exe` (or `sg.exe`)
- Python tool in Jarvis: `ast_search_codebase(pattern, path, lang, max_lines)`

## CLI Usage (Windows PowerShell)

```powershell
# Search for function declarations in Python
& "C:\Users\Youssef\Projects\.venv\Scripts\ast-grep.exe" run -p 'def $FUNC($$$ARGS): $$$BODY' jarvis\core\safety.py

# Search for class definitions
& "C:\Users\Youssef\Projects\.venv\Scripts\ast-grep.exe" run -p 'class $NAME: $$$BODY' jarvis\skills\

# Search for method invocations
& "C:\Users\Youssef\Projects\.venv\Scripts\ast-grep.exe" run -p '$OBJ.remember_fact($$$ARGS)' .

# Structured JSON output
& "C:\Users\Youssef\Projects\.venv\Scripts\ast-grep.exe" run --json=compact -p 'def $FUNC($$$ARGS): $$$BODY' jarvis\core\safety.py
```

## Pattern Syntax Rules
1. In PowerShell, enclose patterns in single quotes `'...'` so that `$VAR` and `$$$VARS` are not treated as shell variables.
2. Single wildcard: `$NAME` matches a single AST node (e.g., an identifier, literal, or expression).
3. Multi wildcard: `$$$ARGS` matches zero or more AST nodes (e.g., arguments, parameters, or statements in a body).
4. For Python blocks, always match the body with `$$$BODY`:
   - `def $FUNC($$$ARGS): $$$BODY`
   - `class $NAME($$$PARENTS): $$$BODY`
   - `try: $$$TRY except $ERR: $$$EXCEPT`
