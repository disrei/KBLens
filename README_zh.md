```
 ╦╔═ ╔╗  ╦  ╔═╗ ╔╗╔ ╔═╗
 ╠╩╗ ╠╩╗ ║  ║╣  ║║║ ╚═╗
 ╩ ╩ ╚═╝ ╩═╝╚═╝ ╝╚╝ ╚═╝
```

[English](README.md) | 中文

面向大型 C++ 代码库的渐进式代码知识库生成器。KBLens 使用 tree-sitter 提取 AST 骨架，将其打包为 LLM 友好的批次，生成分层 Markdown 摘要——让 AI 编程助手在不阅读每个文件的情况下获取代码库的结构化上下文。

## 为什么需要 KBLens

在进行 **vibe coding**（通过 Cursor、Copilot、OpenCode 等 AI 助手用自然语言编写和重构代码）时，AI 需要理解代码库的架构。但大型代码库（10 万+ 文件）太大，LLM 无法直接消费。没有结构化上下文时，AI 助手要么产生幻觉，要么在被问到内部系统时说"我不知道"。

KBLens 通过从实际源代码生成**三层知识库**来解决这个问题：

```
L0  INDEX.md            项目概览 + 包目录
L1  packages/engine.md  每个包的组件列表和架构
L2  packages/engine/    每个组件：职责、关键类型、公共 API、依赖
```

这为 AI 助手提供了可靠、可搜索的参考——就像一份从实际代码生成的、始终保持最新的架构文档。将知识库指向你的 AI 工具，它就能回答"物理系统是怎么工作的？"或"SmartDrive 的公共 API 是什么？"这类问题，而无需阅读每个源文件。

## 核心特性

- **基于 AST 的提取** — 使用 tree-sitter 从 C++ 头文件和源文件中提取 class/struct/enum/function 签名。无猜测、无幻觉。
- **分层摘要** — 三个层次的详细信息（项目 → 包 → 组件），按需逐层展开。问一个包，得到概览；问一个类，得到细节。
- **增量更新** — 仅重新生成源文件发生变化的组件。通过文件哈希追踪变更。200+ 组件的完整运行约 5 分钟；增量运行仅需数秒。
- **变更检测** — 五态分类（未变/已变/新增/已删/失败），自动清理孤立文件并级联更新受影响的包。
- **多源项目** — 一个配置文件可定义多个源目录。每个源拥有独立的知识库、元数据和变更追踪。
- **并发生成** — 8 个组件并行处理，8 个 LLM 调用并发。包含指数退避重试（3 次尝试）。
- **断点续传** — 每个组件完成后立即持久化进度。Ctrl+C 后重新运行即可从中断处继续。
- **实时仪表盘** — Rich 终端 UI，实时显示进度、活跃组件、token 用量和错误计数。
- **反幻觉提示** — LLM 提示词明确禁止推测性语言和虚构内容。仅在 AST 中可见 `#include` 时才列出依赖。

## 前置要求

- **Python 3.11+**
- **C 编译器** — tree-sitter 编译语法时需要（GCC、Clang 或 MSVC）
  - Ubuntu/Debian: `sudo apt install build-essential`
  - macOS: Xcode 命令行工具 (`xcode-select --install`)
  - Windows: Visual Studio Build Tools 或 MinGW

## 安装

```bash
# 从 PyPI 安装
pip install kblens

# 或直接从 GitHub 安装
pip install git+https://github.com/disrei/KBLens.git

# 或克隆后以开发模式安装
git clone https://github.com/disrei/KBLens.git
cd kblens
pip install -e .

# 验证
kblens version
```

## 快速开始

### 1. 创建配置

```bash
kblens init
```

交互式引导你创建 `~/.config/kblens/config.yaml`，包含源码路径和 LLM 设置。

也可以手动创建：

```yaml
# ~/.config/kblens/config.yaml
version: 1
project: "my_engine"

output_dir: "~/kblens_kb/my_engine"

sources:
  - path: "/absolute/path/to/packages"    # 绝对路径
    name: "core"                          # 短名称，用作子目录

llm:
  model: "gpt-4o-mini"
  # api_key: "your-api-key"     # 见下方「API Key 安全」
  temperature: 0.2
  max_concurrent: 8
  max_concurrent_components: 8

summary_language: "en"          # 摘要语言（en/zh/...）
```

### 2. 预览

```bash
kblens generate --dry-run
```

扫描源码、提取 AST、报告统计数据，不调用 LLM。

### 3. 生成

```bash
kblens generate
```

200 个组件约需 5 分钟，消耗约 40 万输入 token。

