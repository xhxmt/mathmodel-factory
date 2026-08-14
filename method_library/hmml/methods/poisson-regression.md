# Poisson Regression

> 来源：LLM-MM-Agent HMML；本文件由 `scripts/import_hmml.py` 确定性生成。

## 分层位置

Machine Learning (Machine Learning) → Regression

## 建模方法

Poisson Regression is a widely used regression analysis method in statistics and econometrics, mainly used for modeling count data and contingency tables.

## 核心思想

Poisson Regression assumes that the dependent variable (response variable) follows a Poisson distribution and that its expected value can be expressed as a linear combination of independent variables. Specifically, the model form is: \[ \log(\mathbb{E}(Y \mid \mathbf{x})) = \alpha + \mathbf{\beta}^\prime \mathbf{x} \] where \( Y \) is the dependent variable, \( \mathbf{x} \) is the vector of independent variables, \( \alpha \) is the intercept term, and \( \mathbf{\beta} \) is the vector of regression coefficients.

## 典型应用

Poisson Regression is widely used in the following fields: Count data modeling: such as the number of phone calls per unit time, the number of traffic accidents, etc. Epidemiological research: analyzing the relationship between disease incidence and risk factors. Social science research: studying the influencing factors of social phenomena such as crime rates and traffic violations.

## 使用边界

该条目来自广覆盖方法目录，仅用于候选召回；实际采用前必须结合题目数据、假设、求解器与失败模式做二次审查。
