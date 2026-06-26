#!/usr/bin/env python3
"""
Step 13 评委打分缓存机制

缓存论文内容的哈希值和对应的评分结果,避免相同内容重复评分。
支持部分匹配(论文结构相似度检测)以提供增量评分建议。

缓存位置: <project>/.step13_cache.json
缓存结构:
{
  "cache_version": "1.0",
  "entries": [
    {
      "timestamp": "2026-06-23T10:30:00",
      "paper_hash": "sha256:...",
      "paper_sections_hash": {
        "abstract": "sha256:...",
        "model": "sha256:...",
        "solve": "sha256:...",
        "sensitivity": "sha256:...",
        ...
      },
      "verdict": "PASS",
      "overall_score": 82.5,
      "dimension_scores": {...},
      "reopen_cycle": 1
    }
  ]
}

使用场景:
1. Step 13 启动时检查当前论文是否已评分过(exact match)
2. 如果完全匹配,直接返回缓存结果(跳过 LLM 调用)
3. 如果部分匹配(某些章节相同),提示哪些章节已改变,缩小评分焦点
"""

import sys
import json
import hashlib
import argparse
from pathlib import Path
from datetime import datetime
from typing import Dict, Optional, Tuple

CACHE_VERSION = "1.0"

def compute_file_hash(file_path: Path) -> str:
    """计算文件 SHA256 哈希"""
    if not file_path.exists():
        return "MISSING"

    hasher = hashlib.sha256()
    with open(file_path, 'rb') as f:
        for chunk in iter(lambda: f.read(8192), b''):
            hasher.update(chunk)
    return f"sha256:{hasher.hexdigest()[:16]}"

def extract_latex_sections(tex_path: Path) -> Dict[str, str]:
    """从 LaTeX 文件中提取主要章节内容并计算哈希"""
    if not tex_path.exists():
        return {}

    content = tex_path.read_text(encoding='utf-8', errors='ignore')

    # 简单的章节分割(基于 \section 或中文标题)
    sections = {
        'abstract': '',
        'problem_restatement': '',
        'problem_analysis': '',
        'assumptions': '',
        'model': '',
        'solve': '',
        'sensitivity': '',
        'evaluation': '',
        'conclusion': ''
    }

    # 基于关键词粗略分段
    section_markers = {
        'abstract': ['摘要', 'abstract', '\\begin{abstract}'],
        'problem_restatement': ['问题重述', 'Problem Restatement'],
        'problem_analysis': ['问题分析', 'Problem Analysis'],
        'assumptions': ['模型假设', 'Assumptions', '假设'],
        'model': ['模型建立', 'Model Formulation', '模型构建'],
        'solve': ['模型求解', 'Model Solution', '求解'],
        'sensitivity': ['灵敏度', 'Sensitivity', '鲁棒性'],
        'evaluation': ['模型评价', 'Model Evaluation', '评价'],
        'conclusion': ['结论', 'Conclusion']
    }

    # 简单启发式: 找每个 section 的起始位置
    for section_name, markers in section_markers.items():
        for marker in markers:
            idx = content.lower().find(marker.lower())
            if idx >= 0:
                # 提取该段落(简化处理: 取后 500 字符作为代表)
                snippet = content[idx:idx+500]
                sections[section_name] = compute_text_hash(snippet)
                break

    return sections

def compute_text_hash(text: str) -> str:
    """计算文本内容的 SHA256 哈希"""
    hasher = hashlib.sha256()
    hasher.update(text.encode('utf-8'))
    return f"sha256:{hasher.hexdigest()[:16]}"

def load_cache(cache_path: Path) -> dict:
    """加载缓存文件"""
    if not cache_path.exists():
        return {
            "cache_version": CACHE_VERSION,
            "entries": []
        }

    try:
        with open(cache_path, 'r', encoding='utf-8') as f:
            cache = json.load(f)
            if cache.get("cache_version") != CACHE_VERSION:
                print(f"⚠️  缓存版本不匹配, 重置缓存", file=sys.stderr)
                return {"cache_version": CACHE_VERSION, "entries": []}
            return cache
    except Exception as e:
        print(f"⚠️  缓存文件损坏, 重置缓存: {e}", file=sys.stderr)
        return {"cache_version": CACHE_VERSION, "entries": []}

def save_cache(cache_path: Path, cache: dict):
    """保存缓存文件"""
    with open(cache_path, 'w', encoding='utf-8') as f:
        json.dump(cache, f, indent=2, ensure_ascii=False)

def find_cached_entry(cache: dict, paper_hash: str, sections_hash: Dict[str, str]) -> Optional[dict]:
    """查找完全匹配的缓存条目"""
    for entry in cache.get("entries", []):
        if entry.get("paper_hash") == paper_hash:
            return entry
    return None

