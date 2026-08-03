"""AgentConfig：统一配置（类似 WatchdogConfig）。

所有阈值集中管理，Phase 2 的自适应阈值也从此处接入。
"""

from dataclasses import dataclass, field


@dataclass
class AgentConfig:
    """Agent 统一配置。"""

    # 影响面计算
    max_hops: int = 5                # 影响面最大跳数
    decay: float = 0.5               # 距离衰减因子
    impact_threshold: int = 100      # 影响面熔断阈值
    in_degree_threshold: int = 100   # 高入度节点阈值（用于围栏标记）

    # FSM
    rollback_limit: int = 3          # 最大回滚次数
    check_fail_limit: int = 3        # 连续语法检查失败阈值（触发回滚）
    max_steps: int = 50              # 最大步骤数
    token_budget: int = 100000       # Token 预算

    # 权限围栏（Phase 2 模块 B）
    fence_penalty: float = 2.0       # 高影响文件的影响面代价惩罚乘数

    # 分层加载
    max_l1_neighbors: int = 30       # L1 邻接最大展示数量
    max_l2_neighbors: int = 60       # L2 影响半径最大展示数量
    top_n_in_degree: int = 10        # L0 摘要 Top-N 高入度节点

    # 日志解析器（Phase 2 模块 D）
    failures_segment_limit: int = 2000  # FAILURES 段截断上限（防上下文膨胀）

    # 盲区处理
    max_polymorphism_edges: int = 30  # 多态全笼罩最大边数（防止爆炸）
    max_importlib_candidates: int = 20  # importlib 全笼罩最大候选模块数

    # 文件操作关键字（IO 边识别）
    io_keywords: tuple[str, ...] = (
        "open", "os.open", "os.read", "os.write",
        ".read", ".write", ".readlines", ".writelines",
        "json.load", "json.dump", "json.loads", "json.dumps",
        "subprocess.run", "subprocess.call", "subprocess.Popen",
        "os.remove", "os.rename", "os.unlink", "os.listdir", "os.mkdir",
        "shutil.copy", "shutil.move", "shutil.rmtree",
        "pickle.load", "pickle.dump", "yaml.load", "yaml.dump",
    )

    # 图构建缓存
    graph_cache_ttl_sec: int = 3600   # 缓存有效期（git HEAD 未变即有效）

    # 自适应阈值预留（P3）
    def adaptive_impact_threshold(self, node_count: int) -> float:
        """按图规模自适应影响面阈值。"""
        return max(float(self.impact_threshold), node_count * 0.01)
