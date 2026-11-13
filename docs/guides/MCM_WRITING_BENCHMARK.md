# 美赛（MCM/ICM）论文写作与评审基准

本文件是美赛项目写作环节（Step 8.5、9、11、12、14、15）的质量标尺，对应国赛的
`EXCELLENT_PAPER_WRITING_BENCHMARK.md` 与 `NATIONAL1_CALIBRATION_ANCHOR.md`。
美赛项目的提示词开头有「美赛（MCM/ICM）专用要求」段，指向本文件；与国赛文件冲突时以本文件为准。

## 0. 依据

COMAP 不公布固定分值的评分细则（“Guidance on the judging criterion is given after the
contest ends”），以下规则综合自：

- COMAP MCM/ICM 竞赛说明（2026 届）：<https://www.contest.comap.com/undergraduate/contests/mcm/instructions.php>
- COMAP《Use of AI Tools in COMAP Contests》（v102025）：<https://www.contest.comap.com/undergraduate/contests/mcm/flyer/Contest_AI_Policy.pdf>
- 评委意见汇编《20 Years of Good Advice》（Driscoll、Griggs、Parker 等，综合历年 Judges' Commentary）：
  <https://contest.comap.com/undergraduate/contests/resources/PDF/20YearsofGoodAdvice.pdf>
- MCM/ICM 评审轮次说明（triage）：<https://sites.google.com/view/mcm-icm-triage/what-is-mcmicm-triage-judging>
- Outstanding（O 奖）论文的通行结构（《UMAP Journal》历年刊登的获奖论文与评委评论）。

规则每届可能调整。每次比赛以当届 `problem/source.md` 和 COMAP 当届说明为准；与本文件冲突时以当届说明为准。

## 1. 评审流程决定写法

1. **Triage（初筛）**：每篇至少两位评委阅读。评委意见原话：“can a triage judge understand the
   significance of your paper in 6 to 10 minutes?” 初筛主要看 Summary Sheet、目录、各节开头、
   主图主表，判断是否回答了全部问题。**大多数论文在这一轮就定档。**
2. **Screening（复筛）**：处理初筛意见不一致的论文。
3. **Final（终评）**：资深评委选出 Outstanding 与 Finalist，此时才会细读推导、验证和灵敏度分析。
   按轮次不同，评委每篇用时 5–30 分钟。

推论：Summary 是筛选器；每个任务的答案必须在正文中**容易找到**（小节标题、首句、主表）；
后几轮的细读要求模型有自己的推导、验证和灵敏度分析。

## 2. 硬性规则（2026 届）

- 全文 ≤ 25 页，**包括** Summary Sheet、目录、正文、参考文献、附录、代码、Memo/Letter 等题目要求的文档。
- 第 1 页是 Summary Sheet（题号、Team Control Number、标题、Summary）；英文；正文字号 ≥ 12 pt。
- 任何页面不得出现队员、指导老师、学校的名字；页眉只写 Team Control Number。
- 使用了 AI 工具：
  - 正文中对 AI 生成或协助的内容**行内引用**，并在 25 页之内的参考文献里列出所用 AI 工具；
  - 25 页之后追加 **Report on Use of AI Tools**，不限页数、不计入 25 页；每条写工具名称与版本（日期）、
    用途，以及 Query 与 Output（翻译用途可只写一句说明）；
  - 未披露的 AI 使用，COMAP 会处理（可能取消资格）。本工厂的论文由 AI 流水线产出，**必须披露**。

## 3. 评委看重什么

