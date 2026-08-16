#!/usr/bin/env bash
set -euo pipefail

PROJECT="$(realpath "${1:?Usage: $0 <project_dir> <base_name>}")"
BASE="${2:?Usage: $0 <project_dir> <base_name>}"

# Make factory-vendored classes/styles (cumcmthesis.cls, abstract_placeholder.sty)
# resolvable from any project directory. Trailing colon keeps default paths.
FACTORY_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if ! mapfile -t LATEX_CONTRACT < <(
    python3 "$FACTORY_DIR/scripts/latex_dependency_guard.py" \
        "$PROJECT" "$BASE" --contract-lines
); then
    echo "❌ 编译失败：LaTeX 依赖合同不完整" >&2
    exit 1
fi
if [[ "${#LATEX_CONTRACT[@]}" -lt 5 ]]; then
    echo "❌ 编译失败：无法读取 LaTeX 编译合同" >&2
    exit 1
fi
TEX_SOURCE="${LATEX_CONTRACT[0]}"
ENGINE="${LATEX_CONTRACT[1]}"
JOB_NAME="${LATEX_CONTRACT[2]}"
BIB_BACKEND="${LATEX_CONTRACT[3]}"
SEARCH_PATHS=()
for relative in "${LATEX_CONTRACT[@]:4}"; do
    if [[ "$relative" == "." ]]; then
        SEARCH_PATHS+=("$PROJECT")
    else
        SEARCH_PATHS+=("$PROJECT/$relative")
    fi
done
SEARCH_PATHS+=("$FACTORY_DIR/latex_templates")
JOINED_SEARCH_PATHS="$(IFS=:; echo "${SEARCH_PATHS[*]}")"

# Do not inherit user-controlled TeX/BibTeX search paths.  The trailing colon
# retains only the system kpathsea defaults after our fixed project/template
# roots.  User TEXMF trees are replaced by an empty, project-contained runtime.
unset TEXINPUTS BIBINPUTS BSTINPUTS TEXMFCNF TEXMF TEXMFDBS TEXMFHOME \
    TEXMFCONFIG TEXMFVAR VARTEXFONTS LUAINPUTS MFINPUTS MPINPUTS \
    T1FONTS TFMFONTS VFFONTS OPENTYPEFONTS TTFONTS FONTCONFIG_FILE \
    FONTCONFIG_PATH BIBER_CACHE
TEX_RUNTIME="$PROJECT/.factory/tex-runtime"
mkdir -p "$TEX_RUNTIME/home" "$TEX_RUNTIME/config" "$TEX_RUNTIME/var"
export TEXINPUTS="${JOINED_SEARCH_PATHS}:"
export BIBINPUTS="${JOINED_SEARCH_PATHS}:"
export BSTINPUTS="${JOINED_SEARCH_PATHS}:"
export TEXMFHOME="$TEX_RUNTIME/home"
export TEXMFCONFIG="$TEX_RUNTIME/config"
export TEXMFVAR="$TEX_RUNTIME/var"
export openin_any=p
export openout_any=p

cd "$PROJECT"

# 创建编译日志目录
mkdir -p logs/compilation

# Remove only this job's stale compiler intermediates.  In particular, an old
# .bbl must never survive a failed bibliography backend and enter a new PDF.
for suffix in aux bbl bcf run.xml blg toc out fls fdb_latexmk synctex.gz; do
    rm -f -- "$PROJECT/${JOB_NAME}.${suffix}"
done

ENGINE_ARGS=(
    -recorder
    -no-shell-escape
    -file-line-error
    -halt-on-error
    -interaction=nonstopmode
    "-jobname=$JOB_NAME"
)

capture_recorder() {
    local pass="$1"
    if [[ ! -f "$PROJECT/${JOB_NAME}.fls" ]]; then
        echo "❌ 编译失败：第 ${pass} 轮缺少 recorder 文件" >&2
        exit 1
    fi
    cp -- "$PROJECT/${JOB_NAME}.fls" "$PROJECT/logs/compilation/pass${pass}.fls"
}

echo "$(date '+%Y-%m-%d %H:%M:%S') - 使用编译引擎: $ENGINE" >> logs/compilation/compile.log

# 第一次编译（生成 .aux）
if ! "$ENGINE" "${ENGINE_ARGS[@]}" "$TEX_SOURCE" > logs/compilation/pass1.log 2>&1; then
    echo "❌ 编译失败：第一次 $ENGINE 编译出错" >&2
    echo "" >&2
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━" >&2
    echo "📋 错误诊断（提取自 ${BASE}_paper.log）：" >&2
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━" >&2

    # 提取关键错误信息
    if [[ -f "${BASE}_paper.log" ]]; then
        # 提取第一个错误（! 开头）及其上下文
        awk '/^!/{found=1} found{print; if(/^l\.[0-9]+/){print ""; exit}}' "${BASE}_paper.log" | head -20 >&2
        echo "" >&2

        # 常见错误模式诊断
        if grep -q "Undefined control sequence" "${BASE}_paper.log"; then
            echo "🔍 诊断：未定义的控制序列（可能缺少宏包或拼写错误）" >&2
            grep -A2 "Undefined control sequence" "${BASE}_paper.log" | head -5 >&2
        fi

        if grep -q "Missing \\$ inserted" "${BASE}_paper.log"; then
            echo "🔍 诊断：数学模式错误（可能缺少 $ 或 _ 未转义）" >&2
            grep -B1 -A2 "Missing \\$ inserted" "${BASE}_paper.log" | head -8 >&2
        fi

        if grep -q "File.*not found" "${BASE}_paper.log"; then
            echo "🔍 诊断：缺失文件" >&2
            grep "File.*not found" "${BASE}_paper.log" | head -5 >&2
        fi

        if grep -q "! Package" "${BASE}_paper.log"; then
            echo "🔍 诊断：宏包错误" >&2
            grep "! Package" "${BASE}_paper.log" | head -5 >&2
        fi

        echo "" >&2
        echo "💡 完整日志位置: $(pwd)/logs/compilation/pass1.log" >&2
        echo "💡 LaTeX 日志: $(pwd)/${BASE}_paper.log" >&2
    else
        echo "⚠️  无法找到 ${BASE}_paper.log 文件" >&2
        echo "💡 完整输出: $(pwd)/logs/compilation/pass1.log" >&2
    fi
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━" >&2
    exit 1
