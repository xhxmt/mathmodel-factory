#!/usr/bin/env python3
"""Deterministic paper perturbations for calibration without human labels."""

from __future__ import annotations

import re
from typing import Callable


def _remove_section(text: str, keywords: tuple[str, ...]) -> str:
    lines = text.splitlines(keepends=True)
    output: list[str] = []
    removing = False
    level = 0
    for line in lines:
        stripped = line.strip()
        match = re.match(r"^(#{1,6})\s+(.+)", stripped)
        numbered = re.match(r"^([一二三四五六七八九十]+)[、．.]\s*(.+)", stripped)
        latex = re.match(r"^\\(section|subsection)\{(.+)\}", stripped)
        if match or numbered or latex:
            if match:
                current_level = len(match.group(1))
                heading = match.group(2).lower()
            elif numbered:
                current_level = 2
                heading = numbered.group(2).lower()
            else:
                current_level = 1 if latex.group(1) == "section" else 2
                heading = latex.group(2).lower()
            if any(keyword.lower() in heading for keyword in keywords):
                removing = True
                level = current_level
                continue
            if removing and current_level <= level:
                removing = False
        if not removing:
            output.append(line)
    return "".join(output)


def _numeric_contradiction(text: str) -> str:
    pattern = re.compile(r"(?<!\d)(\d+\.\d{2,6})(?=\s*(?:s|秒|%|m|米|元))")
    match = pattern.search(text)
    if not match:
        return text + "\n\n主要结果复核后由 1.00 改为 9.00，但正文其他位置仍采用原值。\n"
    original = float(match.group(1))
    replacement = f"{original * 1.73:.{len(match.group(1).split('.')[1])}f}"
    return text[: match.start()] + replacement + text[match.end() :]


def _unsupported_optimality(text: str) -> str:
    return text + (
        "\n\n## 最优性补充声明\n\n"
        "本文所有启发式搜索所得结果均已严格证明为唯一全局最优解，"
        "无需进一步给出下界、最优性证书或穷举证明。\n"
    )


def _duplicate_robotic_prose(text: str) -> str:
    paragraph = (
        "首先，本文建立模型。其次，本文求解模型。最后，本文验证模型。"
        "综上所述，模型具有较强的科学性、先进性、合理性和广泛推广价值。"
    )
    return text + "\n\n## 补充总结\n\n" + "\n\n".join([paragraph] * 4) + "\n"


def _remove_answers(text: str) -> str:
    reduced = _remove_section(text, ("结论", "结果分析", "模型求解", "求解结果"))
    return reduced if len(reduced) < len(text) else text[: max(1000, len(text) * 2 // 3)]


PERTURBATIONS: dict[str, Callable[[str], str]] = {
    "no_sensitivity": lambda text: _remove_section(
        text, ("灵敏度", "敏感性", "鲁棒性", "sensitivity")
    ),
    "no_symbols": lambda text: _remove_section(
        text, ("符号说明", "符号表", "变量说明", "notation")
    ),
    "numeric_contradiction": _numeric_contradiction,
    "unsupported_optimality": _unsupported_optimality,
    "robotic_repetition": _duplicate_robotic_prose,
    "missing_answers": _remove_answers,
}


def apply_perturbation(text: str, perturbation: str | None) -> str:
    if not perturbation:
        return text
    try:
        transformed = PERTURBATIONS[perturbation](text)
    except KeyError as exc:
        raise ValueError(f"unknown proxy perturbation: {perturbation}") from exc
    if transformed == text:
        raise ValueError(f"proxy perturbation made no change: {perturbation}")
    return transformed
