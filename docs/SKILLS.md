# 两个迁移技能与完整验收

Weft-Agent 首批迁移了原项目的 `code-documentation` 和 `newsletter-generation`。
两者只需要文件工具，不依赖 Docker、Python 执行或外部搜索服务。

## 目录与来源

```text
skills/
  catalog.json                         模型可见的名称和说明
  PROVENANCE.json                      原始路径及原文件 SHA-256
  LICENSE                              上游 MIT 许可
  code-documentation/
    SKILL.md                           适配后的运行说明
    upstream/SKILL.md                   原始文件的未修改副本
  newsletter-generation/
    SKILL.md
    upstream/SKILL.md
```

这是按现有能力做的精简迁移，不是声称完整实现原版全部功能。
代码文档技能保留源码分析、API 说明、示例与质量核对；去掉固定 /mnt 路径、bash 和 present_files。
简报技能保留材料筛选、结构化写作、出处与数字核对；当前仅支持用户提供的材料，不联网检索新闻。
原始技能仅供追溯，不会被加载器自动发送给模型或执行其中的工具调用指导。

## 加载流程

1. `main.py` 从本项目 skills/catalog.json 读取受信任元数据，注入系统提示。
2. 模型判断任务匹配后，调用 `load_skill(name)` 读取指定技能正文。
3. 完整正文作为普通 ToolResult 回填；技能不改变系统权限或注册新的工具。
4. 模型使用 read_file 获取材料、write_file 生成产物、read_file 回读验证。
5. 事件可查看 load_skill 的执行；SQLite 工具结果保留技能名称、正文和 SHA-256。

技能加载器只接受目录中注册的名称，不接受任意路径；检查解析后路径和正文大小（16000 字节）。
元数据目录属于应用配置，不能让不可信第三方直接修改。当前不支持在线安装、技能扫描、
allowed-tools 限制、技能版本升级或自动选择确定性保证。
长会话压缩可能摘要掉技能正文；系统目录仍可见，模型可以再次加载，但当前没有技能正文永久钉住机制。

## 日常使用

将待处理文件放入 workspace，然后在 Weft-Agent 目录运行：

```powershell
python main.py '读取 calculator.py，生成 outputs/README.md 使用说明并回读核对' --thread docs
python main.py '根据 notes.md 生成 outputs/newsletter.md 简报，标明材料来源并回读核对' --thread newsletter
```

示例输入在 examples/skills_inputs/。简报输入是明确标注的合成材料，example.invalid 链接只是占位引用，
不代表真实新闻。两种任务默认不需要 --sandbox。

## 自动验收

```powershell
# 离线协议测试：脚本假模型，不证明真实写作能力
python smoke_skills.py
# 实际模型完成两项任务：产生 API 用量，使用本地 .env
python smoke_skills.py --live
python -m unittest discover -s tests -v
```

每次验收创建新的 workspace/skill-smoke-<id>/，不覆盖原有产物。
报告保存到 .state/skill-smoke/last_report.json，包含模式、每个检查项、产物路径和事件路径。
验证项：正确技能成功加载 → 读取输入 → 写文件 → 回读产物；产物存在；关键 API/数字/来源包含在内容里。
这些检查验证工作流和具体关键事实，不是完整的文档语义质量评测；真实模型产物仍应人工阅读。

## 改名说明

项目文件夹从 agent_baseline 改为 Weft-Agent，.env、workspace、.state 一并迁移。
内部 Python 包名保持 baseline，避免同时扩大导入 API 的变更范围。
历史事件里的绝对路径是原运行记录，不重写；新运行自动使用新目录。
Docker 的 agent-baseline 容器标签暂时保留，保证旧容器仍可用原清理方式定位。
