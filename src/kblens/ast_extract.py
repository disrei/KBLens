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
import tree_sitter_javascript
import tree_sitter_python
import tree_sitter_typescript

from .models import ASTEntry, Component, Config

# ---------------------------------------------------------------------------
# Token estimation
# ---------------------------------------------------------------------------


# Token estimation
CHARS_PER_TOKEN = 4
MAX_ENUM_CHARS = 4000
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
# Python extractor
# ---------------------------------------------------------------------------

_py_language = tree_sitter.Language(tree_sitter_python.language())
_py_parser = tree_sitter.Parser(_py_language)

_PY_EXTS = {".py", ".pyi"}

# ---------------------------------------------------------------------------
# TypeScript / JavaScript extractor
# ---------------------------------------------------------------------------

_ts_language = tree_sitter.Language(tree_sitter_typescript.language_typescript())
_tsx_language = tree_sitter.Language(tree_sitter_typescript.language_tsx())
_js_language = tree_sitter.Language(tree_sitter_javascript.language())

_ts_parser = tree_sitter.Parser(_ts_language)
_tsx_parser = tree_sitter.Parser(_tsx_language)
_js_parser = tree_sitter.Parser(_js_language)

_TS_EXTS = {".ts", ".tsx"}
_JS_EXTS = {".js", ".jsx", ".mjs", ".cjs"}


def _py_node_text(node: tree_sitter.Node, source: bytes) -> str:
    return source[node.start_byte : node.end_byte].decode("utf-8", errors="replace")


def _py_is_private(name: str) -> bool:
    """Check if a Python name is private (starts with _ but not __dunder__)."""
    if name.startswith("__") and name.endswith("__"):
        return False  # dunder is public
    return name.startswith("_")


def _py_get_docstring(body_node: tree_sitter.Node, source: bytes) -> str | None:
    """Extract the first-line docstring from a class/function body."""
    for child in body_node.children:
        if child.type == "expression_statement":
            for sub in child.children:
                if sub.type == "string":
                    raw = _py_node_text(sub, source).strip()
                    # Strip triple quotes
                    for q in ('"""', "'''"):
                        if raw.startswith(q) and raw.endswith(q):
                            raw = raw[3:-3].strip()
                            break
                    # Take first line only
                    first_line = raw.split("\n")[0].strip()
                    if first_line:
                        return first_line
            break  # Only check the very first statement
        elif child.type in ("comment",):
            continue  # Skip leading comments
        else:
            break
    return None


def _py_extract_func_sig(
    node: tree_sitter.Node,
    source: bytes,
    indent: str = "",
    decorators: list[str] | None = None,
) -> list[str]:
    """Extract a function signature with type hints and optional decorators."""
    lines: list[str] = []
    if decorators:
        for d in decorators:
            lines.append(f"{indent}{d}")

    name = ""
    params = ""
    return_type = ""
    for child in node.children:
        if child.type == "identifier":
            name = _py_node_text(child, source)
        elif child.type == "parameters":
            params = _py_node_text(child, source)
        elif child.type == "type":
            return_type = _py_node_text(child, source)

    sig = f"def {name}{params}"
    if return_type:
        sig += f" -> {return_type}"
    sig += ": ..."

    lines.append(f"{indent}{sig}")

    # Extract docstring
    body = node.child_by_field_name("body")
    if body:
        doc = _py_get_docstring(body, source)
        if doc:
            lines.append(f'{indent}    """{doc}"""')

    return lines


def _py_extract_class(node: tree_sitter.Node, source: bytes) -> list[str]:
    """Extract a class definition: name, bases, docstring, public method signatures."""
    lines: list[str] = []

    name = ""
    bases = ""
    for child in node.children:
        if child.type == "identifier":
            name = _py_node_text(child, source)
        elif child.type == "argument_list":
            bases = _py_node_text(child, source)

    header = f"class {name}"
    if bases:
        header += bases
    header += ":"
    lines.append(header)

    body = node.child_by_field_name("body")
    if not body:
        return lines

    # Docstring
    doc = _py_get_docstring(body, source)
    if doc:
        lines.append(f'    """{doc}"""')

    for child in body.children:
        if child.type == "function_definition":
            fname = ""
            for sub in child.children:
                if sub.type == "identifier":
                    fname = _py_node_text(sub, source)
                    break
            if _py_is_private(fname):
                continue
            lines.extend(_py_extract_func_sig(child, source, indent="    "))

        elif child.type == "decorated_definition":
            # Extract decorators, then the inner function
            decorators: list[str] = []
            inner_func = None
            for sub in child.children:
                if sub.type == "decorator":
                    decorators.append(_py_node_text(sub, source).strip())
                elif sub.type == "function_definition":
                    inner_func = sub
            if inner_func:
                fname = ""
                for sub in inner_func.children:
                    if sub.type == "identifier":
                        fname = _py_node_text(sub, source)
                        break
                if _py_is_private(fname):
                    continue
                lines.extend(
                    _py_extract_func_sig(inner_func, source, indent="    ", decorators=decorators)
                )

        elif child.type == "expression_statement":
            # Class-level attribute (skip if private)
            text = _py_node_text(child, source).strip()
            if text.startswith('"""') or text.startswith("'''"):
                continue  # Already handled as docstring
            # Check if it's a typed assignment like `name: str = "default"`
            if ":" in text:
                attr_name = text.split(":")[0].strip()
                if not _py_is_private(attr_name):
                    lines.append(f"    {text}")

    return lines


