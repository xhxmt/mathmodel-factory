# Infectious Disease Model

> 来源：LLM-MM-Agent HMML；本文件由 `scripts/import_hmml.py` 确定性生成。

## 分层位置

Prediction (Prediction) → Continuous Prediction (Continuous Prediction) → Differential Equations (Differential Equations, DE)

## 建模方法

Infectious disease models use mathematical and statistical methods to describe and analyze the spread of infectious diseases in populations. By establishing these models, researchers can predict the development trends of epidemics, evaluate the effectiveness of control measures, and provide scientific evidence for public health decision-making.

## 核心思想

Infectious disease models typically divide the population into different states or "compartments," each representing individuals with the same health status. Common compartments include: Susceptible (S): Individuals who have not yet been infected but are at risk of infection. Exposed (E): Individuals who have been exposed to the pathogen but are not yet infectious. Infectious (I): Individuals who are infectious and can spread the disease. Recovered (R): Individuals who have recovered from the infection and usually have immunity. Depending on the characteristics of the disease, the specific compartmentalization and parameter settings of the model may vary. Common models: 1. SIR Model: Divides the population into Susceptible, Infectious, and Recovered, suitable for diseases with no latent period and no loss of immunity. 2. SEIR Model: Adds an Exposed state to the SIR model, suitable for diseases with a latent period. 3. SIRS Model: Considers the possibility of recovered individuals losing immunity and becoming susceptible again.

## 典型应用

Epidemic prediction: Predicting the spread speed, peak, and duration of infectious diseases. Control strategy evaluation: Assessing the effectiveness of measures such as quarantine, vaccination, and social distancing. Resource optimization: Optimizing the allocation of medical resources, such as beds, medications, and protective equipment.

## 使用边界

该条目来自广覆盖方法目录，仅用于候选召回；实际采用前必须结合题目数据、假设、求解器与失败模式做二次审查。
