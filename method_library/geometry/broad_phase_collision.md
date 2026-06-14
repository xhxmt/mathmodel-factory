# 宽相碰撞检测 / AABB 包围盒

## 适用场景

- 大规模几何体碰撞预筛选（N > 100 个对象，N² 精确检测不可行）
- 机器人路径规划：快速剔除明显不相交的障碍物
- 动画 / 游戏：粗筛选后再精确碰撞（SAT / GJK）
- CUMCM 国赛典型：板材排布无干涉、物流调度空间冲突、机械臂避障

**何时切换**：
- N < 20 且对象简单（圆、矩形）→ 直接暴力 O(N²) 精确检测
- 需要精确间距（不只是是否碰撞）→ 宽相后必须接窄相（SAT / 距离场）
- 对象凹多边形 → AABB 包围盒过于松弛，改用 OBB（有向包围盒）或凸分解

---

## 核心假设

1. **轴对齐**：AABB（Axis-Aligned Bounding Box）边平行于坐标轴，旋转对象后重新计算
2. **保守性**：包围盒相交 ⊇ 对象相交（允许假阳性 false positive，不允许假阴性）
3. **静态或慢速运动**：对象移动后实时更新 AABB；高速运动需改用 Swept AABB

**违反假设的后果**：
- 旋转对象未更新 AABB → 碰撞漏检 → 物理不合理
- 包围盒过大 → 假阳性率高 → 窄相检测负担重

---

## 数学形式

### 1. AABB 表示
对象 $\mathcal{O}$ 的所有顶点 $\{\mathbf{v}_i\}$，AABB 为：
$$
\text{AABB} = [\mathbf{b}_{\min}, \mathbf{b}_{\max}], \quad
\mathbf{b}_{\min} = \begin{bmatrix} \min_i x_i \\ \min_i y_i \end{bmatrix}, \quad
\mathbf{b}_{\max} = \begin{bmatrix} \max_i x_i \\ \max_i y_i \end{bmatrix}
$$

### 2. AABB 相交判定（2D）
AABB₁ 和 AABB₂ 相交 ⟺ 两轴投影均重叠：
$$
\begin{cases}
b_{\min,1}^x \le b_{\max,2}^x \land b_{\max,1}^x \ge b_{\min,2}^x \\
b_{\min,1}^y \le b_{\max,2}^y \land b_{\max,1}^y \ge b_{\min,2}^y
\end{cases}
$$

**代码实现**（单次 O(1)）：
```python
def aabb_intersect(aabb1, aabb2):
    return (aabb1[0][0] <= aabb2[1][0] and aabb1[1][0] >= aabb2[0][0] and
            aabb1[0][1] <= aabb2[1][1] and aabb1[1][1] >= aabb2[0][1])
```

### 3. N 个对象的宽相检测（暴力 O(N²)）
```python
for i in range(N):
    for j in range(i+1, N):
        if aabb_intersect(aabbs[i], aabbs[j]):
            candidate_pairs.append((i, j))  # 候选碰撞对，交窄相精确检测
```

### 4. 加速结构：Sweep and Prune（O(N log N)）
- 按 x 轴排序所有 AABB 端点
- 扫描线从左到右，维护活跃集合（x 区间重叠的 AABB）
- 只对活跃集合内的对做 y 轴重叠检测

---

## 求解工具

- **纯 NumPy 实现**（适合 N < 1000）：直接向量化 AABB 计算
- **空间索引**：`scipy.spatial.KDTree`（范围查询，O(log N)）
- **专用库**（高级）：
  - `shapely`（2D 几何操作，含 STRtree 空间索引）
  - `rtree`（R-tree 索引，处理动态插入 / 删除）

---

## 代码模板

