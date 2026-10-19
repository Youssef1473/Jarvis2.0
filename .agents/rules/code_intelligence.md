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
   - **Structural Outline & Skeleton:** Use `python tools/agy_context_slicer.py <file>` to inspect class and function signatures with exact line ranges before reading large files.
   - **Diagnostic Gate:** Use `ruff` (`check_code_diagnostics`) for instant static analysis and lint/syntax verification before executing unit tests.

4. **Token-Efficient Context Slicing (`tools/agy_context_slicer.py`):**
   - Never dump or read entire >300-line source files into context.
   - Command: `& "C:\Users\Youssef\Projects\.venv\Scripts\python.exe" tools/agy_context_slicer.py <path>`
   - Extracts symbol signatures and line numbers in <10ms to achieve 75%+ context token reduction. Target specific line ranges with `view_file` using the extracted line ranges.

5. **Antigravity CLI Perfection & 4-Point Verification Matrix:**
   - Before editing any file, verify:
     1. **Public Signatures & Shims:** Never alter or remove existing interfaces or deprecation shims imported by `tests/`.
     2. **Thread/Event-Loop Ownership:** Zero blocking calls on WASAPI playback, Kokoro neural synthesis, Whisper STT, or FastAPI/Starlette event loops.
     3. **Compute Hardware Envelope:** Respect the 4GB RTX 3050 VRAM ceiling; enforce CPU thread pinning (`inter_op_num_threads = 1`, `allow_spinning = "0"`).
     4. **Win32 & SQLite WAL Lifecycle:** Release all Win32 handles and SQLite read-marks (`isolation_level = None`, `conn.commit()`) to prevent WAL tail pinning.
   - Run tests cleanly via `tools/run_fast_tests.py` to prevent context pollution.
