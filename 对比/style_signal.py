"""
风格信号提取模块（基于成长/价值指数对）

四步法实现风险偏好信号提取：
Step 1: 市场多重分层 - 通过不同市值层级的指数对实现
Step 2: 层内因子组合 - 成长/价值指数本身就是因子组合
Step 3: 跨层信号提取 - 比较不同市值层级的相对强度
Step 4: 信号合并 - 加权合并得到最终风险偏好信号
"""

import json
import numpy as np
import pandas as pd
from typing import Dict, List, Optional, Tuple
import yaml


class HierarchicalStyleSignalExtractor:
    """
    分层风格信号提取器

    实现四步法风险偏好信号提取
    """

    def __init__(self, config_path: str = None):
        """
        初始化信号提取器

        Parameters
        ----------
        config_path : str, optional
            配置文件路径
        """
        if config_path is None:
            config_path = "config/index_pairs.yaml"

        with open(config_path, 'r', encoding='utf-8') as f:
            self.config = yaml.safe_load(f)

        self.index_pairs = self.config['index_pairs']
        self.signal_config = self.config['signal_config']

        # Step 1: 按市值层级分组
        self.layers = self._build_layers()

    # ============================================================
    # Step 1: 市场多重分层
    # ============================================================

    def _build_layers(self) -> Dict[str, List[Dict]]:
        """
        Step 1: 构建市值层级

        将指数对按市值层级分组

        Returns
        -------
        Dict[str, List[Dict]]
            按市值层级分组的指数对
        """
        layers = {
            'large': [],   # 大盘
            'mid': [],     # 中盘
            'small': []    # 小盘
        }

        for pair in self.index_pairs:
            tier = pair.get('market_cap_tier', 'mid')
            if tier in layers:
                layers[tier].append(pair)

        print("\n" + "=" * 50)
        print("Step 1: 市场多重分层")
        print("=" * 50)
        for tier_name, pairs in layers.items():
            tier_cn = {'large': '大盘', 'mid': '中盘', 'small': '小盘'}[tier_name]
            pair_names = [p['name'] for p in pairs]
            print(f"\n{tier_cn}层 ({len(pairs)}对):")
            for name in pair_names:
                print(f"  - {name}")

        return layers

    def get_layer_stocks(self, layer_name: str) -> List[Dict]:
        """获取指定层级的指数对"""
        return self.layers.get(layer_name, [])

    # ============================================================
    # Step 2: 层内因子组合
    # ============================================================

    def calculate_layer_relative_return(self, growth_price: pd.Series,
                                        value_price: pd.Series) -> pd.Series:
        """
        Step 2: 计算层级内相对收益

        相对收益 = 成长指数收益 - 价值指数收益

        Parameters
        ----------
        growth_price : pd.Series
            成长指数价格
        value_price : pd.Series
            价值指数价格

        Returns
        -------
        pd.Series
            层级内相对收益序列
        """
        # 对齐日期
        aligned = pd.DataFrame({
            'growth': growth_price,
            'value': value_price
        }).dropna()

        # 计算收益率
        growth_ret = aligned['growth'].pct_change().fillna(0)
        value_ret = aligned['value'].pct_change().fillna(0)

        # 相对收益 = 成长收益 - 价值收益
        relative_return = growth_ret - value_ret

        return relative_return

    def calculate_pair_relative_return(self, price_data: Dict[str, pd.Series],
                                       pair: Dict) -> pd.Series:
        """
        计算单对指数的相对收益

        Parameters
        ----------
        price_data : Dict[str, pd.Series]
            所有指数价格数据
        pair : Dict
            指数对配置

        Returns
        -------
        pd.Series
            相对收益序列
        """
        growth_code = pair['growth_code']
        value_code = pair['value_code']

        if growth_code not in price_data or value_code not in price_data:
            return pd.Series(dtype=float)

        return self.calculate_layer_relative_return(
            price_data[growth_code],
            price_data[value_code]
        )

    def calculate_all_layer_returns(self, price_data: Dict[str, pd.Series]) -> Dict[str, pd.DataFrame]:
        """
        计算所有层级的相对收益

        Returns
        -------
        Dict[str, pd.DataFrame]
            每个层级的相对收益数据框
        """
        print("\n" + "=" * 50)
        print("Step 2: 层内因子组合 - 计算相对收益")
        print("=" * 50)

        layer_returns = {}

        for tier, pairs in self.layers.items():
            tier_cn = {'large': '大盘', 'mid': '中盘', 'small': '小盘'}[tier]
            returns_data = {}

            for pair in pairs:
                rel_ret = self.calculate_pair_relative_return(price_data, pair)
                if not rel_ret.empty:
                    returns_data[pair['name']] = rel_ret

            if returns_data:
                layer_returns[tier] = pd.DataFrame(returns_data)
                print(f"\n{tier_cn}层相对收益统计:")
                print(layer_returns[tier].describe().loc[['mean', 'std', 'min', 'max']])

        return layer_returns

    # ============================================================
    # Step 3: 跨层信号提取
    # ============================================================

    def _compute_pair_signal(self, rel_ret_1d: np.ndarray,
                             window: int) -> np.ndarray:
        """对单对指数的相对收益序列计算信号.

        等价于:
            cumulative = (1 + weighted_ret).rolling(window).apply(lambda x: x.prod() - 1)
            rolling_mean = cumulative.rolling(window*2).mean()
            rolling_std  = cumulative.rolling(window*2).std()
            zscore = (cumulative - rolling_mean) / rolling_std
            signal = np.tanh(zscore / 2)

        Parameters
        ----------
        rel_ret_1d : np.ndarray (D,)
            单对指数的相对收益（1D）
        window : int
            lookback 窗口

        Returns
        -------
        np.ndarray (D,)
        """
        D = len(rel_ret_1d)

        # 滚动累计收益: (1 + ret).rolling(w).prod() - 1
        one_plus = 1.0 + rel_ret_1d  # (D,)
        # 使用 pandas rolling 实现滚动 product
        cumulative = pd.Series(one_plus).rolling(window, min_periods=1).apply(
            lambda x: x.prod() - 1, raw=False
        ).values  # (D,)

        # 滚动 z-score，窗口 = window * 2
        cum_series = pd.Series(cumulative)
        rolling_mean = cum_series.rolling(window * 2, min_periods=1).mean().values
        rolling_std = cum_series.rolling(window * 2, min_periods=1).std().values
        rolling_std = np.where(rolling_std < 1e-8, 1e-8, rolling_std)
        zscore = (cumulative - rolling_mean) / rolling_std

        # 填充前期 NaN 为 0
        zscore = np.where(np.isnan(zscore), 0.0, zscore)

        # tanh 压缩
        signal = np.tanh(zscore / 2.0)

        return signal  # (D,)

    def calculate_layer_signal(self, layer_returns: pd.DataFrame,
                               window: int = None) -> pd.Series:
        """
        计算单层信号

        对相对收益进行滚动累计和标准化

        Parameters
        ----------
        layer_returns : pd.DataFrame
            层级内各指数对的相对收益
        window : int, optional
            滚动窗口

        Returns
        -------
        pd.Series
            层级信号
        """
        if layer_returns.empty:
            return pd.Series(dtype=float)

        window = window or self.signal_config['lookback_window']

        # 计算加权平均相对收益（按配置权重）
        weights = {}
        for col in layer_returns.columns:
            for pair in self.index_pairs:
                if pair['name'] == col:
                    weights[col] = pair['weight']
                    break
            if col not in weights:
                weights[col] = 1.0

        # 加权平均
        total_weight = sum(weights.get(col, 1.0) for col in layer_returns.columns)
        weighted_ret = pd.Series(0.0, index=layer_returns.index)
        for col in layer_returns.columns:
            weighted_ret += layer_returns[col] * weights.get(col, 1.0)
        weighted_ret /= total_weight

        # 计算信号
        signal_values = self._compute_pair_signal(weighted_ret.values, window)

        return pd.Series(signal_values, index=layer_returns.index)

    def calculate_cross_layer_signal(self, layer_returns: Dict[str, pd.DataFrame]) -> pd.Series:
        """
        Step 3: 跨层信号提取

        核心逻辑：
        最终信号 = 所有指数对信号的等权平均

        当信号 > 0: 成长跑赢价值 → 风险偏好上升
        当信号 < 0: 价值跑赢成长 → 风险偏好下降

        Parameters
        ----------
        layer_returns : Dict[str, pd.DataFrame]
            各层级相对收益

        Returns
        -------
        pd.Series
            跨层风险偏好信号
        """
        print("\n" + "=" * 50)
        print("Step 3: 跨层信号提取（15对指数等权平均）")
        print("=" * 50)

        # 收集所有指数对的信号
        all_pair_signals = []
        all_dates = None

        for tier, returns_df in layer_returns.items():
            if not returns_df.empty:
                # 计算该层每对指数的信号
                for col in returns_df.columns:
                    signal = self.calculate_layer_signal(returns_df[[col]])
                    all_pair_signals.append(signal)
                    if all_dates is None:
                        all_dates = signal.index
                    else:
                        all_dates = all_dates.union(signal.index)

        if not all_pair_signals:
            return pd.Series(dtype=float)

        # 等权平均所有指数对的信号
        all_dates = sorted(all_dates)
        cross_layer_signal = pd.Series(0.0, index=all_dates)

        for signal in all_pair_signals:
            cross_layer_signal += signal.reindex(all_dates).fillna(0)

        cross_layer_signal /= len(all_pair_signals)

        print(f"\n使用了 {len(all_pair_signals)} 对指数的信号")
        print(f"最终信号统计 (等权平均 {len(all_pair_signals)} 对):")
        print(f"  均值={cross_layer_signal.mean():.4f}")
        print(f"  标准差={cross_layer_signal.std():.4f}")
        print(f"  最小值={cross_layer_signal.min():.4f}")
        print(f"  最大值={cross_layer_signal.max():.4f}")

        return cross_layer_signal

    # ============================================================
    # Step 4: 信号合并
    # ============================================================

    def calculate_final_signal(self, price_data: Dict[str, pd.Series]) -> pd.Series:
        """
        执行完整的四步法信号提取

        Parameters
        ----------
        price_data : Dict[str, pd.Series]
            指数价格数据

        Returns
        -------
        pd.Series
            最终风险偏好信号 (-1 到 1)
        """
        print("\n" + "=" * 60)
        print("四步法风险偏好信号提取")
        print("=" * 60)

        # Step 1: 市场多重分层 (已在初始化时完成)
        # self.layers 已构建

        # Step 2: 层内因子组合
        layer_returns = self.calculate_all_layer_returns(price_data)

        # Step 3: 跨层信号提取
        cross_layer_signal = self.calculate_cross_layer_signal(layer_returns)

        # Step 4: 信号平滑
        print("\n" + "=" * 50)
        print("Step 4: 信号平滑")
        print("=" * 50)

        smooth_window = self.signal_config.get('smoothing_window', 5)

        # 滚动平均平滑
        smoothed = cross_layer_signal.rolling(smooth_window, min_periods=1).mean()

        final_signal = smoothed.fillna(cross_layer_signal)

        print(f"平滑窗口: {smooth_window}天")
        print(f"最终信号范围: [{final_signal.min():.4f}, {final_signal.max():.4f}]")

        return final_signal

    def discretize_signal(self, signal: pd.Series) -> pd.Series:
        """
        将连续信号离散化为 -1/0/1

        Parameters
        ----------
        signal : pd.Series
            连续信号

        Returns
        -------
        pd.Series
            离散信号
        """
        upper = self.signal_config['upper_threshold']
        lower = self.signal_config['lower_threshold']

        discrete = pd.Series(0, index=signal.index)
        discrete[signal >= upper] = 1
        discrete[signal <= lower] = -1

        return discrete

    def get_all_pair_signals(self, price_data: Dict[str, pd.Series]) -> pd.DataFrame:
        """
        获取所有指数对的信号（用于分析）

        Parameters
        ----------
        price_data : Dict[str, pd.Series]
            指数价格数据

        Returns
        -------
        pd.DataFrame
            各指数对信号
        """
        pair_signals = {}

        for pair in self.index_pairs:
            rel_ret = self.calculate_pair_relative_return(price_data, pair)
            if not rel_ret.empty:
                # 计算信号
                window = self.signal_config['lookback_window']
                signal_values = self._compute_pair_signal(rel_ret.values, window)
                pair_signals[pair['name']] = pd.Series(signal_values, index=rel_ret.index)

        return pd.DataFrame(pair_signals)


# 向后兼容的别名
IndexPairSignalExtractor = HierarchicalStyleSignalExtractor


if __name__ == "__main__":
    # 测试四步法信号提取器
    import sys
    sys.path.append('..')

    from data.data_loader import MockDataGenerator

    print("测试四步法信号提取器...\n")

    # 生成模拟数据
    mock_gen = MockDataGenerator()
    with open('config/index_pairs.yaml', 'r', encoding='utf-8') as f:
        config = yaml.safe_load(f)
    price_data = mock_gen.generate_all_data(config)

    # 创建信号提取器
    extractor = HierarchicalStyleSignalExtractor()

    # 执行四步法
    signal = extractor.calculate_final_signal(price_data)

    # 离散化
    discrete = extractor.discretize_signal(signal)

    print(f"\n" + "=" * 50)
    print("信号结果")
    print("=" * 50)
    print(f"离散信号分布:")
    print(discrete.value_counts())