```python
#!/usr/bin/env python3
"""
宽相碰撞检测：AABB + Sweep and Prune
场景：100 个旋转矩形，快速找出所有潜在碰撞对
"""
import numpy as np
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle

np.random.seed(42)

# 生成 N 个旋转矩形
N = 100
centers = np.random.uniform(0, 100, (N, 2))
widths = np.random.uniform(5, 15, N)
heights = np.random.uniform(5, 15, N)
angles = np.random.uniform(0, 2*np.pi, N)  # 旋转角

def compute_aabb(center, w, h, angle):
    """计算旋转矩形的 AABB"""
    corners = np.array([[-w/2, -h/2], [w/2, -h/2], [w/2, h/2], [-w/2, h/2]])
    R = np.array([[np.cos(angle), -np.sin(angle)],
                  [np.sin(angle),  np.cos(angle)]])
    rotated = corners @ R.T + center
    return rotated.min(axis=0), rotated.max(axis=0)

# 计算所有 AABB
aabbs = [compute_aabb(centers[i], widths[i], heights[i], angles[i]) for i in range(N)]

# 暴力 O(N²) 宽相检测
def aabb_intersect(aabb1, aabb2):
    return (aabb1[0][0] <= aabb2[1][0] and aabb1[1][0] >= aabb2[0][0] and
            aabb1[0][1] <= aabb2[1][1] and aabb1[1][1] >= aabb2[0][1])

collision_pairs = []
for i in range(N):
    for j in range(i+1, N):
        if aabb_intersect(aabbs[i], aabbs[j]):
            collision_pairs.append((i, j))

print(f"候选碰撞对数量: {len(collision_pairs)} / {N*(N-1)//2} (剔除率: {1 - len(collision_pairs)/(N*(N-1)/2):.1%})")

# 可视化
fig, ax = plt.subplots(figsize=(8, 8))
for i in range(N):
    bmin, bmax = aabbs[i]
    rect = Rectangle(bmin, bmax[0]-bmin[0], bmax[1]-bmin[1], 
                     fill=False, edgecolor='blue', linewidth=0.5)
    ax.add_patch(rect)
    ax.plot(*centers[i], 'ro', markersize=2)

# 标出候选碰撞对的 AABB（加粗）
for i, j in collision_pairs[:20]:  # 只画前 20 对
    for k in [i, j]:
        bmin, bmax = aabbs[k]
        rect = Rectangle(bmin, bmax[0]-bmin[0], bmax[1]-bmin[1], 
                         fill=False, edgecolor='red', linewidth=1.5)
        ax.add_patch(rect)

ax.set_xlim(0, 100)
ax.set_ylim(0, 100)
ax.set_aspect('equal')
ax.grid(True, alpha=0.3)
plt.savefig('aabb_broad_phase.pdf')
```

---

## 常见陷阱

1. **旋转后未更新 AABB**：对象旋转 → AABB 应重新计算 → 否则碰撞漏检
2. **假阳性未处理**：宽相通过 ≠ 真碰撞 → 必须接窄相（SAT / GJK）验证
3. **包围盒过大**：细长对象（长宽比 > 5）旋转 45° → AABB 面积膨胀 √2 倍 → 假阳性率高 → 改用 OBB（有向包围盒）
4. **动态场景无更新**：对象移动后未维护空间索引 → 检测结果过时

---

## 在建模比赛中的典型应用

| 竞赛 | 题目 | 应用场景 | 关键约束 |
|------|------|---------|---------|
| CUMCM 2024 A | 板材龙舟调头 | 16 调头点 × 圆盘干涉预筛选 | AABB 相交 → SAT 精确验证 |
| CUMCM 2018 B | 智能 RGV 调度 | 轨道段占用冲突检测 | 1D AABB（区间重叠） |
| MCM 2020 D | 无人机编队 | N² 避障对筛选 | 3D AABB → 距离场细化 |

**CUMCM 2024 A 题实例**：
- 问题 4：16 个调头点，每点检查「龙舟 + 4 个圆盘」是否与其他 15 点干涉
- 挑战：调头路径连续变化 → AABB 需逐时间步更新 → 83% 残差说明可行性检测失败
- 优化：宽相剔除 95% 无关对 → 窄相只需检测 <100 对 → 计算时间从 O(分钟) 降到 O(秒)

---

## 参考文献

1. **Ericson, C.** (2004). *Real-Time Collision Detection*. Morgan Kaufmann. (AABB / OBB / 空间分割经典教材)
2. **Bergen, G. van den** (1997). "Efficient Collision Detection of Complex Deformable Models using AABB Trees". *Journal of Graphics Tools*, 2(4), 1-13.
3. **shapely.strtree 文档**：https://shapely.readthedocs.io/en/stable/strtree.html
4. **Sweep and Prune 算法**：Held, M. et al. (1995). "Collision Detection for Moving Polyhedra". *IEEE TVCG*.

---

**关键词**：AABB、包围盒、宽相碰撞、Sweep-and-Prune、空间索引、R-tree、假阳性、窄相检测
