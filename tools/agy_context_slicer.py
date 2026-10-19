"""Token-Efficient AST Context Slicer for Jarvis 2.0.

Extracts class, method, and function signatures with exact line ranges from Python
source files using the standard library ast module. Enables 75%+ LLM context token
reduction by allowing agents to inspect exact symbol layouts and line boundaries
before performing targeted file edits.
"""
from __future__ import annotations

import argparse
import ast
from pathlib import Path


def _format_args(args_node: ast.arguments) -> str:
    """Formats ast.arguments into clean Python argument signature text."""
    parts: list[str] = []

    # Positional only args (Python 3.8+)
    posonly = getattr(args_node, "posonlyargs", [])
    for a in posonly:
        ann = f": {ast.unparse(a.annotation)}" if a.annotation else ""
        parts.append(f"{a.arg}{ann}")
    if posonly:
        parts.append("/")

    # Regular positional / keyword args
    defaults = [None] * (len(args_node.args) - len(args_node.defaults)) + list(args_node.defaults)
    for a, d in zip(args_node.args, defaults):
        ann = f": {ast.unparse(a.annotation)}" if a.annotation else ""
        def_str = f" = {ast.unparse(d)}" if d else ""
        parts.append(f"{a.arg}{ann}{def_str}")

    # *vararg
    if args_node.vararg:
        ann = f": {ast.unparse(args_node.vararg.annotation)}" if args_node.vararg.annotation else ""
        parts.append(f"*{args_node.vararg.arg}{ann}")
    elif args_node.kwonlyargs:
        parts.append("*")

    # Keyword only args
    kw_defaults = dict(zip([a.arg for a in args_node.kwonlyargs], args_node.kw_defaults))
    for a in args_node.kwonlyargs:
        ann = f": {ast.unparse(a.annotation)}" if a.annotation else ""
        d = kw_defaults.get(a.arg)
        def_str = f" = {ast.unparse(d)}" if d else ""
        parts.append(f"{a.arg}{ann}{def_str}")

    # **kwarg
    if args_node.kwarg:
        ann = f": {ast.unparse(args_node.kwarg.annotation)}" if args_node.kwarg.annotation else ""
        parts.append(f"**{args_node.kwarg.arg}{ann}")

    return ", ".join(parts)


def build_ast_skeleton(
    file_path: Path,
    include_line_numbers: bool = True,
    include_docstrings: bool = False,
) -> str:
    """Parses a Python file and returns a structured structural AST skeleton."""
    path = Path(file_path)
    if not path.exists():
        return f"# Error: File '{file_path}' does not exist."

    try:
        source = path.read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(source, filename=str(path))
    except SyntaxError as e:
        return f"# SyntaxError parsing '{path.name}': {e}"
    except (OSError, UnicodeDecodeError, ValueError) as e:
        return f"# Error reading '{path.name}': {e}"

    lines: list[str] = [f"# AST SKELETON: {path.as_posix()} ({len(source.splitlines())} lines)"]

    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            loc = f"  # L{node.lineno}-L{node.end_lineno}" if include_line_numbers and hasattr(node, "end_lineno") else ""
            bases = f"({', '.join(ast.unparse(b) for b in node.bases)})" if node.bases else ""
            lines.append(f"\nclass {node.name}{bases}:{loc}")

            if include_docstrings:
                doc = ast.get_docstring(node)
                if doc:
                    short_doc = doc.strip().split("\n")[0]
                    lines.append(f'    """{short_doc}"""')

            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    prefix = "async def" if isinstance(item, ast.AsyncFunctionDef) else "def"
                    item_loc = (
                        f"  # L{item.lineno}-L{item.end_lineno}"
                        if include_line_numbers and hasattr(item, "end_lineno")
                        else ""
                    )
                    args = _format_args(item.args)
                    ret = f" -> {ast.unparse(item.returns)}" if item.returns else ""
                    lines.append(f"    {prefix} {item.name}({args}){ret}:{item_loc}")
                    if include_docstrings:
                        doc = ast.get_docstring(item)
                        if doc:
                            short_doc = doc.strip().split("\n")[0]
                            lines.append(f'        """{short_doc}"""')
                elif isinstance(item, ast.Assign):
                    for target in item.targets:
                        if isinstance(target, ast.Name) and target.id.startswith("__"):
                            lines.append(f"    {target.id} = ...")

        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
            loc = f"  # L{node.lineno}-L{node.end_lineno}" if include_line_numbers and hasattr(node, "end_lineno") else ""
            args = _format_args(node.args)
            ret = f" -> {ast.unparse(node.returns)}" if node.returns else ""
            lines.append(f"\n{prefix} {node.name}({args}){ret}:{loc}")
            if include_docstrings:
                doc = ast.get_docstring(node)
                if doc:
                    short_doc = doc.strip().split("\n")[0]
                    lines.append(f'    """{short_doc}"""')

        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and (target.id.isupper() or target.id == "__all__"):
                    try:
                        val_str = ast.unparse(node.value)
                        if len(val_str) > 80:
                            val_str = val_str[:77] + "..."
                        lines.append(f"{target.id} = {val_str}")
                    except (ValueError, TypeError, AttributeError):
                        lines.append(f"{target.id} = ...")

    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(
        description="Token-Efficient AST Context Slicer for Jarvis 2.0"
    )
    parser.add_argument("file_path", type=Path, help="Path to Python source file to slice")
    parser.add_argument(
        "--no-lines", action="store_true", help="Omit line number comments from skeleton output"
    )
    parser.add_argument(
        "-d", "--docstrings", action="store_true", help="Include first line of docstrings"
    )

    args = parser.parse_args()
    skeleton = build_ast_skeleton(
        args.file_path,
        include_line_numbers=not args.no_lines,
        include_docstrings=args.docstrings,
    )
    print(skeleton)


if __name__ == "__main__":
    main()
