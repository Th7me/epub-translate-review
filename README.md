# EPUB Translate & Review

一个面向 Codex 的长篇 EPUB 翻译与回源审校 skill。默认由 **GPT-5.6-Luna（max）初译，GPT-6.1-Sol（high）逐条回源审校**，保留原书结构交付 EPUB，支持分批处理与断点续作。

默认场景是把用户提供的小说 EPUB 翻译为简体中文；用户指定其他目标语言时，应相应调整术语、语体和审校标准。模型负责理解与翻译，工作台脚本负责文本定位、回填和 EPUB 结构检查，不调用翻译 API。

## 默认 Codex 角色配方

| 角色 | 默认运行配置 | 负责内容 | 交付边界 |
| --- | --- | --- | --- |
| 主助手 | GPT-6.1-Sol；`model = gpt-6.1-sol`；reasoning effort `high` | 全书目录与资料库、人物/称呼/术语标准、样稿标准、批次协调、完整逐条回源审校、审校记录、EPUB 集成与交付 | 只有主助手实际读过并核对的范围才能标为 `mainreviewed` |
| 批次子代理 | GPT-5.6-Luna；`model = gpt-5.6-luna`；reasoning effort `max` | 直接阅读源文的分批初译，以及交付前逐条自查 | 只写获授权批次的冻结初稿，不承担主审结论，也不替代主助手的回源审校 |

这是本包的默认执行配方。用户可以显式覆盖模型、reasoning effort、批次范围或并发；如果指定模型或配置不可用，应保存进度并报告差异，停止依赖该配置的新初译，继续可完成的已有稿审校与整理，不能暗中换模型。

### 配置示例怎么用

`examples/codex/config.toml` 和 `examples/codex/agents/epub-translator.toml` 是可复制、可审阅的示例配置，不是对用户环境的自动修改。它们表达的默认设置包括：主助手使用 `gpt-6.1-sol`/`high`，默认子代理使用 `gpt-5.6-luna`/`max`，示例并发上限为 2（可按项目额度和资源调整）；自定义 agent 名为 `epub_translator`，职责是直接原文初译与自查，不主审。

将示例合并到独立翻译项目的以下位置：

```text
<translation-project>/.codex/config.toml
<translation-project>/.codex/agents/epub-translator.toml
```

合并前先查看项目已有配置，逐项加入所需设置，不覆盖已有设置，不自动改写用户配置。Codex 的默认子代理模型和 effort 可以从父助手继承；`[agents]` 中的 `default_subagent_model` 与 `default_subagent_reasoning_effort` 可设默认值。自定义 agent TOML 中的 `model` 与 `model_reasoning_effort` 会覆盖该 agent 的继承值，常用字段为 `name`、`description` 和 `developer_instructions`。启用自定义角色后，在调用中使用 `epub_translator`，并从真实运行元数据、界面或宿主记录核对实际模型与 effort；配置文件本身不是运行证据。