### 4. 使用

生成的知识库是一个 Markdown 文件目录，你可以：

- **直接浏览** — 打开 `INDEX.md`，沿层级导航
- **用 grep 搜索** — 在所有摘要中查找任何类、函数或概念
- **集成 AI 工具** — 将知识库目录指向你的编程助手（见下方 [AI 助手集成](#ai-助手集成)）

## API Key 安全

**永远不要将 API Key 提交到版本控制。** 使用以下方式之一：

1. **环境变量**（推荐）：
   ```bash
   export KBLENS_LLM_KEY=sk-your-key-here
   ```

2. **本地配置覆盖** — 在配置文件旁创建 `.local.yaml`：
   ```yaml
   # ~/.config/kblens/config.local.yaml
   llm:
     api_key: "sk-your-key-here"
   ```

3. **配置中引用环境变量**：
   ```yaml
   llm:
     api_key_env: "MY_OPENAI_KEY"
   ```

## 配置系统

KBLens 使用两层配置系统：

| 层级 | 位置 | 用途 |
|------|------|------|
| 全局 | `~/.config/kblens/config.yaml` | 共享 LLM 设置、打包参数 |
| 项目 | `./kblens.yaml`（项目根目录） | 项目特定的源码和输出路径 |

项目配置覆盖全局配置。每层都可以有 `.local.yaml` 文件用于存放敏感值（API Key）。

### 配置参考

```yaml
version: 1
project: "my_project"                # 项目名（CLI 中显示）

output_dir: "~/kblens_kb/my_project"  # 知识库输出根目录

sources:                              # 要扫描的源目录
  - path: "/absolute/path/to/src"     # 绝对路径
    name: "core"                      # 短名称（用作子目录）

include_extensions: "auto"            # "auto" 或显式列表: [".h", ".cpp"]

exclude_patterns:                     # 排除的 glob 模式
  - "*/test/*"
  - "*_test.*"

llm:
  model: "gpt-4o-mini"               # 任何 litellm 兼容的模型
  api_base: "https://api.openai.com/v1"
  api_key: "sk-..."                   # 或用 api_key_env / KBLENS_LLM_KEY
  temperature: 0.2
  max_concurrent: 8                   # LLM 并发调用数
  max_concurrent_components: 8        # 组件并行处理数

packing:
  token_budget: 8000                  # 每批目标 token 数
  token_min: 1000                     # 最小批次大小
  token_max: 24000                    # 最大批次大小
  component_split_threshold: 200      # 文件数阈值，超过则拆分

summary_language: "en"                # 生成摘要的语言
```

### 环境变量

| 变量 | 用途 |
|------|------|
| `KBLENS_LLM_KEY` | LLM API Key（覆盖配置文件） |

## CLI 参考

```
kblens generate                    # 生成所有源
kblens generate --source core      # 仅生成 "core" 源
kblens generate --dry-run          # 预览，不调用 LLM
kblens generate --config ./my.yaml # 使用指定配置文件
kblens status                      # 显示知识库状态
kblens monitor                     # 实时监控正在运行的生成进程
kblens init                        # 交互式配置向导
kblens version                     # 显示版本号
```

## 输出结构

以包含两个源的项目为例：

```
~/kblens_kb/my_project/
├── core/                           # 源: core
│   ├── INDEX.md                    # L0: 包目录与链接
│   ├── _meta.json                  # 组件状态、哈希、token 统计
│   ├── _progress.jsonl             # 生成事件日志
│   └── core/                       # packages（与源同名）
│       ├── engine.md               # L1: engine 包概览
│       ├── engine/
│       │   ├── SoundSystem.md      # L2: 组件概览
│       │   ├── SoundSystem/        # 叶子批次文件（大组件）
│       │   │   ├── src_reverb.md
│       │   │   └── src_voice.md
│       │   └── Physics.md
│       ├── gameplay.md
│       └── gameplay/
│           └── ...
└── tools/                          # 源: tools
    ├── INDEX.md
    └── tools/
        └── ...
```

### Markdown 格式

每个 L2 组件文件遵循统一结构：

```markdown
# ComponentName

## Responsibility
一到两句话描述该组件的职责。

## Key Types and Relationships
类、结构体、枚举及其关系。

## Main Public Interfaces
关键方法及其签名。

## Dependencies
显式 #include 路径，或 "No explicit dependencies visible in AST excerpt."
```

## 工作原理

KBLens 对每个源执行六阶段管线：

1. **扫描** — 遍历目录树，发现组件（包/子目录对），统计文件数和行数
2. **AST 提取** — 用 tree-sitter 解析 C++ 文件，提取 class/struct/enum/function 骨架和 `#include` 指令
3. **打包** — 将 AST 条目按 token 预算分组，为大组件创建聚合组
4. **叶子摘要** — 将每个批次发送给 LLM 生成聚焦摘要（Phase 4）
5. **聚合** — 向上合并摘要：片段 → 组件概览 → 包概览 → INDEX（Phase 5a-5d）
6. **写入** — 持久化 Markdown 文件，增量更新 `_meta.json`

### 增量行为

KBLens 设计为日常开发中反复使用。代码变更后只需重新运行 `kblens generate`，它会自动判断哪些需要更新。

后续运行时：

- **未变组件** 完全跳过（基于文件路径 + 修改时间 + 大小的哈希匹配）
- **已变组件** 重新生成，并更新其所在包的 L1 概览
- **新组件** 生成并添加到包概览
- **已删组件** 清理其 `.md` 文件和元数据
- **失败组件** （上次超时/出错）自动重试
- **跳过的组件**（AST token < 100）记录在元数据中，避免重复扫描
- **L0 INDEX** 仅在有包发生变化时才重新生成

#### 典型工作流

```bash
# 首次运行：完整生成（200 组件约 5 分钟）
kblens generate

# ... 修改代码 ...

# 后续运行：仅重新生成变更的组件（约数秒）
kblens generate

# 查看知识库状态
kblens status
```

#### 变更检测原理

每个组件的标识是其所有代码文件的 `(相对路径, 修改时间, 文件大小)` 的哈希。重新运行 `kblens generate` 时：

1. **扫描** 发现当前所有组件
2. **比较** 每个组件的哈希与 `_meta.json` 中的记录
3. 哈希匹配 → 跳过。不匹配或缺失 → 重新生成
4. `_meta.json` 中有但磁盘上不存在的组件 → 删除其 `.md` 文件
5. 仅包含脏组件的包会重新生成 L1 概览
6. 仅在有 L1 变化时才重新生成 L0 INDEX

## 语言支持

目前支持 **C++**（`.h`、`.hpp`、`.cpp`、`.cc`、`.cxx`）。AST 提取、打包和摘要管线本身与语言无关——仅 tree-sitter 解析器和提取逻辑是语言特定的。

其他文件类型在扫描时会被检测到，但产生 0 个 AST token 并被跳过。AST token 少于 100 的组件不会进行 LLM 摘要。

### 路线图

计划支持更多语言。架构层面已经就绪——添加新语言只需：

1. tree-sitter 语法包（如 `tree-sitter-python`）
2. 语言特定的提取函数
3. 在 `phase2_extract_ast()` 中添加扩展名映射

计划支持的语言（按优先级排序）：

- [ ] Python
- [ ] TypeScript / JavaScript
- [ ] C#
- [ ] Java / Kotlin
- [ ] Rust
- [ ] Go

## AI 助手集成

KBLens 生成的知识库可供 AI 编程助手查询。项目中包含一个 [OpenCode](https://opencode.ai) skill 模板：`skills/kblens-kb/SKILL.md`。

### OpenCode 配置

1. 将 skill 复制到 OpenCode 配置目录：

   ```bash
   # Linux / macOS
   mkdir -p ~/.config/opencode/skills/kblens-kb
   cp skills/kblens-kb/SKILL.md ~/.config/opencode/skills/kblens-kb/

   # Windows
   mkdir "%USERPROFILE%\.config\opencode\skills\kblens-kb"
   copy skills\kblens-kb\SKILL.md "%USERPROFILE%\.config\opencode\skills\kblens-kb\"
   ```

2. Skill 会自动读取 `~/.config/kblens/config.yaml` 来定位知识库位置。

3. 向 AI 助手提问关于代码库的问题——它会搜索知识库来回答。

### 其他 AI 工具

知识库是纯 Markdown 文件，可以集成到任何支持文件上下文的 AI 工具：

- 将知识库目录添加为引用路径
- 用 grep/search 查找相关 `.md` 文件
- 三层层级（INDEX → 包 → 组件）提供自然的渐进式信息展开

## 注意事项

- 知识库在 `_meta.json` 中使用**绝对路径**进行变更追踪。如果移动了源码目录，请用 `kblens generate` 重新生成知识库。
- LLM 模型兼容性：KBLens 底层使用 [litellm](https://github.com/BerriAI/litellm)，因此 litellm 支持的所有模型都可使用（OpenAI、Anthropic、本地 Ollama 等）。

## 许可证

MIT — 见 [LICENSE](LICENSE)。
