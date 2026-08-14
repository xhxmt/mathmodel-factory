# Technique for Order Preference by Similarity to an Ideal Solution (TOPSIS)

> 来源：LLM-MM-Agent HMML；本文件由 `scripts/import_hmml.py` 确定性生成。

## 分层位置

Evaluation Methods (Evaluation Methods) → Scoring Evaluation (Scoring Evaluation)

## 建模方法

The Technique for Order Preference by Similarity to Ideal Solution (TOPSIS) is a multi-criteria decision analysis method. Its core idea is to evaluate the similarity of each alternative to the ideal solution by constructing an ideal solution and a negative ideal solution, thereby ranking and selecting the alternatives.

## 核心思想

The basic steps of the TOPSIS method include: 1. Construct the decision matrix: List all alternatives and their corresponding indicator values. 2. Standardize the data: Standardize the decision matrix to eliminate the influence of different dimensions. 3. Construct the weighted standardized decision matrix: Weight the standardized matrix according to the importance (weight) of each indicator. 4. Determine the ideal solution and the negative ideal solution: The ideal solution is the optimal value of each indicator, and the negative ideal solution is the worst value of each indicator. 5. Calculate the distance of each alternative from the ideal solution and the negative ideal solution: Usually using Euclidean distance or other distance measurement methods. 6. Calculate the relative closeness: Calculate the relative closeness based on the distance of each alternative from the ideal solution and the negative ideal solution as the basis for ranking.

## 典型应用

The TOPSIS method is widely used in the following fields: Supplier selection: Evaluating the comprehensive capabilities of different suppliers, such as quality, delivery time, and price, to select the best supplier. Project evaluation: Conducting comprehensive evaluations of multiple projects, considering factors such as investment return, risk, and resource requirements, to determine the priority projects. Human resource management: In employee performance evaluation and promotion, comprehensively considering factors such as work performance, ability, and potential for scientific assessment. Product design: In new product development, evaluating the feasibility, cost, and market demand of different design schemes to select the best design. Environmental impact assessment: In environmental management, evaluating the impact of different schemes on the environment, such as pollutant emissions and resource consumption, to formulate effective environmental protection measures.

## 使用边界

该条目来自广覆盖方法目录，仅用于候选召回；实际采用前必须结合题目数据、假设、求解器与失败模式做二次审查。
