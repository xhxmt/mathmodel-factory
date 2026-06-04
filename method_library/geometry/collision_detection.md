# 碰撞 / 干涉检测 (Collision Detection)

## 适用场景

- 多刚体运动 / 布局问题中的**互不重叠**约束：板凳、车辆、面板、线缆不能交叉
- 平面**有向矩形 / 凸多边形**之间的相交判定
- 把"不碰撞"写成一组可计算的几何不等式 $g_k(\mathbf x)\le 0$，供可行性判断或优化约束
- 终止时刻 / 临界间距的事件定位（配合根查找 [[root_finding]]）

不适用：
- 圆形 / 球形物体（直接用圆心距 $\ge r_i+r_j$，无需 SAT）
- 连续介质形变 / 接触力学（本文只判"是否相交"，不算接触力）
- 三维一般凸体（本文给 2D；3D 需 GJK，竞赛少见）

## 核心假设

1. 物体可近似为**凸**多边形（凹体先做凸分解）
2. 平面问题（2D）；刚体在每个时刻位姿已知
3. 相邻 / 铰接物体在共用连接点处允许接触，不计为碰撞（**邻接豁免**）
4. 关心的碰撞对可枚举（或用 AABB / 空间哈希预筛后枚举）

## 数学形式

**圆形物体**（最简）：$ (x_i-x_j)^2+(y_i-y_j)^2 \ge (r_i+r_j)^2 $。

**分离轴定理 (SAT)**：两个凸多边形**不相交**当且仅当存在一条轴，使两者在该轴上的投影区间不重叠。对凸多边形，候选轴 = 各边的外法向量（矩形只需 4 条，平行边合并后 2+2）。

设多边形顶点集投影到单位轴 $\hat{\mathbf a}$ 上得到区间 $[\min_i \mathbf v_i\!\cdot\!\hat{\mathbf a},\ \max_i \mathbf v_i\!\cdot\!\hat{\mathbf a}]$；若**任一**候选轴上两区间不重叠 → 不相交。

**AABB 预筛**：先比较两物体的轴对齐包围盒，不重叠则直接判不相交（$O(1)$ 快速否决，避免对所有对做 SAT）。

## 求解工具

- `numpy` — 顶点 / 投影 / 法向量向量化
- `scipy.spatial`（可选）— `ConvexHull`、`cKDTree`（大量物体的邻近预筛）
- 自实现 SAT（几十行，竞赛首选，完全可控）

## 代码模板

```python
"""
collision_detection_template.py — 有向矩形(板凳)的 SAT 相交判定 + AABB 预筛 + 邻接豁免
用法: python collision_detection_template.py   # 或 solver_submit.sh --type python
"""
import json
import os
import numpy as np

np.random.seed(42)

def rect_corners(p1, p2, width, end_ext=0.0):
    """由两孔中心 p1->p2 构造矩形板的 4 个角点。
    width: 板宽; end_ext: 孔中心外延到板端的距离（板比孔距长）。"""
    p1, p2 = np.asarray(p1, float), np.asarray(p2, float)
    axis = p2 - p1
    L = np.linalg.norm(axis)
    u = axis / L                      # 长度方向单位向量
    n = np.array([-u[1], u[0]])       # 法向（宽度方向）
    a = p1 - u * end_ext              # 沿长度外延
    b = p2 + u * end_ext
    hw = width / 2.0
    return np.array([a + n*hw, a - n*hw, b - n*hw, b + n*hw])

def _project(poly, axis):
    d = poly @ axis
    return d.min(), d.max()

def _axes(poly):
    edges = np.roll(poly, -1, axis=0) - poly
    axs = np.stack([-edges[:, 1], edges[:, 0]], axis=1)   # 边法向
    norms = np.linalg.norm(axs, axis=1, keepdims=True)
    return axs / np.clip(norms, 1e-12, None)

def aabb_overlap(A, B):
    return (A[:,0].max() >= B[:,0].min() and B[:,0].max() >= A[:,0].min() and
            A[:,1].max() >= B[:,1].min() and B[:,1].max() >= A[:,1].min())

def rects_intersect(A, B, eps=1e-9):
    """SAT：True=相交(重叠)。先 AABB 预筛。"""
    if not aabb_overlap(A, B):
        return False
    for axis in np.vstack([_axes(A), _axes(B)]):
        amin, amax = _project(A, axis)
        bmin, bmax = _project(B, axis)
        if amin > bmax + eps or bmin > amax + eps:   # 找到分离轴
            return False
    return True

# ---- 演示：两块板凳；邻接(共享把手)对豁免 ----
W = 0.30
boardA = rect_corners([0, 0], [1.65, 0], W, end_ext=0.275)
boardB_far = rect_corners([0, 1.0], [1.65, 1.0], W, end_ext=0.275)   # 平行远离
boardB_hit = rect_corners([0.8, 0.1], [2.0, 0.6], W, end_ext=0.275)  # 斜插相交

print("A vs 远离板:", rects_intersect(boardA, boardB_far), "(期望 False)")
print("A vs 斜插板:", rects_intersect(boardA, boardB_hit), "(期望 True)")

# 邻接豁免：链条上 |i-j|<=1 的板共享铰接点，跳过
def any_collision(boards, exempt_adjacent=True):
    n = len(boards)
    hits = []
    for i in range(n):
        for j in range(i + 1, n):
            if exempt_adjacent and j - i == 1:
                continue
            if rects_intersect(boards[i], boards[j]):
                hits.append((i, j))
    return hits

boards = [boardA, boardB_far, boardB_hit]
hits = any_collision(boards)
print("碰撞对:", hits)

os.makedirs("results", exist_ok=True)
with open("results/collision_demo.json", "w") as f:
    json.dump({"collision_pairs": hits, "width": W}, f, ensure_ascii=False, indent=2)
print("written results/collision_demo.json")
```

