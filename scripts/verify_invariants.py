#!/usr/bin/env python3
"""verify_invariants.py — 数值不变量门禁（Gate 1 的"正确性"补充）。

verify_numbers.py 只查"论文数字可追溯到 results/"（溯源），不查"results/ 里的
数字本身是否自洽"（正确性）。本脚本独立重算模型声明的不变量：

  - 聚合恒等式：总量 = 分项之和（例：总遮蔽时长 = 各弹遮蔽时长之和）
  - 单调性/支配关系：题目蕴含的必然不等式（例：三弹遮蔽 ≥ 单弹遮蔽）
  - 守恒/边界：占比之和 = 100%，概率 ∈ [0,1] 等
  - 严格增益（gt_strict）：更多资源必须带来 > margin 的改进（例：三弹 > 单弹 + 0.1s）。
    弱不等式 `ge` 会在"三弹==单弹"时误判 PASS（rerun_0706 P3=P2=4.53 的真实漏网）；
    对"资源支配"类关系一律用 gt_strict，把"加资源零收益"变成 FAIL。
  - 非零贡献（nonzero）：已部署的资源（弹/机/轮次）贡献必须 > min_abs，
    否则是"分配了但零遮蔽"的搜索失败信号（rerun_0706 P3 第三弹、P5 10/15 弹为 0）。

不变量在 Step 5 求解定稿时声明于 `results/invariants.json`：

  {
    "invariants": [
      {"name": "问题3总时长=各弹之和", "type": "sum",
       "total": "canonical:p3.total_time",
       "parts": ["canonical:p3.bombs[0].time", "canonical:p3.bombs[1].time"],
       "tol": 0.01},
      {"name": "三弹最优≥单弹最优", "type": "ge",
       "left": "canonical:p3.objective", "right": "canonical:p2.objective"},
      {"name": "占比和=100", "type": "sum", "total": 100.0,
       "parts": ["canonical:p5.share[0]", "canonical:p5.share[1]"], "tol": 0.5},
      {"name": "遮蔽时长序列非降", "type": "monotone", "direction": "nondecreasing",
       "series": ["canonical:p2.t1", "canonical:p3.t", "canonical:p4.t"]}
    ]
  }

引用语法 `<别名>:<点路径>`：
  - `canonical:` → `results/canonical_results.json`
  - `p<N>:`      → `results/p<N>/values.json`（或 `results/problem<N>/values.json`）
  - `results/xxx.json:` → 项目内任意 JSON 文件
  - 纯数字字面量直接写数值。
点路径支持 `a.b[2].c`。

Usage:
    python3 verify_invariants.py <project_dir>

Exit code: 0 = 全部通过（或无 invariants.json 时 SKIP）；1 = 有不变量被违反。
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

DEFAULT_TOL = 1e-6


def read_json(path: Path):
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def resolve_file(project: Path, alias: str) -> Path | None:
    if alias == "canonical":
        return project / "results" / "canonical_results.json"
    m = re.fullmatch(r"p(\d+)", alias)
    if m:
        for cand in (
            project / "results" / f"p{m.group(1)}" / "values.json",
            project / "results" / f"problem{m.group(1)}" / "values.json",
        ):
            if cand.is_file():
                return cand
        return None
    cand = project / alias
    return cand if cand.is_file() else None


def dig(data, dotted: str):
    """按 a.b[2].c 取值。"""
    for token in re.findall(r"[^.\[\]]+|\[\d+\]", dotted):
        if token.startswith("["):
            data = data[int(token[1:-1])]
        else:
            data = data[token]
    return data


def resolve_ref(project: Path, ref, cache: dict) -> float:
    if isinstance(ref, (int, float)) and not isinstance(ref, bool):
        return float(ref)
    if not isinstance(ref, str):
        raise ValueError(f"无法解析引用: {ref!r}")
    if ":" not in ref:
        raise ValueError(f"引用缺少文件别名前缀: {ref!r}")
    alias, dotted = ref.split(":", 1)
    path = resolve_file(project, alias)
    if path is None or not path.is_file():
        raise FileNotFoundError(f"引用 {ref!r} 的文件不存在（别名 {alias!r}）")
    if path not in cache:
        cache[path] = read_json(path)
    value = dig(cache[path], dotted)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"引用 {ref!r} 解析到非数值: {value!r}")
    return float(value)


def resolve_list(project: Path, ref: str, cache: dict, field: str | None = None) -> list[float]:
    """把 alias:dotted 解析成 float 列表 (nonzero_each 用)。

    dotted 的叶子必须是 JSON 数组。数组元素可以是数字, 或 dict — 后者需给
    `field` 从每个 dict 取标量 (如 per_smoke 列表里每项的 contribution)。"""
    if not isinstance(ref, str) or ":" not in ref:
        raise ValueError(f"nonzero_each 的 array 需为 alias:dotted 引用: {ref!r}")
    alias, dotted = ref.split(":", 1)
    path = resolve_file(project, alias)
    if path is None or not path.is_file():
        raise FileNotFoundError(f"引用 {ref!r} 的文件不存在（别名 {alias!r}）")
    if path not in cache:
        cache[path] = read_json(path)
    data = dig(cache[path], dotted)
    if not isinstance(data, list):
        raise ValueError(f"引用 {ref!r} 解析到非数组: {type(data).__name__}")
    out: list[float] = []
    for i, item in enumerate(data):
        if field is not None:
            if not isinstance(item, dict) or field not in item:
                raise ValueError(f"{ref!r}[{i}] 缺少字段 {field!r}")
            item = item[field]
        if isinstance(item, bool) or not isinstance(item, (int, float)):
            raise ValueError(f"{ref!r}[{i}] 非数值: {item!r}")
        out.append(float(item))
    if not out:
        raise ValueError(f"{ref!r} 解析到空数组 (无资源可查, 疑似落盘缺失)")
    return out


def check_invariant(project: Path, inv: dict, cache: dict) -> tuple[bool, str]:
    kind = inv.get("type", "")
    tol = float(inv.get("tol", DEFAULT_TOL))
    if kind == "sum":
        total = resolve_ref(project, inv["total"], cache)
        parts = [resolve_ref(project, p, cache) for p in inv["parts"]]
        s = sum(parts)
        ok = abs(total - s) <= max(tol, tol * abs(total))
        return ok, f"total={total:g} sum(parts)={s:g} diff={total - s:+g} tol={tol:g}"
    if kind in ("eq", "ge", "le"):
        left = resolve_ref(project, inv["left"], cache)
        right = resolve_ref(project, inv["right"], cache)
        if kind == "eq":
            ok = abs(left - right) <= max(tol, tol * abs(right))
        elif kind == "ge":
            ok = left >= right - tol
        else:
            ok = left <= right + tol
        return ok, f"left={left:g} {kind} right={right:g} tol={tol:g}"
    if kind == "gt_strict":
        # C1: STRICT dominance with a positive margin.  Motivation: rerun_0706
        # declared "P3 三弹 ≥ P2 单弹" as `ge` and passed on P3==P2==4.53 — the
        # exact signature of a search that never used the extra resource.  A
        # strict-gain invariant turns "more resource, no improvement" into FAIL.
        # margin defaults to 0.1 (s); override per-invariant.
        left = resolve_ref(project, inv["left"], cache)
        right = resolve_ref(project, inv["right"], cache)
        margin = float(inv.get("margin", 0.1))
        ok = left > right + margin
        return ok, f"left={left:g} > right={right:g} + margin={margin:g} (gain={left - right:+g})"
    if kind == "nonzero":
        # C1: a DEPLOYED resource must make a positive contribution.  A value at
        # or below `min_abs` means a bomb/UAV/round that was allocated but
        # contributes zero masked time — a search-failure signature (rerun_0706
        # P3 third bomb T_single=0, P5 10/15 bombs zero).
        value = resolve_ref(project, inv["value"], cache)
        min_abs = float(inv.get("min_abs", 1e-6))
        ok = value > min_abs
        return ok, f"value={value:g} > min_abs={min_abs:g} (deployed resource must contribute)"
    if kind == "nonzero_each":
        # A2 (blind 2025A): batch version of `nonzero`.  `nonzero` checks one
        # scalar; a solve deploying 15 smokes needs every element of an array
        # checked.  `array` is an alias:dotted path whose LEAF is a JSON list
        # (numbers, or dicts from which `field` selects the contribution).
        # blind P4 smoke3 contributed 0.000s, P5 had 7/15 dead smokes — each a
        # wasted resource that a scalar invariant cannot see.
        values = resolve_list(project, inv["array"], cache, inv.get("field"))
        min_abs = float(inv.get("min_abs", 1e-6))
        bad = [i for i, x in enumerate(values) if not (x > min_abs)]
        ok = not bad
        return ok, (
            f"n={len(values)} zero_contrib_idx={bad} min_abs={min_abs:g} "
            f"(每个已部署资源都必须贡献 > min_abs)"
        )
    if kind == "quantized_off":
        # A2 (blind 2025A): the canonical objective must NOT sit exactly on a
        # Δt grid multiple.  blind shipped 1.30/4.45/7.30/8.25/24.55 — all exact
        # multiples of dt=0.05 — because the coarse-grid COUNT was written as
        # canonical while the bisection-refined value (1.35/1.39...) was demoted
        # to a diagnostic.  A refined interval-union duration is essentially
        # never an exact grid multiple; landing on one is the quantization red
        # flag.  Params: value, dt, eps (fractional tolerance, default 0.02).
        value = resolve_ref(project, inv["value"], cache)
        dt = float(inv["dt"])
        eps = float(inv.get("eps", 0.02))
        ratio = value / dt
        frac = abs(ratio - round(ratio))
        ok = frac > eps
        return ok, (
            f"value={value:g} dt={dt:g} value/dt={ratio:.4f} "
            f"frac_from_int={frac:.4f} > eps={eps:g} "
            f"(采信值恰为 Δt 整数倍 = 未做边界精化的量化红旗)"
        )
    if kind == "monotone":
        series = [resolve_ref(project, r, cache) for r in inv["series"]]
        direction = inv.get("direction", "nondecreasing")
        pairs = zip(series, series[1:])
        if direction == "nondecreasing":
            ok = all(b >= a - tol for a, b in pairs)
        elif direction == "nonincreasing":
            ok = all(b <= a + tol for a, b in pairs)
        else:
            return False, f"未知 direction: {direction!r}"
        return ok, f"series={[f'{v:g}' for v in series]} ({direction})"
    if kind == "range":
        value = resolve_ref(project, inv["value"], cache)
        lo = float(inv.get("min", float("-inf")))
        hi = float(inv.get("max", float("inf")))
        ok = lo - tol <= value <= hi + tol
        return ok, f"value={value:g} ∈ [{lo:g}, {hi:g}]"
    return False, f"未知不变量类型: {kind!r}"


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    project = Path(sys.argv[1]).resolve()
    spec_path = project / "results" / "invariants.json"
    if not spec_path.is_file():
        print("INVARIANTS_SPEC=MISSING")
        print("VERDICT: SKIP (no results/invariants.json)")
        return 0
    try:
        spec = read_json(spec_path)
    except Exception as exc:
        print(f"INVARIANTS_SPEC=PARSE_ERROR {exc}")
        print("VERDICT: FAIL")
        return 1

    invariants = spec.get("invariants") or []
    if not invariants:
        print("INVARIANTS_TOTAL=0")
        print("VERDICT: FAIL (invariants.json 存在但为空 — 至少声明 3 条)")
        return 1

    cache: dict = {}
    failed = 0
    errored = 0
    for inv in invariants:
        name = inv.get("name", "<unnamed>")
        try:
            ok, detail = check_invariant(project, inv, cache)
        except Exception as exc:
            errored += 1
            print(f"[ERROR] {name}: {exc}")
            continue
        marker = "PASS" if ok else "FAIL"
        if not ok:
            failed += 1
        print(f"[{marker}] {name}: {detail}")

    print(f"INVARIANTS_TOTAL={len(invariants)}")
    print(f"INVARIANTS_FAILED={failed}")
    print(f"INVARIANTS_ERRORED={errored}")
    if failed or errored:
        print("VERDICT: FAIL")
        return 1
    print("VERDICT: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