`skills/epub-translate-review/agents/openai.yaml` 只提供 skill 的显示信息和默认提示词，不强制模型、并发或审校结果。配置语义和技能目录以 [Codex 官方文档](https://learn.chatgpt.com/docs/agent-configuration/subagents) 与 [构建 skills 文档](https://learn.chatgpt.com/docs/build-skills) 为准。

## 流程

```mermaid
flowchart LR
    A[核对 EPUB: OPF / spine / 目录 / 正文范围] --> B[主助手建立全书资料库与翻译标准]
    B --> C[用代表性段落固定样稿与语体]
    C --> D[子代理分批直接从源文初译并自查]
    D --> E[冻结每批初稿与疑点]
    E --> F[主助手完整逐条对照原文审校]
    F --> G[更新资料库、标准与审校记录]
    G --> H[按原结构回填 EPUB]
    H --> I[ZIP / XML / 链接 / 范围检查]
    I --> J[具备目标阅读器时进行 UI 实测]
    J --> K[按证据交付 EPUB 与状态]
    G -. 后续批次 .-> D
```

1. **先核对原书和范围。** 依据 OPF、spine、目录和正文标题确定实际章节；不根据文件数或目录项数猜章节。
2. **先建标准，再扩充资料库。** 记录人物、关系、称呼变化、术语、时间线、系统字段、语体和已知疑点。未知关系保持未知，不能把后文真相写进前文视角。
3. **直接从源文分批初译。** 每个子代理只处理不重叠的授权范围，读取同一版本的标准和相邻上下文，交付完整字段、疑点和自查结果。
4. **主助手逐条回源审校。** 完整阅读本轮原文和译文，核对覆盖、主客体、说话者、否定/条件/反问、数字、时间、称呼、术语、动作方向、系统限制、内联节点和未完成台词；旧译稿也要经过同样审校。
5. **保留原版结构回填。** 沿用章节、段落、内联节点、资源路径、manifest/spine 和命名空间，避免整本 XML 重新序列化造成阅读器兼容问题。
6. **按证据交付。** 分开记录 `generated`、`selfreviewed`、`mainreviewed`、`packaged` 和 `UItested`；程序检查通过与译文语义已审是不同结论。

## 安装

需要支持 skills 与子代理的 Codex 环境，以及用于 EPUB 工作台的 Python 3.10 或以上。默认配方还需要当前账号和客户端支持对应模型及推理强度；模型与强度分别配置，`max` 和 `high` 不属于模型 ID。

将 `skills/epub-translate-review` 整个目录安装到宿主支持的技能目录。当前 Codex 文档列出的用户目录是 `~/.agents/skills`，项目目录是 `.agents/skills`；已配置其他目录的环境按实际配置放置。

在下载的仓库目录中用 PowerShell：

```powershell
$source = (Resolve-Path './skills/epub-translate-review').Path
$skillsRoot = Join-Path $HOME '.agents/skills'
$destination = Join-Path $skillsRoot 'epub-translate-review'
New-Item -ItemType Directory -Force -Path $skillsRoot | Out-Null
if (Test-Path -LiteralPath $destination) {
    throw "目标已存在：$destination；请先备份并按需合并。"
}
Copy-Item -LiteralPath $source -Destination $skillsRoot -Recurse
```

可选的 Bash 写法：

```bash
source_dir="$(pwd)/skills/epub-translate-review"
skills_root="${HOME}/.agents/skills"
destination="${skills_root}/epub-translate-review"
mkdir -p "$skills_root"
if [ -e "$destination" ]; then
  printf '目标已存在：%s；请先备份并按需合并。\n' "$destination" >&2
  exit 1
fi
cp -R "$source_dir" "$skills_root/"
```

项目级安装可把 `skillsRoot`/`skills_root` 改为当前项目的 `.agents/skills`。配置示例的合并位置是独立翻译项目的 `.codex/`，与 skill 安装目录分开。

## 首次运行

把下面的提示词复制到 Codex，并替换尖括号占位符。`<EPUB_PATH>` 可以是用户本机的绝对路径或当前项目内的相对路径；`<CHAPTER_RANGE>` 应写成实际要处理的章节或正文字段范围。

```text
使用 $epub-translate-review 处理以下 EPUB：

- 源文件：<EPUB_PATH，例如 C:\Books\book.epub>
- 本轮范围：<CHAPTER_RANGE，例如 第1–5章>
- 目标语言：简体中文
- 工作目录：<WORK_DIR，可选>

按本 skill 的默认 Codex 配方执行：主助手使用 GPT-6.1-Sol（gpt-6.1-sol / high），负责全书资料库、翻译标准、协调、完整逐条回源审校、EPUB 集成与交付；批次子代理使用 GPT-5.6-Luna（gpt-5.6-luna / max），只负责直接阅读源文的分批初译和自查，不承担主审。

先核对 OPF、spine、目录和正文范围，建立可持续更新的全书人物、关系、称呼和术语资料库，用代表性段落固定文风。然后只处理本轮范围：分批直接从源文初译，冻结初稿，主助手逐条对照原文审校，保留审校记录，按原 EPUB 结构回填并做结构检查。

如果默认模型或配置不可用，请保存进度并报告差异，不要暗中换模型；可继续完成已有稿审校与整理。交付时分别报告 generated、selfreviewed、mainreviewed、packaged、UItested 的实际范围和证据；没有目标阅读器实测时明确标记 UItested 未完成。
```

用户只要求方案或范围设计时，skill 只给方案，不启动翻译。用户明确要求样章时，只处理指定样章。

## 续作提示词

续作时提供已验证产物和状态，避免从头重译：

```text
继续使用 $epub-translate-review 处理同一本 EPUB。

- 源文件：<EPUB_PATH>
- 工作目录：<WORK_DIR>
- 已验证范围：<VERIFIED_RANGE，例如 第1–5章>
- 本轮续作范围：<NEXT_RANGE，例如 第6–10章>
- 现有资料库/翻译标准/审校记录：<PROJECT_ARTIFACTS_PATHS>

先读取并核对现有资料库、标准、初稿、审校稿、结构检查结果和未解决疑点；保留已验证范围，不覆盖原初稿，不重译已交付范围。沿用默认配方：主助手 gpt-6.1-sol / high 负责资料库、标准、完整逐条回源审校和集成交付，批次子代理 gpt-5.6-luna / max 负责直接原文初译与自查。若实际配置不可用，保存进度并报告差异，不暗换模型；可继续完成已有稿审校与整理。

完成后只把实际读过、审过、封装和实测的范围标为对应状态，并交付本轮 EPUB、审校记录、结构检查结果和待处理疑点。
```

## 范围状态与产物

状态按证据推进，含义如下：

| 状态 | 含义 | 可接受证据 |
| --- | --- | --- |
| `generated` | 指定范围已写出初稿 | 文件存在，范围和字段可核对 |
| `selfreviewed` | 初译代理已从源文逐条自查 | 自查记录或明确交付声明 |
| `mainreviewed` | 主助手完成完整源文—译文对照并记录决定 | 审校稿、定位信息和修订记录 |
| `packaged` | 审校稿已回填原结构并通过结构检查 | EPUB 构建产物和检查结果 |
| `UItested` | 在目标阅读器/界面实际打开并检查 | 实际打开记录、截图或等价证据 |

一次可追溯交付通常包含：

- 原 EPUB 的只读副本和本轮范围声明；
- 全书人物、关系、称呼、术语、时间线与疑点资料库；
- 已批准的翻译标准和样稿；
- 各批次直接源文初稿、`selfreviewed` 记录和冻结版本；
- 主助手的逐条回源审校稿、修改理由和未决疑点；
- 保留原书结构的 EPUB；
- ZIP/XML、章节范围、链接、资源和残留源文检查结果；
- 如确实进行过，目标阅读器 UI 实测证据。

## 目录结构

```text
epub-translate-review/
├── README.md
├── LICENSE
├── examples/
│   └── codex/
│       ├── config.toml
│       └── agents/
│           └── epub-translator.toml
└── skills/
    └── epub-translate-review/
        ├── SKILL.md
        ├── agents/
        │   └── openai.yaml
        ├── references/
        │   ├── translation-standard.md
        │   ├── coordinator-and-worker.md
        │   ├── codex-recipe.md
        │   ├── review-rubric.md
        │   └── epub-workbench.md
        └── scripts/
            ├── epub_workbench.py
            └── test_epub_workbench.py
```

其中：

- `SKILL.md` 是运行入口和工作边界；
- `references/` 提供翻译标准、协调与批次、Codex 角色配方、审校量规和工作台用法；
- `scripts/epub_workbench.py` 负责抽取、定位、回填和结构检查，不负责翻译；
- `examples/codex/` 只放模型与自定义 agent 的示例配置，合并前应与用户项目现有配置对照；
- `agents/openai.yaml` 只提供 skill 入口元数据和提示词。

## 验证与限制

在本包根目录运行工作台端到端测试：

```text
python -m unittest discover -s skills/epub-translate-review/scripts -p test_epub_workbench.py -v
```

如宿主提供 skill-creator 的 `quick_validate.py`，也可用它检查 `SKILL.md` 元数据格式；这项检查只覆盖技能入口格式，不替代工作台端到端验证或译文语义审校。

当前发布包的验证记录包括五项工作台工具端到端测试，以及由独立代理仅依照 skill 完成的合成 EPUB 指定章节试用。试用沿真实的“抽取 → 审校数据 → 回填 → 重开 ZIP/XML → 核对结构与文本”路径执行；指定章节改变范围、其他章节与资源保留、嵌套内联文本顺序和页内链接均已检查。

配置示例中的模型、effort 和并发值是运行约定与可调默认值，不是生产调用通过的证明；实际项目启用后应从宿主运行信息核对每次调用。

程序验证覆盖结构与回填路径；语义质量由主助手完整回源审校负责。当前没有目标阅读器 UI 实测，`packaged` 与 `UItested` 分别记录。

工作台使用 Python 标准库，要求可解析的 XML、UTF-8 XHTML 和可定位文本槽；CDATA 等特殊结构可能需要适配。默认只回填正文，书名、语言元数据、目录和 head/title 需要单独处理，详见 [工作台说明](skills/epub-translate-review/references/epub-workbench.md)。源文歧义、损坏或不完整内容进入疑点记录；阅读器兼容性在实际环境中验证。

## 分享范围

公共分享包只包含通用流程、模板、脚本、示例配置和原创合成测试。它不包含真实书籍正文、真实书名或人物资料、生成译本、项目资料库、模型会话日志、访问凭据、密钥或私有绝对路径；用户的 EPUB 和工作目录应放在本包之外。

本项目采用 [MIT 许可证](LICENSE)，版权署名为 Th7me。欢迎提交 issue 或 pull request，分享可复现的问题、翻译标准改进和 EPUB 兼容性修复。
