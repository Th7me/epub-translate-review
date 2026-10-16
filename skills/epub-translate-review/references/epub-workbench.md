# EPUB 文本工作台

`scripts/epub_workbench.py` 是本 skill 的文件处理工具，使用 Python 标准库，建议 Python 3.10 或以上。它不调用模型、翻译 API 或网络。用户仍需让代理完成翻译和真实回源审校。

## 支持范围

- 从 `META-INF/container.xml` 读取实际 OPF 路径，不假定固定目录。
- 检查 manifest、spine、EPUB 导航和 NCX。spine 项数不是小说章数，需另外读标题/目录确认。
- 抽取 spine 中可处理 XHTML 的非纯空白 body `text`/`tail` 字段；同时给出完整段落上下文和稳定定位。
- 回填时只替换原 XML 字符流中的文本，保留命名空间、标签、内联元素、段落、路径和未修改资源。
- 支持部分范围回填；未提供的字段保持源文。全范围要求可用 `--require-complete`。

**正文完成范围不等于整书完成范围。** 工具默认不修改 OPF 书名/语言、head/title、NCX 和未参与回填的目录。若目录 XHTML 也在 spine 中，其 body 会作为普通文档抽取。协调者必须识别前言/目录/封面，选择正确范围；需要中文目录、语言元数据时另行做最小修改，再验证最终文件。

工具要求可解析的 XML、UTF-8 XHTML 和普通文本槽。正文槽内有 CDATA、注释、处理指令或不能可靠映射的结构时会拒绝处理；不要删除这些内容来强行通过，应适配定位器或换用能保结构的工具。它不是 EPUBCheck 的替代品，也不能确认目标阅读器兼容或文学质量。

## 命令

以下命令从 skill 目录执行；`work/` 是示例项目位置，实际工作目录应放在 skill 外面。所有路径都可改为绝对路径。

```text
python scripts/epub_workbench.py --help
python scripts/epub_workbench.py inspect --epub input.epub --output inspection.json
python scripts/epub_workbench.py extract --epub input.epub --output work.json
python scripts/epub_workbench.py build --source input.epub --work work.json --translations reviewed.json --output translated.epub --report report.json
python scripts/epub_workbench.py validate --epub translated.epub --output validation.json
```

完整抽取范围必须都有已审译文时：

```text
python scripts/epub_workbench.py build --source input.epub --work work.json --translations reviewed.json --output translated.epub --require-complete --report report.json
```

`validate --epub input.epub --work work.json` 可检查**原书**相对抽取时是否漂移。不能拿原书 work.json 与已改译的输出做该比较：正文变化正是翻译结果，必然不同。验证输出用普通 `validate`，回填一致性由 build 负责。

源书、工作 JSON、译文 JSON、输出和报告应使用互不冲突的文件名；先保留旧版，再写新版本。build 默认拒绝覆盖已有 EPUB；明确需要替换时才使用 `--overwrite`。发生错误时查看非零退出码与错误 JSON，不把残留旧文件当作本轮新结果。

## 抽取与翻译数据

`extract` 产生一个 work manifest，其中包括出版物结构、文档与字段列表。每个字段有：

- `field_id`：文档与节点/字段的稳定标识。
- `source`：解码后的准确源文字串。
- `source_lexical`：原 XML 中的字面文本，例如 `&amp;`。
- 文档、节点、`text`/`tail` 定位及 `block_source`：完整段落上下文。

不要在手工译稿中抄写 `source_lexical`；从抽取结果直接复制 `field_id` 和 `source`，避免实体、空格、引号漂移。翻译前先读 `block_source`，按段落理解，再分配回原字段。

审校输入格式：

```json
{
  "schema": "epub-translate-review/translations-v1",
  "fields": [
    {
      "field_id": "manifest:chapter::/html[1]/body[1]/p[1]::text",
      "source": "The door is not locked.",
      "translation": "门没有锁。",
      "reviewed": true
    }
  ]
}
```

示例为原创短句。真实 ID 必须取自实际 work.json，不能照搬本示例。`reviewed: true` 是主审在完成回源后填写的**声明**，工具只验证字段值，不能据此证明有人审过。

worker 可另存未审文件和自查记录；不要为了进入 build 而批量把未审稿标为 true。冻结初稿、审校决定、术语库和真实进度由协调者在项目目录维护，不嵌入 EPUB 正文。

## 部分批次与续作

1. 从原书抽取完整 work.json，依据目录、正文和 `documents` 确定本批应覆盖的字段集合。
2. 对照这个集合检查译稿是否完整；部分 build 自身只能说明提交了哪些字段，不能替你判断用户要求的第几章是否已全部提交。
3. 将本批已审字段与此前已审字段按 `field_id` 合并，冲突时回看审校记录，不自动用新初稿覆盖旧成稿。
4. 每次从同一原书加累计已审 JSON 重建；不要把混合译本再次当作新原书，否则源文定位与审校依据会变化。
5. `status: partial` 表示仍有未提交的**抽取字段**；`status: complete` 只表示这些字段全部提交，不代表其他元数据已译、语义可靠或 UI 已测。

保留主助手旧亲译时，将其映射回相同源字段并复核，再加入累计已审集合；原旧稿另存。

## 检查与限制

build 会核对源文、ID、声明状态、换行及回填结构，先生成临时 EPUB，检查成功后再形成输出。检查包括 ZIP/CRC、mimetype、XML、manifest/spine、本地引用及 XML 片段锚点；未改资源按实际字节核对。报告中的 `completion_scope` 明确覆盖抽取的 spine body 字段，`review_flags_are_attestations` 提醒审校状态仅是调用者声明，`metadata_unchanged` 说明 OPF 元数据未改。

正文硬换行按源字段约定保留，不能为了视觉排版随意新增；原有 `<br>` 和标签不变。源语言残留、意外控制字符和术语一致性另由协调者按项目目标检查，不机械禁止所有非中文字符。

工具目前要求非空译文字段。如果中文语序确实需要清空一个内联片段，不能用空格填充或重复文字凑数；应先适配映射格式与检查逻辑，并用该段的端到端样本验证，或对该段作保结构的单独处理。

目标阅读器实测至少看：目录跳转、开头、含内联格式/长段的中部、本批结尾。无法实测时交付结构检查报告并标明未测，不把 `ok: true` 写成“所有阅读器打开成功”。