fi
capture_recorder 1

# Preserve the exact control file consumed by the bibliography backend.
if [[ -f "${JOB_NAME}.aux" ]]; then
    cp -- "${JOB_NAME}.aux" logs/compilation/pass1.aux
fi
if [[ -f "${JOB_NAME}.bcf" ]]; then
    cp -- "${JOB_NAME}.bcf" logs/compilation/pass1.bcf
fi

BIB_BACKEND_VERSION=""
case "$BIB_BACKEND" in
    bibtex)
        if ! command -v bibtex >/dev/null 2>&1; then
            echo "❌ 编译失败：论文需要 BibTeX，但 bibtex 不可用" >&2
            exit 1
        fi
        BIB_BACKEND_VERSION="$( { bibtex --version 2>/dev/null || true; } | head -1)"
        if ! bibtex "$JOB_NAME" > logs/compilation/bibliography_backend.log 2>&1; then
            echo "❌ 编译失败：BibTeX 执行失败" >&2
            tail -20 logs/compilation/bibliography_backend.log >&2
            exit 1
        fi
        ;;
    biber)
        if ! command -v biber >/dev/null 2>&1; then
            echo "❌ 编译失败：论文需要 Biber，但 biber 不可用" >&2
            exit 1
        fi
        BIB_BACKEND_VERSION="$( { biber --version 2>/dev/null || true; } | head -1)"
        if ! biber "$JOB_NAME" > logs/compilation/bibliography_backend.log 2>&1; then
            echo "❌ 编译失败：Biber 执行失败" >&2
            tail -20 logs/compilation/bibliography_backend.log >&2
            exit 1
        fi
        ;;
    none)
        : > logs/compilation/bibliography_backend.log
        ;;
    *)
        echo "❌ 编译失败：未知 bibliography backend: $BIB_BACKEND" >&2
        exit 1
        ;;
esac

# 第二次编译（处理引用）
if ! "$ENGINE" "${ENGINE_ARGS[@]}" "$TEX_SOURCE" > logs/compilation/pass2.log 2>&1; then
    echo "❌ 编译失败：第二次 $ENGINE 编译出错" >&2
    echo "💡 日志: $(pwd)/logs/compilation/pass2.log" >&2
    exit 1
fi
capture_recorder 2

# 第三次编译（最终化）
if ! "$ENGINE" "${ENGINE_ARGS[@]}" "$TEX_SOURCE" > logs/compilation/pass3.log 2>&1; then
    echo "❌ 编译失败：第三次 $ENGINE 编译出错" >&2
    echo "💡 日志: $(pwd)/logs/compilation/pass3.log" >&2
    exit 1
fi
capture_recorder 3

if ! python3 "$FACTORY_DIR/scripts/bibliography_build_guard.py" \
    "$PROJECT" "$BASE" \
    --backend "$BIB_BACKEND" \
    --backend-version "$BIB_BACKEND_VERSION" \
    --backend-log "$PROJECT/logs/compilation/bibliography_backend.log" \
    --final-log "$PROJECT/logs/compilation/pass3.log"; then
    echo "❌ 编译失败：bibliography 构建证据不完整或存在未解析引用" >&2
    exit 1
fi

if ! python3 "$FACTORY_DIR/scripts/latex_dependency_guard.py" \
    "$PROJECT" "$BASE" \
    --fls "$PROJECT/logs/compilation/pass1.fls" \
    --fls "$PROJECT/logs/compilation/pass2.fls" \
    --fls "$PROJECT/logs/compilation/pass3.fls" \
    --allowed-runtime-root "$FACTORY_DIR/latex_templates" \
    --output "$PROJECT/logs/compilation/latex_inputs.json"; then
    echo "❌ 编译失败：实际读取的项目文件与已冻结 LaTeX 依赖不一致" >&2
    exit 1
fi

# 验证 PDF 生成
if [[ ! -f "${BASE}_paper.pdf" ]]; then
    echo "❌ 编译失败：PDF 未生成" >&2
    echo "💡 检查日志: $(pwd)/logs/compilation/" >&2
    exit 1
fi

# 成功信息
PDF_SIZE=$(du -h "${BASE}_paper.pdf" | cut -f1)
PDF_PAGES=$(pdfinfo "${BASE}_paper.pdf" 2>/dev/null | grep "Pages:" | awk '{print $2}' || echo "unknown")
echo "✅ 编译成功: ${BASE}_paper.pdf (${PDF_SIZE}, ${PDF_PAGES} 页)" >> logs/compilation/compile.log
echo "✅ PDF 已生成: ${BASE}_paper.pdf (${PDF_SIZE}, ${PDF_PAGES} 页)"
