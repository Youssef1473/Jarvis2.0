## Code Intelligence: AST-Grep & Ruff

This project has `ast-grep` (Rust tree-sitter AST engine) and `ruff` (Rust static analyzer & linter) installed in `.venv\Scripts\`.

### Directives:
1. **Intra-File & Structural AST Search (`ast-grep`):**
   - For searching syntax structures, function signatures, decorators, method calls, or AST patterns (e.g. `def $FUNC($$$ARGS):`, `$OBJ.remember_fact($$$ARGS)`), prefer `ast-grep` over raw regex or line-based grep.
   - Command: `& "C:\Users\Youssef\Projects\.venv\Scripts\ast-grep.exe" run -p '<pattern>' <path>`
   - In PowerShell, always use single quotes `'...'` around patterns containing `$` to prevent PowerShell variable expansion.
   - When matching functions/classes in Python, remember AST nodes require complete bodies or body ellipses: `def $FUNC($$$ARGS): $$$BODY` or `class $NAME($$$PARENTS): $$$BODY`.
   - In ast-grep 0.51+, use `ast-grep` (`sg` is deprecated).

2. **Instant Compiler & Syntax Diagnostics (`ruff`):**
   - Before and after modifying Python files, run `ruff check` on the target file/directory to immediately catch syntax errors, missing imports, and undefined names in <20ms.
   - Command: `& "C:\Users\Youssef\Projects\.venv\Scripts\ruff.exe" check --color never <path>`
   - For syntax/undefined name triage only: `ruff check --select E9,F821 <path>`
   - For safe auto-fixing: `ruff check --fix <path>`

3. **Multi-Scale Triaging Hierarchy:**
   - **Macro Level (Inter-file relationships):** Use Graphify (`graphify query "<concept>"`, `query_codebase_graph`) to understand caller/callee graphs and blast-radius.
   - **Micro Level (Structural AST queries):** Use `ast-grep` (`ast_search_codebase`) for structural AST pattern matching.
   - **Diagnostic Gate:** Use `ruff` (`check_code_diagnostics`) for instant static analysis and lint/syntax verification before executing unit tests.
