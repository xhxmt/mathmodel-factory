# Changelog

本文档记录 Paper Factory (Modeling Factory) 的重要更新。

## [Unreleased]

## [2026-06-24] - 优秀论文可视化与写作框架系统性改进

### 新增

#### 优秀论文基准文档
- **`docs/guides/EXCELLENT_PAPER_VISUALIZATION_BENCHMARK.md`** - 优秀论文可视化基准
  - 四类叙事角色定义：`explain_model` / `report_result` / `validate_result` / `show_limitation`
  - 六条选图规则：视觉锚点、解释图、可信度图、图表分工、路径题三件套、空间分布多宫格
  - 负面模式清单：不画工程流程、不让粗网格抢主图、不为凑数画图
  
- **`docs/guides/EXCELLENT_PAPER_WRITING_BENCHMARK.md`** - 优秀论文写作基准
  - 五条核心规则：
    1. 摘要采用"开头总述 + 逐问交付"（对齐 2024A A242/A163、2025A A196）
    2. 问题分析写成阅卷索引（难点 → 对象/变量 → 方法 → 输出）
    3. 模型求解先报最终采信口径，诊断和未采信分支后置
    4. 验证支撑可信度，不制造不确定感
    5. 删除内部工程痕迹（m1/m2/results/*.json/RELAXED/fallback/workflow）

#### 可视化架构改进
- **Step 8 (visualization)**: 
  - 引入强制叙事角色分类机制
  - `visualization_log.md` 表格新增"叙事角色"和"依据来源"列
  - 增加"每个子问题至少一个视觉锚点"规则
  - 扩充禁止事项清单：工程流程图、粗网格抢主图、为凑数画图

#### 论文写作框架重构
- **Step 9 (paper_draft)**:
  - 摘要结构从"四段法"改为"开头总述 + 逐问交付"
  - 问题分析要求按"难点 → 对象/变量 → 方法 → 输出"写阅卷索引
  - 模型求解章节先报最终采信口径，诊断后置
  - 图表按叙事角色摆放（explain_model → 问题分析，report_result → 模型求解等）

- **Step 11 (constructive_review)**:
  - 新增"图表质量评估"章节，按优秀论文可视化基准检查
  - 写作评估扩充：摘要结构、问题分析索引、结果口径、工程痕迹

- **Step 12 (revision)**:
  - 优先对齐优秀论文基准：逐问交付、采信口径先行、验证支撑可信度、删除工程痕迹
  - 增加"改文字不改底层"红线说明

- **Step 13 (gate2_judge)**:
  - 新增"优秀论文写作基准检查"五项
  - 新增"优秀论文可视化基准检查"五项
  - 摘要素材提示改为"开头总述 + 逐问交付"结构

- **Step 14 (abstract)**:
  - 模板从"四段散文"改为"总述 + 逐问段落 + 可选亮点收束"
  - 每问段必须按"模型/算法 → 关键结果 → 验证或附件"写

- **Step 15 (polish)**:
  - 新增"内部工程痕迹"检查章节
  - 新增"风险措辞重写"章节（"脆弱/翻转" → "验证/收敛/稳定性"）

### 变更

#### modeling_guide.md
- **§LaTeX Document Requirements**: 摘要描述从"四段法"改为"功能导向 + 分问优先逐问交付"
- **新增 §Figure Selection**: 定义四类叙事角色，明确视觉锚点、解释图、可信度图规则
- **§Color Palette**: 从原 Paper Factory 商业配色改为学术配色
  - Deep blue `#2E5C8A` / Brick red `#C04D4D` / Forest green `#4D9D5B` / Amber `#D49B3E` / Royal purple `#6B4D9A`

### 参考材料

本次改进基于优秀论文深度分析（数据由 `scripts/dxs_*.py` 下载至本地 `external/`，分析文稿见 `docs/reference/`）：
- **2024A**: A163, A242, A016, A053（板凳龙题）
- **2025A**: A196（烟幕弹题）
- **2023A**: A0165（定日镜题）

分析报告见：
- `evaluation/recent_runs_vs_excellent_papers_diagnosis.md` - 最近运行与优秀论文对比诊断
- `docs/reference/2024A_writing_comparison.md` - 2024A 写作对标
- `docs/reference/2023_2025A_writing_commonality.md` - 跨年优秀论文写作共性
- `docs/reference/excellent_paper_visualization_study.md` - 优秀论文可视化方案学习

### 预期效果

- **可视化质量**: 主文图密度提升 30%（8-10 张精准图 vs 12-15 张混杂图）
- **审稿效率**: Step 11 审稿图表调整建议从平均 4.5 条降至 1.5 条
- **评委评分**: Step 13 图表质量评分从 7.2/10 提升至 8.3/10
- **摘要相似度**: 与优秀论文相似度从 65% 提升至 85%+
- **工程痕迹**: 残留从平均 8 处降至 <2 处

---

## [2026-06-23] - GCP 集成与文档清理

### 新增
- GCP Secret Manager 集成
- Cloud Run Solver 服务
- 文档结构化重组

---

## [2026-06-22] - Web Dashboard 前端重构

### 新增
- 逐步选模型界面
- 模型管理功能
- 控制台前端重构

---

## [2026-06-21] - 论文写作环节修复

### 修复
- P0/P1 级别错误修复
- 数值验证完整性提升

---

## 文档说明

- **[YYYY-MM-DD]**: 发布日期
- **新增**: 新功能或新文件
- **变更**: 现有功能的修改
- **修复**: Bug 修复
- **移除**: 移除的功能或文件
- **废弃**: 即将移除的功能

更多技术细节请参考各版本的 commit 记录。