def find_similar_entry(cache: dict, sections_hash: Dict[str, str]) -> Tuple[Optional[dict], float]:
    """查找部分匹配的缓存条目,返回(条目, 相似度)"""
    best_entry = None
    best_similarity = 0.0

    for entry in cache.get("entries", []):
        cached_sections = entry.get("paper_sections_hash", {})

        # 计算章节哈希匹配度
        matching_sections = 0
        total_sections = len(sections_hash)

        for section_name, section_hash in sections_hash.items():
            if cached_sections.get(section_name) == section_hash:
                matching_sections += 1

        similarity = matching_sections / total_sections if total_sections > 0 else 0.0

        if similarity > best_similarity and similarity >= 0.5:  # 至少 50% 相似
            best_similarity = similarity
            best_entry = entry

    return best_entry, best_similarity

def add_cache_entry(cache: dict, paper_hash: str, sections_hash: Dict[str, str],
                   verdict: str, overall_score: float, dimension_scores: dict, reopen_cycle: int):
    """添加新的缓存条目"""
    entry = {
        "timestamp": datetime.now().isoformat(),
        "paper_hash": paper_hash,
        "paper_sections_hash": sections_hash,
        "verdict": verdict,
        "overall_score": overall_score,
        "dimension_scores": dimension_scores,
        "reopen_cycle": reopen_cycle
    }

    cache.setdefault("entries", []).append(entry)

    # 只保留最近 10 个条目
    if len(cache["entries"]) > 10:
        cache["entries"] = cache["entries"][-10:]

def main():
    parser = argparse.ArgumentParser(description="Step 13 评委打分缓存工具")
    parser.add_argument("project_path", type=Path, help="项目目录路径")
    parser.add_argument("--check", action="store_true", help="检查是否有缓存命中")
    parser.add_argument("--save", action="store_true", help="保存评分结果到缓存")
    parser.add_argument("--verdict", help="评分结论(保存时使用)")
    parser.add_argument("--score", type=float, help="总分(保存时使用)")
    parser.add_argument("--reopen-cycle", type=int, default=1, help="reopen 轮次")
    args = parser.parse_args()

    project_path = args.project_path.resolve()
    cache_path = project_path / ".step13_cache.json"

    # 查找论文文件
    base_name = project_path.name
    paper_tex = project_path / f"{base_name}_paper.tex"
    if not paper_tex.exists():
        paper_tex = project_path / "paper" / "paper.tex"

    if not paper_tex.exists():
        print(f"❌ 论文文件不存在: {paper_tex}", file=sys.stderr)
        sys.exit(2)

    # 计算论文哈希
    paper_hash = compute_file_hash(paper_tex)
    sections_hash = extract_latex_sections(paper_tex)

    cache = load_cache(cache_path)

    if args.check:
        # 检查模式: 查找缓存命中
        print(f"检查缓存: {cache_path}")
        print(f"论文哈希: {paper_hash}")

        # 精确匹配
        exact_entry = find_cached_entry(cache, paper_hash, sections_hash)
        if exact_entry:
            print(f"\n✅ 精确匹配缓存(时间: {exact_entry['timestamp']})")
            print(f"  Verdict: {exact_entry['verdict']}")
            print(f"  Overall Score: {exact_entry['overall_score']}")
            print(f"  Reopen Cycle: {exact_entry['reopen_cycle']}")
            print(f"\n缓存结果可直接复用, 无需重新评分")
            sys.exit(0)

        # 部分匹配
        similar_entry, similarity = find_similar_entry(cache, sections_hash)
        if similar_entry:
            print(f"\n⚠️  部分匹配缓存(相似度: {similarity:.0%}, 时间: {similar_entry['timestamp']})")
            print(f"  上次 Verdict: {similar_entry['verdict']}")
            print(f"  上次 Score: {similar_entry['overall_score']}")

            # 对比章节变化
            cached_sections = similar_entry.get("paper_sections_hash", {})
            changed_sections = []
            for section_name, section_hash in sections_hash.items():
                if cached_sections.get(section_name) != section_hash:
                    changed_sections.append(section_name)

            if changed_sections:
                print(f"\n  变化的章节: {', '.join(changed_sections)}")
                print(f"  建议: 重点评审这些章节, 其他章节可参考缓存评分")

            sys.exit(1)

        print(f"\n❌ 无缓存命中, 需要完整评分")
        sys.exit(2)

    elif args.save:
        # 保存模式
        if not args.verdict or args.score is None:
            print(f"❌ 保存模式需要 --verdict 和 --score 参数", file=sys.stderr)
            sys.exit(1)

        # 简化的维度评分(从 judge_evaluation.md 解析更好, 这里用占位)
        dimension_scores = {
            "model_rationality": args.score * 0.2,
            "solution_correctness": args.score * 0.2,
            "innovation": args.score * 0.2,
            "writing_clarity": args.score * 0.15,
            "result_persuasiveness": args.score * 0.15,
            "sensitivity_analysis": args.score * 0.1
        }

        add_cache_entry(cache, paper_hash, sections_hash, args.verdict,
                       args.score, dimension_scores, args.reopen_cycle)
        save_cache(cache_path, cache)

        print(f"✅ 缓存已保存: {cache_path}")
        print(f"  Verdict: {args.verdict}, Score: {args.score}")
        sys.exit(0)

    else:
        parser.print_help()
        sys.exit(1)

if __name__ == "__main__":
    main()