def extract_python_file(file_path: Path, source: bytes) -> str:
    """Extract AST skeleton from a Python file.

    Extracts: imports, __all__, module-level constants (typed), classes
    (with public methods), and module-level functions. Private names
    (prefixed with _) are skipped.
    """
    tree = _py_parser.parse(source)
    root = tree.root_node
    lines: list[str] = []

    for child in root.children:
        if child.type in ("import_statement", "import_from_statement"):
            lines.append(_py_node_text(child, source).strip())

        elif child.type == "expression_statement":
            text = _py_node_text(child, source).strip()
            # __all__ definition
            if text.startswith("__all__"):
                lines.append(text)
            # Typed module-level constant: NAME: type = value
            elif ":" in text and "=" in text:
                var_name = text.split(":")[0].strip()
                if not _py_is_private(var_name):
                    lines.append(text)

        elif child.type == "class_definition":
            cname = ""
            for sub in child.children:
                if sub.type == "identifier":
                    cname = _py_node_text(sub, source)
                    break
            if _py_is_private(cname):
                continue
            lines.extend(_py_extract_class(child, source))

        elif child.type == "function_definition":
            fname = ""
            for sub in child.children:
                if sub.type == "identifier":
                    fname = _py_node_text(sub, source)
                    break
            if _py_is_private(fname):
                continue
            lines.extend(_py_extract_func_sig(child, source))

        elif child.type == "decorated_definition":
            decorators: list[str] = []
            inner = None
            inner_type = None
            for sub in child.children:
                if sub.type == "decorator":
                    decorators.append(_py_node_text(sub, source).strip())
                elif sub.type == "function_definition":
                    inner = sub
                    inner_type = "function"
                elif sub.type == "class_definition":
                    inner = sub
                    inner_type = "class"

            if inner_type == "function":
                fname = ""
                for sub in inner.children:
                    if sub.type == "identifier":
                        fname = _py_node_text(sub, source)
                        break
                if _py_is_private(fname):
                    continue
                lines.extend(_py_extract_func_sig(inner, source, decorators=decorators))

            elif inner_type == "class":
                cname = ""
                for sub in inner.children:
                    if sub.type == "identifier":
                        cname = _py_node_text(sub, source)
                        break
                if _py_is_private(cname):
                    continue
                for d in decorators:
                    lines.append(d)
                lines.extend(_py_extract_class(inner, source))

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# TypeScript / JavaScript extraction helpers
# ---------------------------------------------------------------------------


def _ts_node_text(node: tree_sitter.Node, source: bytes) -> str:
    return source[node.start_byte : node.end_byte].decode("utf-8", errors="replace")


def _ts_is_private(name: str) -> bool:
    """Check if a TS/JS name is private (_ prefix, but not constructor)."""
    if name in ("constructor",):
        return False
    return name.startswith("_")


def _ts_extract_method_sig(node: tree_sitter.Node, source: bytes, indent: str = "  ") -> str | None:
    """Extract method signature from a method_definition node."""
    parts: list[str] = []
    name = ""
    for child in node.children:
        if child.type in ("async", "static", "readonly", "override", "abstract"):
            parts.append(child.type)
        elif child.type in ("accessibility_modifier",):
            # public, private, protected
            text = _ts_node_text(child, source)
            if text == "private":
                return None  # skip private methods
            parts.append(text)
        elif child.type == "property_identifier":
            name = _ts_node_text(child, source)
        elif child.type == "formal_parameters":
            parts.append(f"{name}{_ts_node_text(child, source)}")
        elif child.type == "type_annotation":
            parts.append(_ts_node_text(child, source))
    if not name:
        return None
    if _ts_is_private(name):
        return None
    return f"{indent}{' '.join(parts)};"