| 维度 | 评委要求（原话或要点） | 本工厂的落点 |
|---|---|---|
| Summary | “needs to be clear and contain results. It should not say, ‘Read inside for results.’” 写“bottom-line and managerial recommendation”，不是按时间顺序叙述做了什么；不能照抄 Introduction；不要罗列一长串技术名词 | Step 8.5 产出逐任务句组，Step 14 写成一页 Summary |
| 回答全部问题 | “Answer all the required parts and make it clear that you have done so.” 漏答多项的论文很快被淘汰 | 每个任务一节，小节首句给答案；结论节逐任务复述答案 |
| 假设 | 清楚陈述；不写用不到的假设；说明每条假设的影响和必要性；不能把问题中有挑战性的部分“假设掉” | `Assumptions and Justifications`，每条一句 Justification，并能在模型中找到用处 |
| 自己的建模 | 评委寻找“evidence whether a team actually did some modeling of its own”；“far better to work out one model thoroughly than to present several half-baked approaches”；先简单后细化 | 主模型讲透，辅助模型只用于对比或验证；不写试错流水账 |
| 结果与验证 | 模型必须产生结果，并用符号、图形、数值方法证明结果合理；与基线比较；仿真不能只跑一次 | 每个任务有主图或主表；写出对比基线、多次运行的统计量 |
| 灵敏度分析 | “undertake sensitivity analysis precisely to build credibility in the model”；与优缺点分开成节 | 独立 `Sensitivity Analysis` 节，数值来自 `sensitivity_report.md` |
| 优缺点 | 单独成节；回头逐条核对假设 | `Strengths` / `Weaknesses` 具体、带数字，不写空话 |
| 可读性 | 目录；答案位置清楚；页、表、图、公式编号；定义有歧义的术语；“Do not include a single figure, table, or graph that is extraneous” | 每张图表都在正文被引用并解释，带单位和坐标轴标签 |
| 参考文献 | “A mere list of books or URLs at the end is not sufficient!” 正文中具体引用；优先期刊、书、政府网站 | 每条参考文献在正文中被引用，数据来源写明 |
| 非技术文档 | Memo、新闻稿、政策简报等：简洁、给出“bottom line and managerial results”、**不写方法和公式** | 写给题目指定的读者，数字与正文一致 |

## 4. 初筛评委的雷区

- 表头用希腊字母或缩写，读者不能立刻看懂。
- 定义和符号埋在段落中间（用列表或符号表）。
- 公式中的变量未定义。
- 详细推导直接照搬的公式（应引用来源，简述来历，重点展示怎么用）。
- 公式凭空出现、图表没有解释、数据表放着不分析。
- 关键证据放在附录，正文需要它的地方却没有。
- 只给代码或伪代码，不解释算法为什么有效。

## 5. O 奖论文的通行结构（25 页预算）

| 部分 | 内容 | 经验页数 |
|---|---|---|
| Summary Sheet | 标题 + Summary + Keywords | 1 |
| Contents | 目录 | 1 |
| Introduction | Problem Background（用自己的话）、Restatement of the Problem（逐任务列出）、Our Work（一张流程图：任务 → 模型 → 输出） | 1.5–2 |
| Assumptions and Justifications | 4–8 条，每条带 Justification | 0.5–1 |
| Notations | 符号表（Symbol / Description / Unit） | 0.5 |
| Data Preprocessing（C 题、数据类题目） | 数据来源、清洗、异常值、特征构造 | 1–2 |
| 每个任务一节模型 | Model Formulation → Solution → Results，结果先行 | 共 10–13 |
| Sensitivity Analysis | 关键参数扰动、结论是否翻转 | 1.5–2 |
| Model Evaluation and Further Discussion | Strengths / Weaknesses / Further Discussion（推广要具体到改哪个输入、哪个约束） | 1 |
| Conclusion | 逐任务复述答案和建议 | 0.5 |
| Memo / Letter / Policy Brief（如要求） | 写给指定读者的非技术文档 | 1–2 |
| References | 正文中实际引用的文献，含 AI 工具条目 | 0.5–1 |
| Appendices | 只放复现主结果所需的核心代码 | ≤ 2 |
| Report on Use of AI Tools | 25 页之后，不计页数 | 不限 |

页数是经验值，不是官方要求；总数必须 ≤ 25。超页时先删附录代码，再合并次要图表，最后压缩推导。

## 6. Summary 的写法

