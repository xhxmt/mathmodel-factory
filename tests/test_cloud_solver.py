#!/usr/bin/env python3
"""
测试 Cloud Run Solver 的简单脚本
计算斐波那契数列（模拟中等复杂度的计算任务）
"""
import time
import sys

def fibonacci(n):
    """计算第 n 个斐波那契数"""
    if n <= 1:
        return n
    a, b = 0, 1
    for _ in range(2, n + 1):
        a, b = b, a + b
    return b

def main():
    print("Starting fibonacci calculation...")
    start = time.time()

    # 计算前 100000 个斐波那契数
    results = []
    for i in range(100000):
        if i % 10000 == 0:
            print(f"Progress: {i}/100000")
        results.append(fibonacci(i % 50))  # 控制数字大小

    end = time.time()
    duration = end - start

    print(f"\nCompleted!")
    print(f"Duration: {duration:.2f} seconds")
    print(f"Computed {len(results)} values")
    print(f"Sample results: {results[:10]}")

    # 输出结果到文件
    with open("fibonacci_results.txt", "w") as f:
        f.write(f"Duration: {duration:.2f}s\n")
        f.write(f"Total: {len(results)}\n")
        f.write(f"Sample: {results[:20]}\n")

if __name__ == "__main__":
    main()
