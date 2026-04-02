"""Phase 2: Extract AST skeletons using tree-sitter.

Architecture note for multi-language support:
  The phase2_extract_ast() entry point dispatches to language-specific extractors
  based on file extension. Currently only C++ is implemented. To add a new
  language (e.g. Python):
    1. Add a tree-sitter parser (e.g. tree_sitter_python)
    2. Implement extract_<lang>_file() and optionally extract_<lang>_supplementary()
    3. Add file extension mapping in phase2_extract_ast()
  The rest of the pipeline (packer, summarizer, writer) is language-agnostic.
"""

from __future__ import annotations

from pathlib import Path

import tree_sitter
import tree_sitter_cpp

from .models import ASTEntry, Component, Config

# ---------------------------------------------------------------------------
# Token estimation
# ---------------------------------------------------------------------------


# Token estimation
CHARS_PER_TOKEN = 4
MAX_ENUM_CHARS = 500
SUPPLEMENTARY_MIN_TOKENS = 50

# Skip files larger than this (bytes). Giant data files like voice synthesis
# model dumps (16MB .c files full of arrays) will freeze tree-sitter.
MAX_FILE_SIZE = 1_000_000  # 1MB


def estimate_tokens(text: str) -> int:
    """Rough token estimate based on CHARS_PER_TOKEN."""
    return max(1, len(text) // CHARS_PER_TOKEN)


# ---------------------------------------------------------------------------
# C++ extractor
# ---------------------------------------------------------------------------

_cpp_language = tree_sitter.Language(tree_sitter_cpp.language())
_cpp_parser = tree_sitter.Parser(_cpp_language)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _node_text(node: tree_sitter.Node, source: bytes) -> str:
    """Get the source text for a node."""
    return source[node.start_byte : node.end_byte].decode("utf-8", errors="replace")


# Node types that represent real C++ definitions. If any of these appear
# inside a function body, the enclosing "function" is almost certainly a
# macro-induced mis-parse (C++ does not allow nested function definitions
# or top-level class/enum definitions inside a function).
_DEFINITION_TYPES = frozenset(
    {
        "function_definition",
        "class_specifier",
        "struct_specifier",
        "enum_specifier",
        "alias_declaration",
        "type_definition",
        "template_declaration",
        "namespace_definition",
    }
)


def _has_nested_definitions(func_node: tree_sitter.Node) -> bool:
    """Check if a function_definition's body contains other definitions.

    In standard C++, a function body cannot contain nested function
    definitions, class definitions, or namespace definitions. If these
    appear, it means the enclosing "function" is actually a mis-parse
    caused by a macro (e.g. a namespace macro that tree-sitter couldn't
    understand). This detection is fully generic — it does not depend on
    knowing any specific macro names.
    """
    body = func_node.child_by_field_name("body")
    if not body:
        return False
    for child in body.children:
        if child.type in _DEFINITION_TYPES:
            return True
    return False


def _extract_class_or_struct(node: tree_sitter.Node, source: bytes) -> list[str]:
    """Extract class/struct declaration with public members only."""
    lines: list[str] = []
    kind = node.type  # "class_specifier" or "struct_specifier"
    kind_keyword = "class" if "class" in kind else "struct"

    # Get class name
    name_node = node.child_by_field_name("name")
    name = _node_text(name_node, source) if name_node else "<anonymous>"

    # Get base classes
    bases = ""
    for child in node.children:
        if child.type == "base_class_clause":
            bases = " " + _node_text(child, source)
            break

    lines.append(f"{kind_keyword} {name}{bases} {{")

    # Parse field declaration list
    field_list = node.child_by_field_name("body")
    if not field_list:
        lines.append("};")
        return lines

    # For struct, default visibility is public
    # For class, default visibility is private
    in_public = kind_keyword == "struct"

    for child in field_list.children:
        if child.type == "access_specifier":
            spec_text = _node_text(child, source).strip().rstrip(":")
            if "public" in spec_text:
                in_public = True
                lines.append("public:")
            elif "protected" in spec_text:
                in_public = False
                lines.append("protected:")
            else:
                in_public = False
            continue

        if child.type in ("{", "}"):
            continue

        if not in_public:
            continue

        # Extract public members
        if child.type == "field_declaration":
            # Could be method declaration or data member
            text = _node_text(child, source).strip()
            # Skip data members (simple heuristic: no parentheses = data member)
            if "(" in text:
                # Method declaration — keep
                lines.append(f"    {text}")
            else:
                # Data member — keep for struct, skip for class
                if kind_keyword == "struct":
                    lines.append(f"    {text}")

        elif child.type == "function_definition":
            # Inline method — extract just the signature
            decl = child.child_by_field_name("declarator")
            ret_type = child.child_by_field_name("type")
            if decl and ret_type:
                sig = f"{_node_text(ret_type, source)} {_node_text(decl, source)}"
                # Check for const/override qualifiers
                for qchild in child.children:
                    if qchild.type == "type_qualifier" and qchild.start_byte > decl.end_byte:
                        sig += f" {_node_text(qchild, source)}"
                lines.append(f"    {sig};")

        elif child.type == "template_declaration":
            # Template method in class
            text = _node_text(child, source).strip()
            # Truncate body
            brace = text.find("{")
            if brace > 0:
                text = text[:brace].rstrip() + ";"
            lines.append(f"    {text}")

        elif child.type == "friend_declaration":
            lines.append(f"    {_node_text(child, source).strip()}")

        elif child.type == "type_definition":
            lines.append(f"    {_node_text(child, source).strip()}")

        elif child.type == "alias_declaration":
            lines.append(f"    {_node_text(child, source).strip()}")

    lines.append("};")
    return lines


def _extract_enum(node: tree_sitter.Node, source: bytes) -> list[str]:
    """Extract enum/enum class declaration."""
    text = _node_text(node, source).strip()
    # Keep enum definition as-is (usually compact)
    if len(text) > MAX_ENUM_CHARS:
        # Truncate very long enums
        return [text[:MAX_ENUM_CHARS] + " /* ... */ };"]
    return [text]


def _extract_namespace(node: tree_sitter.Node, source: bytes) -> tuple[str | None, list[str]]:
    """Extract namespace name and return (ns_name, inner_lines)."""
    ns_name = None
    for child in node.children:
        if child.type == "namespace_identifier":
            ns_name = _node_text(child, source)
            break
    return ns_name, []


def _is_project_include(node: tree_sitter.Node, source: bytes) -> bool:
    """Check if #include uses quotes (project-internal)."""
    for child in node.children:
        if child.type == "string_literal":
            return True
    return False


# ---------------------------------------------------------------------------
# Full file extraction (headers + orphan .cpp)
# ---------------------------------------------------------------------------


def extract_cpp_file(file_path: Path, source: bytes) -> str:
    """Extract AST skeleton from a C++ file.

    Returns a compact text representation of the public API.
    """
    tree = _cpp_parser.parse(source)
    root = tree.root_node

    lines: list[str] = []
    current_ns: list[str] = []

    def _process_node(node: tree_sitter.Node, ns_stack: list[str]) -> None:
        for child in node.children:
            if child.type == "preproc_include":
                if _is_project_include(child, source):
                    lines.append(_node_text(child, source).strip())

            elif child.type == "namespace_definition":
                ns_name, _ = _extract_namespace(child, source)
                if ns_name:
                    lines.append(f"namespace {ns_name} {{")
                    ns_stack.append(ns_name)
                # Recurse into namespace body
                body = child.child_by_field_name("body")
                if body:
                    _process_node(body, ns_stack)
                if ns_name:
                    lines.append(f"}} // namespace {ns_name}")
                    ns_stack.pop()

            elif child.type in ("class_specifier", "struct_specifier"):
                cls_lines = _extract_class_or_struct(child, source)
                lines.extend(cls_lines)

            elif child.type == "enum_specifier":
                lines.extend(_extract_enum(child, source))

            elif child.type == "declaration":
                # Top-level function declaration (not definition)
                text = _node_text(child, source).strip()
                if "(" in text:
                    lines.append(text)

            elif child.type == "function_definition":
                if _has_nested_definitions(child):
                    # Macro-induced mis-parse: the "function body" actually
                    # contains real definitions. Recurse into the body to
                    # extract them, treating it like a scope/namespace.
                    body = child.child_by_field_name("body")
                    if body:
                        _process_node(body, ns_stack)
                else:
                    # Normal function — extract signature only
                    decl = child.child_by_field_name("declarator")
                    ret_type = child.child_by_field_name("type")
                    storage = None
                    for sc in child.children:
                        if sc.type == "storage_class_specifier":
                            storage = _node_text(sc, source)
                            break
                    if decl and ret_type:
                        sig = f"{_node_text(ret_type, source)} {_node_text(decl, source)};"
                        if storage:
                            sig = f"{storage} {sig}"
                        lines.append(sig)

            elif child.type == "template_declaration":
                # Template class or function at top level
                tmpl_text = _node_text(child, source).strip()
                # Check if it contains a class/struct
                has_class = False
                for tc in child.children:
                    if tc.type in ("class_specifier", "struct_specifier"):
                        tmpl_params = ""
                        for tp in child.children:
                            if tp.type == "template_parameter_list":
                                tmpl_params = _node_text(tp, source)
                                break
                        lines.append(f"template {tmpl_params}")
                        lines.extend(_extract_class_or_struct(tc, source))
                        has_class = True
                        break
                if not has_class:
                    # Template function — extract signature
                    brace = tmpl_text.find("{")
                    if brace > 0:
                        tmpl_text = tmpl_text[:brace].rstrip() + ";"
                    lines.append(tmpl_text)

            elif child.type == "type_definition":
                lines.append(_node_text(child, source).strip())

            elif child.type == "alias_declaration":
                lines.append(_node_text(child, source).strip())

    _process_node(root, current_ns)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Supplementary extraction (.cpp with matching .h)
# ---------------------------------------------------------------------------


def _is_member_function(node: tree_sitter.Node, source: bytes) -> bool:
    """Check if a function_definition is a class member (Foo::Bar).

    Looks at the function name (identifier) only, not the full declarator,
    because parameter types like ECS::Query also contain '::'.
    """
    decl = node.child_by_field_name("declarator")
    if decl:
        for child in decl.children:
            if child.type == "qualified_identifier":
                return True  # e.g. Foo::Bar(...)
            if child.type == "identifier":
                return False  # e.g. Bar(...)
        # Fallback: check full declarator text up to '('
        decl_text = _node_text(decl, source)
        paren = decl_text.find("(")
        name_part = decl_text[:paren] if paren > 0 else decl_text
        return "::" in name_part
    return False


def _extract_function_sig(
    node: tree_sitter.Node,
    source: bytes,
    prefix: str = "",
) -> str | None:
    """Extract signature from a function_definition node."""
    decl = node.child_by_field_name("declarator")
    ret_type = node.child_by_field_name("type")
    if not (decl and ret_type):
        return None
    storage = ""
    for sc in node.children:
        if sc.type == "storage_class_specifier":
            storage = _node_text(sc, source) + " "
            break
    return f"{prefix}{storage}{_node_text(ret_type, source)} {_node_text(decl, source)};"


def extract_cpp_supplementary(file_path: Path, source: bytes) -> str:
    """Extract supplementary content from .cpp that has a matching .h.

    The .h file provides class/struct declarations. This function extracts
    additional information from the .cpp that is NOT redundant with the .h:

    - Project-internal #include directives (dependency info)
    - Non-member function signatures (free functions, callbacks, system functions)
    - File-scope using declarations and typedefs (type aliases used in impl)
    - Template explicit instantiations
    - Anonymous namespace content (types, functions)
    - Static function signatures

    Member function definitions (e.g. void Foo::Bar()) are intentionally
    skipped — their signatures are already in the .h file.

    When a macro-induced mis-parse is detected (a "function" whose body
    contains other definitions), we recurse into the body transparently.
    This handles any engine's namespace macros without configuration.
    """
    tree = _cpp_parser.parse(source)
    root = tree.root_node

    includes: list[str] = []
    lines: list[str] = []

    def _process_toplevel(node: tree_sitter.Node, indent: str = "") -> None:
        """Process a top-level or namespace-body node."""
        for child in node.children:
            if child.type == "preproc_include":
                if _is_project_include(child, source):
                    includes.append(_node_text(child, source).strip())

            elif child.type == "namespace_definition":
                ns_name, _ = _extract_namespace(child, source)
                body = child.child_by_field_name("body")
                if ns_name is None:
                    # Anonymous namespace — extract everything inside
                    lines.append(f"{indent}namespace {{ // anonymous")
                    if body:
                        for inner in body.children:
                            if inner.type in ("class_specifier", "struct_specifier"):
                                lines.extend(_extract_class_or_struct(inner, source))
                            elif inner.type == "function_definition":
                                if _has_nested_definitions(inner):
                                    body2 = inner.child_by_field_name("body")
                                    if body2:
                                        _process_toplevel(body2, indent + "  ")
                                else:
                                    sig = _extract_function_sig(inner, source, indent + "  ")
                                    if sig:
                                        lines.append(sig)
                            elif inner.type == "enum_specifier":
                                lines.extend(_extract_enum(inner, source))
                            elif inner.type in ("type_definition", "alias_declaration"):
                                lines.append(f"{indent}  {_node_text(inner, source).strip()}")
                    lines.append(f"{indent}}} // anonymous namespace")
                else:
                    # Named namespace — recurse to find free functions etc.
                    if body:
                        pre_len = len(lines)
                        _process_toplevel(body, indent + "  ")
                        # Only emit namespace wrapper if we found something inside
                        if len(lines) > pre_len:
                            lines.insert(pre_len, f"{indent}namespace {ns_name} {{")
                            lines.append(f"{indent}}} // namespace {ns_name}")

            elif child.type == "function_definition":
                if _has_nested_definitions(child):
                    # Macro-induced mis-parse — recurse into the body
                    body = child.child_by_field_name("body")
                    if body:
                        _process_toplevel(body, indent)
                elif not _is_member_function(child, source):
                    # Non-member function — extract signature
                    sig = _extract_function_sig(child, source, indent)
                    if sig:
                        lines.append(sig)

            elif child.type == "template_declaration":
                tmpl_text = _node_text(child, source).strip()
                has_class = any(
                    tc.type in ("class_specifier", "struct_specifier") for tc in child.children
                )
                if not has_class:
                    brace = tmpl_text.find("{")
                    if brace > 0:
                        tmpl_text = tmpl_text[:brace].rstrip() + ";"
                    lines.append(f"{indent}{tmpl_text}")

            elif child.type in ("type_definition", "alias_declaration"):
                lines.append(f"{indent}{_node_text(child, source).strip()}")

            elif child.type == "declaration":
                text = _node_text(child, source).strip()
                if text.startswith("template") or text.startswith("extern"):
                    lines.append(f"{indent}{text}")

    _process_toplevel(root)

    # Put includes at the top so LLM can see dependency info
    return "\n".join(includes + lines)


# ---------------------------------------------------------------------------
# File classification helpers
# ---------------------------------------------------------------------------

_CPP_HEADER_EXTS = {".h", ".hpp", ".hxx"}
_CPP_IMPL_EXTS = {".cpp", ".cc", ".cxx", ".c"}


def _is_header(path: Path) -> bool:
    return path.suffix.lower() in _CPP_HEADER_EXTS


def _is_impl(path: Path) -> bool:
    return path.suffix.lower() in _CPP_IMPL_EXTS


# ---------------------------------------------------------------------------
# Phase 2 entry point
# ---------------------------------------------------------------------------


def phase2_extract_ast(
    component: Component,
    config: Config,
    include_exts: set[str],
) -> dict[str, ASTEntry]:
    """Extract AST skeletons for all code files in a component.

    Returns a dict keyed by relative path.
    """
    from .scanner import _matches_exclude, is_code_file

    ast_map: dict[str, ASTEntry] = {}
    comp_path = component.path

    # Collect all code files (skip oversized data files)
    all_files: list[Path] = []
    for f in comp_path.rglob("*"):
        if not f.is_file():
            continue
        if f.stat().st_size > MAX_FILE_SIZE:
            continue
        rel = str(f.relative_to(comp_path)).replace("\\", "/")
        if _matches_exclude(rel, config.exclude_patterns):
            continue
        if is_code_file(f, include_exts):
            all_files.append(f)

    # Detect if we have C/C++ files
    has_cpp = any(f.suffix.lower() in (_CPP_HEADER_EXTS | _CPP_IMPL_EXTS) for f in all_files)

    if has_cpp:
        headers = [f for f in all_files if _is_header(f)]
        impls = [f for f in all_files if _is_impl(f)]

        header_stems = {f.stem.lower() for f in headers}

        # Extract all headers
        for f in headers:
            try:
                source = f.read_bytes()
            except (OSError, PermissionError):
                continue
            skeleton = extract_cpp_file(f, source)
            if not skeleton.strip():
                continue
            rel = str(f.relative_to(comp_path)).replace("\\", "/")
            dir_part = str(f.parent.relative_to(comp_path)).replace("\\", "/")
            if dir_part == ".":
                dir_part = ""
            ast_map[rel] = ASTEntry(
                rel_path=rel,
                dir=dir_part,
                content=skeleton,
                tokens=estimate_tokens(skeleton),
                language="cpp",
            )

        # Orphan .cpp (no matching .h) — full extraction
        for f in impls:
            if f.stem.lower() not in header_stems:
                try:
                    source = f.read_bytes()
                except (OSError, PermissionError):
                    continue
                skeleton = extract_cpp_file(f, source)
                if not skeleton.strip():
                    continue
                rel = str(f.relative_to(comp_path)).replace("\\", "/")
                dir_part = str(f.parent.relative_to(comp_path)).replace("\\", "/")
                if dir_part == ".":
                    dir_part = ""
                ast_map[rel] = ASTEntry(
                    rel_path=rel,
                    dir=dir_part,
                    content=skeleton,
                    tokens=estimate_tokens(skeleton),
                    language="cpp",
                )

        # .cpp with matching .h — supplementary only
        for f in impls:
            if f.stem.lower() in header_stems:
                try:
                    source = f.read_bytes()
                except (OSError, PermissionError):
                    continue
                extra = extract_cpp_supplementary(f, source)
                if extra.strip() and estimate_tokens(extra) > SUPPLEMENTARY_MIN_TOKENS:
                    rel = str(f.relative_to(comp_path)).replace("\\", "/")
                    dir_part = str(f.parent.relative_to(comp_path)).replace("\\", "/")
                    if dir_part == ".":
                        dir_part = ""
                    ast_map[f"{rel} (extra)"] = ASTEntry(
                        rel_path=rel,
                        dir=dir_part,
                        content=extra,
                        tokens=estimate_tokens(extra),
                        language="cpp",
                        is_supplementary=True,
                    )

    # TODO: other languages (Python, Java, etc.) — add extractors here

    return ast_map