- 一页之内，与标题、Keywords 同在第 1 页；一般 350–550 词。
- 结构：
  1. 1–2 句背景，说明问题为什么重要、本文用什么统一思路解决（不要从“With the development of …”开始）；
  2. **每个任务一段**：用什么模型、为什么这样建模（一句动机）、**精确的数值结果或明确的结论**、怎样验证；
     模型名称可以加粗，便于初筛评委定位；
  3. 1–2 句灵敏度或稳健性结论（带数字）；
  4. 如要求 Memo/Letter，一句话点明给谁、建议是什么；
  5. Keywords 4–6 个。
- 禁止：照抄 Introduction；“Read inside for results”式悬念；只罗列方法不报结果；按时间顺序叙述过程；
  没有数字的“our model performs well”；结尾泛泛的推广句。

示例（结构示意，不是真实论文）：

> To keep the regional grid stable while expanding solar capacity, we combine a demand forecast,
> a capacity-planning optimization and a reliability check. **For Task 1**, we fit a seasonal
> ARIMA model to 2015–2024 hourly load; on the held-out 2024 data it reaches a MAPE of 3.8%.
> **For Task 2**, a mixed-integer program chooses the sites and sizes of new plants; the optimal
> plan adds 412 MW at four sites and lowers annual cost by 11.6% against the current plan, …

## 7. 英文写作

- 评委明确偏好第一人称、现在时、主动语态：“We fit a fifth-degree polynomial to the data”，
  而不是“it was discovered that …”。**这与国赛禁止“我们”的规则相反。**
- 每个任务的小节首句给答案，再展开过程。
- 术语全文一致；有日常含义的术语（如 *efficiency*、*risk*）在首次出现时定义。
- 删去 AI 腔：*delve into*、*plays a crucial role*、*It is worth noting that*、*In conclusion*、
  *comprehensive*、*robust*（没有证据时）、*seamlessly*、*a myriad of*；不要每段都用同一句式开头。
- 不写免责清单；局限在 Weaknesses 中写一次，带数字或适用条件。
- 数字带单位，有效位数与求解精度一致；表格数字右对齐。

## 8. 题型差异

| 题型 | 重点 |
|---|---|
| A（连续） | 微分方程、物理机理；量纲与参数来源；数值方法的收敛和稳定性 |
| B（离散） | 优化、组合、算法；与基线或下界比较；复杂度与规模 |
| C（数据洞察） | Data Preprocessing 要扎实；样本外验证、预测区间；不要只堆模型 |
| D（运筹 / 网络科学） | 网络或优化结构的合理性；指标定义；情景比较 |
| E（可持续性） | 指标体系的权重要有依据并做灵敏度；跨尺度推广要具体 |
| F（政策） | 利益相关方、政策可行性、成本；政策建议要能落地，常要求 Policy Brief / Memo |

ICM（D/E/F）和 C 题更常要求非技术文档；要求的文档是题目的一部分，缺失会按漏答处理。

## 9. 非技术文档（Memo / Letter / Policy Brief / Newsletter）

- 读者是题目指定的人（市长、CEO、委员会、公众）；开头一句给出最重要的建议。
- 只写结论、建议、代价和风险，不写方法和公式；数字与正文一致，不引入正文没有的数字。
- 篇幅遵守题目要求（常为 1–2 页）；计入 25 页。

## 10. 自查清单

- [ ] Summary 在第 1 页，含每个任务的数值结果或明确结论，不照抄 Introduction
- [ ] 题目的每个要求都有对应小节，小节首句给答案；Conclusion 逐任务复述
- [ ] 每条假设有 Justification，并在模型中被用到
- [ ] 每个任务有主图或主表，图表有标题、单位、坐标轴标签，并在正文被引用和解释
- [ ] 有基线对比或验证；仿真报告多次运行的统计量
- [ ] Sensitivity Analysis 独立成节，带数字
- [ ] Strengths / Weaknesses 具体，不空泛
- [ ] 题目要求的 Memo / Letter 等文档齐全，面向指定读者，不含公式
- [ ] 每条参考文献在正文中被引用；AI 工具已行内引用并列入参考文献
- [ ] ≤ 25 页（`check_page_count.py` 已扣除 AI 报告页）；Report on Use of AI Tools 在 `\AImatter` 之后
- [ ] 全文无队员、指导老师、学校信息