## 常见陷阱

1. **跳过 AABB 预筛**：$N$ 个物体两两 SAT 是 $O(N^2)$ 且常数大；先 AABB / KD-tree 预筛能数量级加速。
2. **忘记邻接豁免**：铰接链中相邻物体共享连接点，必然"接触"，必须按 $|i-j|\le1$ 豁免，否则全判碰撞。
3. **凹多边形直接套 SAT**：SAT 只对凸多边形成立；凹体须先凸分解。
4. **矩形只取孔中心连线**：板凳实体比孔距长（孔到板端有外延 0.275 m）且有板宽 0.30 m；碰撞用**实体矩形**，不是孔中心线段。
5. **浮点边界误判**：投影重叠判断要带 `eps`，否则擦边接触时 True/False 抖动。
6. **法向量未归一化**：投影区间比较本身不要求归一化，但与阈值/裕度比较时必须归一化，否则裕度量纲错。
7. **把"最小间距"当布尔**：优化时常需要**带符号间距**（穿透深度 / 最近距离）作为连续约束 $g_k\le0$，纯布尔不可微、不利于根查找定位临界时刻。
8. **只测某些对**：多体问题要枚举所有非豁免对（或空间哈希），漏测会假阳性"无碰撞"。
9. **时间离散过粗漏穿透**：快速运动下相邻时刻间可能"穿过"；用更细步长或对最近距离做根查找定位首次接触时刻。
10. **三维误用 2D**：本文是平面 SAT；3D 凸体相交要用 GJK / 分离轴的 3D 版本。

## 在建模比赛中的典型应用

- **CUMCM 2024A 板凳龙**：问题 2/3 判定盘入过程中非相邻板凳是否相交，用实体矩形 + SAT + 邻接豁免；终止时刻 = 首次出现碰撞的临界时刻，用最近距离的根查找定位（见 [[root_finding]]），坐标来自 [[archimedean_spiral]]。
- 布局 / 排样 / 选址中的不重叠约束（设备摆放、停车、装箱的几何可行性）。
- 路径规划中的障碍物避让可行性检查（与 [[root_finding]] 定位临界接触配合）。

## 参考文献

- Ericson, C. (2004). *Real-Time Collision Detection*. Morgan Kaufmann.（SAT / AABB / GJK 权威）
- 司守奎 & 孙兆亮 (2015). 《数学建模算法与应用》(第二版). 几何建模章节.
- de Berg, M. et al. (2008). *Computational Geometry: Algorithms and Applications* (3rd ed.). Springer.
