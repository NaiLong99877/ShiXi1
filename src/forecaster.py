"""
日内滚动预测器 —— 为 MPC 实时调度提供未来 H 个时段的 LMP / AGC 预测

设计原则:
- 简单、可解释、不依赖外部库
- LMP: 以日前出清价为基线, 用已观测实时偏差做短期修正
- AGC: 稀疏信号, 默认保守地预测为 0, 当前非零时按衰减保持
"""
from typing import List, Optional


class Forecaster:
    """
    滚动预测器

    Args:
        da_lmp: 日前 LMP 曲线 (96点, 元/MWh), 作为 LMP 预测基线
        da_agc_cap: 日前 AGC 容量计划 (96点, MW), 可选, 用于 AGC 期望预测
        lmp_bias_decay: 实时偏差衰减系数 (0~1), 越接近 1 表示偏差持续时间越长
        agc_persistence: AGC 非零状态的持续系数
        agc_zero_prob: AGC 为 0 的先验概率, 用于期望值预测
    """

    def __init__(
        self,
        da_lmp: List[float],
        da_agc_cap: Optional[List[float]] = None,
        lmp_bias_decay: float = 0.8,
        agc_persistence: float = 0.5,
        agc_zero_prob: float = 0.85,
    ):
        assert len(da_lmp) == 96, "da_lmp 应为 96 点"
        self.da_lmp = da_lmp
        self.da_agc_cap = da_agc_cap or [0.0] * 96
        self.lmp_bias_decay = lmp_bias_decay
        self.agc_persistence = agc_persistence
        self.agc_zero_prob = agc_zero_prob

        # 已观测的实时偏差: t -> rt_lmp[t] - da_lmp[t]
        self.bias_history: dict[int, float] = {}

    def update(self, t: int, rt_lmp: float, agc_cmd: float) -> None:
        """更新时刻 t 的观测"""
        self.bias_history[t] = rt_lmp - self.da_lmp[t]

    def predict_lmp(self, t: int, H: int) -> List[float]:
        """
        预测未来 H 个时段的实时 LMP

        方法: 日前基线 + 最新观测偏差按 lmp_bias_decay 衰减
        """
        T = len(self.da_lmp)
        latest_bias = self.bias_history.get(t - 1, 0.0)
        forecast = []
        for k in range(1, H + 1):
            idx = t + k - 1
            if idx >= T:
                # 超出当日范围, 沿用最后一个日前价格 + 衰减后的偏差
                base = self.da_lmp[-1]
            else:
                base = self.da_lmp[idx]
            bias = latest_bias * (self.lmp_bias_decay ** k)
            forecast.append(round(base + bias, 2))
        return forecast

    def predict_agc_zero(self, t: int, H: int) -> List[float]:
        """最保守预测: 未来 AGC 全部为 0"""
        return [0.0] * H

    def predict_agc_persistence(self, t: int, H: int, current_agc: float) -> List[float]:
        """
        状态保持预测: 当前 AGC 非零时, 未来按 agc_persistence 衰减
        当前为 0 时, 未来保持为 0
        """
        forecast = []
        for k in range(1, H + 1):
            val = current_agc * (self.agc_persistence ** k)
            forecast.append(round(val, 2))
        return forecast

    def predict_agc_expected(self, t: int, H: int) -> List[float]:
        """
        期望值预测: 用日前 AGC 容量 * (1 - zero_prob)
        适合 AGC 指令较密集的场景
        """
        T = len(self.da_lmp)
        forecast = []
        for k in range(1, H + 1):
            idx = t + k - 1
            cap = self.da_agc_cap[idx] if idx < T else self.da_agc_cap[-1]
            expected = cap * (1.0 - self.agc_zero_prob)
            forecast.append(round(expected, 2))
        return forecast

    def predict_agc(self, t: int, H: int, current_agc: float = 0.0, mode: str = "zero") -> List[float]:
        """
        AGC 预测入口

        mode:
          - "zero": 保守零预测 (默认)
          - "persistence": 状态保持
          - "expected": 日前容量期望值
        """
        if mode == "zero":
            return self.predict_agc_zero(t, H)
        if mode == "persistence":
            return self.predict_agc_persistence(t, H, current_agc)
        if mode == "expected":
            return self.predict_agc_expected(t, H)
        raise ValueError(f"未知 AGC 预测模式: {mode}")


def make_forecaster_from_day_ahead(
    da_lmp: List[float],
    da_agc_cap: Optional[List[float]] = None,
    **kwargs,
) -> Forecaster:
    """从日前市场数据构造预测器"""
    return Forecaster(da_lmp=da_lmp, da_agc_cap=da_agc_cap, **kwargs)