def _ts_extract_class(node: tree_sitter.Node, source: bytes) -> list[str]:
    """Extract class declaration: name, extends, implements, public members."""
    lines: list[str] = []

    # Build class header
    name = ""
    heritage = ""
    for child in node.children:
        if child.type == "type_identifier":
            name = _ts_node_text(child, source)
        elif child.type == "class_heritage":
            heritage = " " + _ts_node_text(child, source)

    lines.append(f"class {name}{heritage} {{")

    body = node.child_by_field_name("body")
    if not body:
        lines.append("}")
        return lines

    for child in body.children:
        if child.type in ("{", "}"):
            continue

        if child.type == "method_definition":
            sig = _ts_extract_method_sig(child, source)
            if sig:
                lines.append(sig)

        elif child.type == "public_field_definition":
            text = _ts_node_text(child, source).strip()
            # Check for private keyword or _ prefix
            is_private = False
            field_name = ""
            for sub in child.children:
                if sub.type == "accessibility_modifier" and _ts_node_text(sub, source) == "private":
                    is_private = True
                    break
                if sub.type == "property_identifier":
                    field_name = _ts_node_text(sub, source)
            if is_private or _ts_is_private(field_name):
                continue
            lines.append(f"  {text};")

    lines.append("}")
    return lines


def _ts_extract_interface(node: tree_sitter.Node, source: bytes) -> list[str]:
    """Extract interface declaration with all members."""
    lines: list[str] = []
    name = ""
    extends = ""
    for child in node.children:
        if child.type == "type_identifier":
            name = _ts_node_text(child, source)
        elif child.type == "extends_type_clause":
            extends = " " + _ts_node_text(child, source)

    lines.append(f"interface {name}{extends} {{")

    # interface_body contains property_signature, method_signature, etc.
    for child in node.children:
        if child.type in ("interface_body", "object_type"):
            for member in child.children:
                if member.type in ("{", "}", ";", ","):
                    continue
                text = _ts_node_text(member, source).strip()
                if text:
                    lines.append(f"  {text};")
            break

    lines.append("}")
    return lines


def _ts_extract_function_sig(node: tree_sitter.Node, source: bytes) -> str | None:
    """Extract function signature from function_declaration."""
    name = ""
    params = ""
    ret = ""
    is_async = False
    for child in node.children:
        if child.type == "async":
            is_async = True
        elif child.type == "identifier":
            name = _ts_node_text(child, source)
        elif child.type == "formal_parameters":
            params = _ts_node_text(child, source)
        elif child.type == "type_annotation":
            ret = _ts_node_text(child, source)
        elif child.type == "type_parameters":
            name += _ts_node_text(child, source)
    if not name:
        return None
    prefix = "async " if is_async else ""
    sig = f"{prefix}function {name}{params}"
    if ret:
        sig += f"{ret}"
    return f"{sig};"


def _ts_unwrap_export(
    node: tree_sitter.Node, source: bytes
) -> tuple[bool, bool, tree_sitter.Node | None]:
    """Unwrap export_statement, return (is_export, is_default, inner_node)."""
    if node.type != "export_statement":
        return False, False, node
    is_default = False
    inner = None
    for child in node.children:
        if child.type == "export":
            continue
        if child.type == "default":
            is_default = True
            continue
        if child.type in (
            "class_declaration",
            "abstract_class_declaration",
            "function_declaration",
            "interface_declaration",
            "type_alias_declaration",
            "enum_declaration",
            "lexical_declaration",
        ):
            inner = child
            break
    return True, is_default, inner


