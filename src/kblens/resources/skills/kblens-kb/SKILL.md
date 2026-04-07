---
name: kblens-kb
description: Query a KBLens-generated code knowledge base to answer questions about code architecture, component responsibilities, class/struct usage, public interfaces, and cross-component dependencies. Trigger this skill whenever a user asks about how a system works, what a component does, or how modules relate - even if they don't mention "knowledge base" or "KBLens". The skill reads hierarchical Markdown summaries generated from C++ AST extraction.
---

# KBLens Code Knowledge Base Query Skill

## Goal

Let the agent answer code architecture questions by searching KBLens-generated hierarchical Markdown knowledge bases, rather than guessing or reading every source file. The knowledge base is generated from C++ AST extraction and covers component responsibilities, key types, public interfaces, and dependencies.

## Step 0: Locate the Knowledge Base

The knowledge base location is not hardcoded - read it from the KBLens config:

1. Read `~/.config/kblens/config.yaml`
2. Get the `output_dir` field (e.g. `~/kblens_kb/my_project`)
3. List subdirectories under `output_dir` - each subdirectory is a **source** (e.g. `core/`, `engine/`, `tools/`)

Each source has this internal structure:
```
<source_name>/
|- INDEX.md              # L0: global index listing all packages with links
|- _meta.json            # Metadata (component status, token stats)
`- <source_name>/        # packages directory (same name as source)
    |- gameplay.md       # L1: package overview (component grouping, deps, navigation)
    |- gameplay/         # L2: components within the package
    |  |- SmartDrive.md           # Component overview
    |  |- SmartDrive/             # Leaf batch details (large components only)
    |  |  |- src_smartdrive.md
    |  |  `- ...
    |  `- Pawn_pawn.interface.md  # Small component (single file)
    |- physics.md
    |- physics/
    |  |- Destruction.md
    |  `- Destruction/
    `- ...
```

Three-layer hierarchy:
- **L0 (INDEX.md)**: Source-level overview + package directory (with hyperlinks)
- **L1 (<package>.md)**: Component grouping within package, cross-component deps, navigation guide
- **L2 (<package>/<component>.md)**: Component overview - Purpose, Architecture, Key API names, Dependencies. For large components, a subdirectory contains leaf files with full API signatures; for small components, signatures are inline in this file.

## Leaf File Structure

Leaf-level `.md` files (in component subdirectories like `SmartDrive/src_smartdrive.md`) have two sections separated by `---`:

1. **Summary section** (above `---`): LLM-generated overview — Responsibility, Key Types, Source Files, Dependencies. Compact and high-level.
2. **`## Complete API Signatures`** (below `---`): Raw AST-extracted C++ signatures — complete function/method declarations, class/struct definitions, enum values. This is the authoritative source for exact signatures.

**Reading strategy to minimize context usage**:
- For architecture questions: read only the summary section (above `---`)
- For specific function signatures: grep the file, then use Read with offset/limit around the match line
- Do NOT read the entire leaf file into context if you only need the summary — the AST section can be very large

## Step 1: Determine Search Type

Based on the user's question, choose a search path:

| Question pattern | Search type | Example |
|-----------------|-------------|---------|
| Contains specific class/struct/function name | **Exact search** | "What is AlertStateComponent?" |
| Asks about a system/module's architecture | **Semantic search** | "How does the perception system work?" |
| Asks about a package or project overview | **Browse search** | "What's in the gameplay package?" |
| Asks about cross-component relationships | **Relationship search** | "How do sound and AI perception integrate?" |

## Step 2a: Exact Search (class/function name)

When the user gives a specific identifier:

1. **Grep search** across the knowledge base root (`output_dir`), covering all sources:
   ```
   Grep "<class_name>" in <output_dir>/ (include *.md)
   ```

2. **Rank results** by relevance:
   - **Highest**: `.interface.md` files - typically where the class/component is **defined** (public API, data structures)
   - **High**: `.core.md` files - typically **implementation** logic
   - **Medium**: Same-named component overview `.md` (e.g. `AI.md`) - provides inter-component context
   - **Lowest**: Other files referencing the class - just consumers, not the definition

3. **Read the most relevant 1-2 files** to answer the question.

4. **Fallback when not found**: If Grep finds nothing in the knowledge base, the symbol may not have been extracted. Then:
   - Try searching a **related component name** (e.g., searching `UpdateAnimal` fails -> try `Animal` -> find `Animal.Core.md`)
   - Get **source file paths** from `_meta.json` (`path` field records absolute path for each component)
   - Use those paths to search actual source code

## Step 2b: Semantic Search (system/architecture)

When the user asks how a system works:

1. **Read INDEX.md** to identify relevant packages and sources
2. **Read L1 package .md** to locate specific components, get grouping and dependency info
3. **Read L2 component .md** for detailed architecture, types, API
4. For finer-grained info, check if the component has a **subdirectory** (large components only) and read leaf `.md` files

## Step 2c: Browse Search (package/project overview)

- What's in the project -> Read INDEX.md
- What's in a package -> Read the corresponding L1 .md (e.g. `gameplay.md`)
- Unsure which source -> Read each source's INDEX.md in turn

## Step 2d: Relationship Search (cross-component dependencies)

1. Read relevant package L1 .md - look for "Cross-Component Dependencies" section
2. Read relevant component L2 .md - look for "Dependencies" section
3. For cross-source relationships, search in both sources

## Filename Mapping Rules

Knowledge base `.md` filenames are generated from the component `name` field by replacing `/` with `_`:
- `Pawn/pawn.interface` -> `Pawn_pawn.interface.md`
- `AICommunity/ai.perception.core` -> `AICommunity_ai.perception.core.md`
- `SmartDrive` -> `SmartDrive.md`

## Limitations

- **Public API only**: The knowledge base is based on C++ AST extraction - it contains header file and standalone `.cpp` signatures, not function implementation bodies
- **Dependencies may be incomplete**: If a `.md` says "No explicit dependencies visible in AST excerpt.", it just means no `#include` was seen in the extracted AST, not that there are truly no dependencies
- **For implementation details**: Use the summary section to understand architecture and the `## Complete API Signatures` section for exact signatures. For actual implementation logic (function bodies), search source code directly
- **C++ only**: Other languages (C#, Python, etc.) are not covered by the knowledge base
