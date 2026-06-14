#!/usr/bin/env bash
# 统计 2024B 完整 16 步运行的 token 消耗（按来源拆分）
# 用法:  ! bash ~/paper_factory/token_cost_2024b.sh
# 一次性脚本，看完可删:  rm ~/paper_factory/token_cost_2024b.sh
P=/home/tfisher/paper_factory/complete/test_cumcm2024b
cd "$P" || { echo "no project $P"; exit 1; }

echo "########## A. 各日志里的 Codex 'tokens used' 数值 ##########"
echo "(每个 codex exec 结束打印一次该步总 tokens; 列出每文件出现的全部数值)"
allsum=0; lastsum=0; nlogs=0
for f in $(find logs -name '*.log' 2>/dev/null | sort); do
  vals=$(grep -A1 "tokens used" "$f" 2>/dev/null | grep -E '^[0-9,]+$' | tr -d ',')
  if [ -n "$vals" ]; then
    list=$(echo "$vals" | paste -sd, )
    last=$(echo "$vals" | tail -1)
    fsum=$(echo "$vals" | awk '{s+=$1} END{print s}')
    printf '  %-44s [%s]\n' "$(basename "$f")" "$list"
    allsum=$((allsum + fsum))
    lastsum=$((lastsum + last))
    nlogs=$((nlogs + 1))
  fi
done
echo "  ------------------------------------------------------------"
echo "  含 token 的日志数: $nlogs"
echo "  合计(每文件取最后一个值相加) = $lastsum"
echo "  合计(所有出现值相加)         = $allsum"
echo

echo "########## B. 每步用了哪个 agent (runner.log) ##########"
grep -aiE "run_codex|run_claude|run_agy|fallback|agy|claude|codex.*(completed|failed|hung)|setup complete|all 16 steps" logs/runner.log 2>/dev/null \
  | sed 's#^#  #' | head -90
echo

echo "########## C. 非 Codex(Claude/Agy) 是否在日志里留了 token 标记 ##########"
hits=$(grep -rhinE "input_tokens|output_tokens|prompt_tokens|completion_tokens|total_tokens" logs 2>/dev/null | head -20)
if [ -n "$hits" ]; then echo "$hits" | sed 's#^#  #'; else echo "  (无 => Claude/Agy 步骤未把 token 写进项目日志)"; fi
echo

echo "########## D. 运行规模(起止时间 + 步数) ##########"
echo -n "  起: "; sed -n '1{p};2{p}' logs/runner.log 2>/dev/null | grep -oE '^[0-9-]+ [0-9:]+' | head -1
echo -n "  止: "; grep -E "All 16 steps complete|Moved project" logs/runner.log 2>/dev/null | tail -1 | grep -oE '^[0-9-]+ [0-9:]+'
echo -n "  日志文件总数: "; find logs -name '*.log' 2>/dev/null | wc -l
echo -n "  codex 会话快照(若有): "; ls -1 logs/runner_snapshots 2>/dev/null | wc -l
echo
echo "提示: 若想要 Codex 的 输入/输出/缓存 拆分(更精确按 API 计价), 看:"
echo "  ls -t ~/.codex/sessions/**/*.jsonl 2>/dev/null | head"