def extract_ts_js_file(file_path: Path, source: bytes) -> str:
    """Extract AST skeleton from a TypeScript or JavaScript file.

    Extracts: imports, exports, interfaces, type aliases, enums, classes
    (with public methods/fields), and module-level functions. Private
    names (_prefixed) and `private` access modifier members are skipped.
    """
    ext = file_path.suffix.lower()
    if ext == ".tsx":
        parser = _tsx_parser
    elif ext in _TS_EXTS:
        parser = _ts_parser
    else:
        parser = _js_parser

    tree = parser.parse(source)
    root = tree.root_node
    lines: list[str] = []

    def _process_node(node: tree_sitter.Node, export_prefix: str = "") -> None:
        ntype = node.type

        if ntype == "import_statement":
            lines.append(_ts_node_text(node, source).rstrip(";") + ";")

        elif ntype == "export_statement":
            is_export, is_default, inner = _ts_unwrap_export(node, source)
            prefix = "export default " if is_default else "export "
            if inner:
                _process_node(inner, prefix)
            else:
                # re-export: export { ... } from '...'
                text = _ts_node_text(node, source).strip()
                if "from" in text:
                    lines.append(text)

        elif ntype in ("class_declaration", "abstract_class_declaration"):
            # Get class name
            cname = ""
            for child in node.children:
                if child.type == "type_identifier":
                    cname = _ts_node_text(child, source)
                    break
            if _ts_is_private(cname):
                return
            cls_lines = _ts_extract_class(node, source)
            if export_prefix and cls_lines:
                cls_lines[0] = export_prefix + cls_lines[0]
            lines.extend(cls_lines)

        elif ntype == "interface_declaration":
            iname = ""
            for child in node.children:
                if child.type == "type_identifier":
                    iname = _ts_node_text(child, source)
                    break
            if _ts_is_private(iname):
                return
            iface_lines = _ts_extract_interface(node, source)
            if export_prefix and iface_lines:
                iface_lines[0] = export_prefix + iface_lines[0]
            lines.extend(iface_lines)

        elif ntype == "type_alias_declaration":
            text = _ts_node_text(node, source).strip().rstrip(";") + ";"
            tname = ""
            for child in node.children:
                if child.type == "type_identifier":
                    tname = _ts_node_text(child, source)
                    break
            if _ts_is_private(tname):
                return
            lines.append(f"{export_prefix}{text}")

        elif ntype == "enum_declaration":
            text = _ts_node_text(node, source).strip()
            ename = ""
            for child in node.children:
                if child.type == "identifier":
                    ename = _ts_node_text(child, source)
                    break
            if _ts_is_private(ename):
                return
            lines.append(f"{export_prefix}{text}")

        elif ntype == "function_declaration":
            sig = _ts_extract_function_sig(node, source)
            if sig:
                fname = ""
                for child in node.children:
                    if child.type == "identifier":
                        fname = _ts_node_text(child, source)
                        break
                if _ts_is_private(fname):
                    return
                lines.append(f"{export_prefix}{sig}")

        elif ntype == "lexical_declaration":
            # const/let declarations — extract typed constants
            text = _ts_node_text(node, source).strip().rstrip(";") + ";"
            # Check for arrow functions or typed constants
            for child in node.children:
                if child.type == "variable_declarator":
                    vname = ""
                    for sub in child.children:
                        if sub.type == "identifier":
                            vname = _ts_node_text(sub, source)
                            break
                    if _ts_is_private(vname):
                        return
                    # Only include if it has a type annotation or is an arrow function
                    has_type = any(s.type == "type_annotation" for s in child.children)
                    has_arrow = any(s.type == "arrow_function" for s in child.children)
                    if has_type or has_arrow:
                        if has_arrow:
                            # Extract arrow function signature only
                            for sub in child.children:
                                if sub.type == "arrow_function":
                                    params = ""
                                    ret = ""
                                    is_async = False
                                    for asub in sub.children:
                                        if asub.type == "formal_parameters":
                                            params = _ts_node_text(asub, source)
                                        elif asub.type == "type_annotation":
                                            ret = _ts_node_text(asub, source)
                                        elif asub.type == "async":
                                            is_async = True
                                    aprefix = "async " if is_async else ""
                                    type_ann = ""
                                    for sub2 in child.children:
                                        if sub2.type == "type_annotation":
                                            type_ann = _ts_node_text(sub2, source)
                                    kw = (
                                        "const"
                                        if "const" in _ts_node_text(node, source)[:6]
                                        else "let"
                                    )
                                    sig = (
                                        f"{export_prefix}{kw} {vname}{type_ann} = {aprefix}{params}"
                                    )
                                    if ret:
                                        sig += f"{ret}"
                                    sig += " => ...;"
                                    lines.append(sig)
                                    return
                        lines.append(f"{export_prefix}{text}")
                    return

    for child in root.children:
        _process_node(child)

    return "\n".join(lines)


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

            elif child.type in ("class_specifier", "struct_specifier"):
                # Top-level struct/class in .cpp (e.g. event data structs)
                lines.extend(_extract_class_or_struct(child, source))

            elif child.type == "enum_specifier":
                # Top-level enum in .cpp
                lines.extend(_extract_enum(child, source))

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

    # ---- Python files ----
    py_files = [f for f in all_files if f.suffix.lower() in _PY_EXTS]
    for f in py_files:
        try:
            source = f.read_bytes()
        except (OSError, PermissionError):
            continue
        skeleton = extract_python_file(f, source)
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
            language="python",
        )

    # ---- TypeScript / JavaScript files ----
    ts_js_files = [f for f in all_files if f.suffix.lower() in (_TS_EXTS | _JS_EXTS)]
    for f in ts_js_files:
        try:
            source = f.read_bytes()
        except (OSError, PermissionError):
            continue
        skeleton = extract_ts_js_file(f, source)
        if not skeleton.strip():
            continue
        rel = str(f.relative_to(comp_path)).replace("\\", "/")
        dir_part = str(f.parent.relative_to(comp_path)).replace("\\", "/")
        if dir_part == ".":
            dir_part = ""
        lang = "typescript" if f.suffix.lower() in _TS_EXTS else "javascript"
        ast_map[rel] = ASTEntry(
            rel_path=rel,
            dir=dir_part,
            content=skeleton,
            tokens=estimate_tokens(skeleton),
            language=lang,
        )

    return ast_map
