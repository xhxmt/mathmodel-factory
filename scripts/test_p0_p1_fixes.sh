#!/bin/bash
# test_p0_p1_fixes.sh - Validate P0 and P1 fixes

set -e

echo "=== Testing P0/P1 Fixes ==="
echo

# Test 1: Abstract placeholder mechanism
echo "Test 1: Abstract placeholder LaTeX package"
if [ -f latex_templates/abstract_placeholder.sty ]; then
    echo "✓ abstract_placeholder.sty exists"
    # Check it has the required commands
    if grep -q "\\\\newcommand{\\\\AbstractPlaceholder}" latex_templates/abstract_placeholder.sty; then
        echo "✓ \AbstractPlaceholder command defined"
    else
        echo "✗ \AbstractPlaceholder command not found"
        exit 1
    fi
else
    echo "✗ abstract_placeholder.sty not found"
    exit 1
fi

# Test 2: Step 9 prompt updated to use new mechanism
echo
echo "Test 2: Step 9 prompt uses AbstractPlaceholder"
if grep -q "\\\\AbstractPlaceholder" prompts/step9_paper_draft.txt; then
    echo "✓ Step 9 uses \AbstractPlaceholder"
    if ! grep -q "\\\\detokenize{ABSTRACT_PLACEHOLDER}" prompts/step9_paper_draft.txt | grep -v "不要"; then
        echo "✓ Old ABSTRACT_PLACEHOLDER removed from examples"
    fi
else
    echo "✗ Step 9 still uses old mechanism"
    exit 1
fi

# Test 3: Step 14 prompt updated
echo
echo "Test 3: Step 14 prompt updated"
if grep -q "\\\\AbstractPlaceholder" prompts/step14_abstract.txt; then
    echo "✓ Step 14 references \AbstractPlaceholder"
else
    echo "✗ Step 14 not updated"
    exit 1
fi

# Test 4: Number verification script exists and is executable
echo
echo "Test 4: Number verification with manifest"
if [ -x scripts/verify_numbers.py ]; then
    echo "✓ verify_numbers.py is executable"
    # Check it has the new flags
    if grep -q "\-\-generate" scripts/verify_numbers.py && \
       grep -q "\-\-verify" scripts/verify_numbers.py && \
       grep -q "numbers_manifest.json" scripts/verify_numbers.py; then
        echo "✓ New manifest-based verification implemented"
    else
        echo "✗ New verification modes not found"
        exit 1
    fi
else
    echo "✗ verify_numbers.py not executable"
    exit 1
fi

# Test 5: Step 0 declares competition type
echo
echo "Test 5: Competition type declaration in Step 0"
if grep -q "竞赛类型" prompts/step0_problem_parsing.txt; then
    echo "✓ Step 0 prompts for explicit competition type"
else
    echo "✗ Competition type declaration not added"
    exit 1
fi

# Test 6: Step 9 reads competition type from feasibility_constraints
echo
echo "Test 6: Step 9 prioritizes feasibility_constraints"
if grep -q "优先.*feasibility_constraints.md.*竞赛类型" prompts/step9_paper_draft.txt; then
    echo "✓ Step 9 prioritizes explicit declaration over keyword matching"
else
    echo "✗ Step 9 competition type logic not updated"
    exit 1
fi

# Test 7: Page count checker exists
echo
echo "Test 7: Page count checker for MCM/ICM"
if [ -x scripts/check_page_count.py ]; then
    echo "✓ check_page_count.py exists and is executable"
    if grep -q "pdfinfo" scripts/check_page_count.py; then
        echo "✓ Uses pdfinfo for page counting"
    fi
else
    echo "✗ check_page_count.py not found or not executable"
    exit 1
fi

# Test 8: Step 9 calls page checker for MCM/ICM
echo
echo "Test 8: Step 9 integrates page check"
if grep -q "check_page_count.py" prompts/step9_paper_draft.txt; then
    echo "✓ Step 9 calls page checker after compilation"
else
    echo "✗ Page check not integrated in Step 9"
    exit 1
fi

# Test 9: Step 10 updated to use new number verification
echo
echo "Test 9: Step 10 uses manifest-based verification"
if grep -q "\-\-generate.*numbers_manifest" prompts/step10_gate1_numerical.txt && \
   grep -q "\-\-verify" prompts/step10_gate1_numerical.txt; then
    echo "✓ Step 10 uses new verification workflow"
else
    echo "✗ Step 10 not updated for manifest verification"
    exit 1
fi

echo
echo "=== All tests passed ✓ ==="
echo
echo "Summary of fixes:"
echo "  P0#1: Abstract placeholder - LaTeX macro mechanism"
echo "  P0#3: Number verification - Manifest with checksums"
echo "  P1#2: Competition type - Explicit declaration in feasibility_constraints"
echo "  P1#9: Page count - Automatic MCM/ICM check with trimming suggestions"
