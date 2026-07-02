"""
生成风格择时因子值

输出：
- 固定文件: results/style_factor.csv
- 历史备份: results/history/style_factor_YYYYMMDD.csv
"""

import sys
import pandas as pd
import numpy as np
from pathlib import Path
from datetime import datetime, timedelta
import argparse

# 添加项目路径
sys.path.insert(0, str(Path(__file__).parent))

from data.data_loader import IndexDataLoader, load_index_data
from signals.style_signal import HierarchicalStyleSignalExtractor
from utils.paths import CACHE_FILE


def main():
    """生成风格择时因子值并保存到两份CSV文件"""

    # 解析命令行参数
    parser = argparse.ArgumentParser(description='生成风格择时因子值')
    parser.add_argument('--use-previous-day', action='store_true',
                        help='使用前一天的数据作为最后一天进行计算（用于15:00前运行）')
    args = parser.parse_args()

    # 尝试从 NPZ 缓存加载（快速路径）
    price_data = None
    try:
        prices, codes, dates, pair_map = load_index_data()
        print(f"从 NPZ 缓存加载: {CACHE_FILE}")
        print(f"  prices shape: {prices.shape}, dates: {dates[0]} ~ {dates[-1]}")
        # 转换为 Dict[str, pd.Series] 以兼容信号提取器
        price_data = {}
        for j, code in enumerate(codes):
            price_data[code] = pd.Series(
                prices[:, j], index=pd.DatetimeIndex(dates), name=code
            )
    except FileNotFoundError:
        print("NPZ 缓存不存在，从 Excel/CSV 加载...")
        loader = IndexDataLoader()
        price_data = loader.load_required_indices()
        if price_data:
            price_data = loader.align_dates(price_data)

    if not price_data:
        print("错误: 无法加载数据")
        return

    # 动态设置结束日期
    today = datetime.now().date()
    if args.use_previous_day:
        # 使用前一天的数据
        end_date = today - timedelta(days=1)
        print(f"使用前一天数据，计算日期: {end_date}")
    else:
        # 使用当天数据
        end_date = today
        print(f"使用当日数据，计算日期: {end_date}")

    # 过滤到指定日期及之前的数据
    for code in price_data:
        price_data[code] = price_data[code][price_data[code].index <= pd.Timestamp(end_date)]

    actual_end_date = list(price_data.values())[0].index[-1].date()

    # 计算因子值
    factor_value = HierarchicalStyleSignalExtractor().calculate_final_signal(price_data)

    # 创建数据框，按时间降序排列
    factor_df = pd.DataFrame({
        'date': factor_value.index,
        'factor_value': factor_value.values
    }).sort_values('date', ascending=False).reset_index(drop=True)

    # 创建输出目录
    output_dir = Path("results")
    output_dir.mkdir(exist_ok=True)
    history_dir = output_dir / "history"
    history_dir.mkdir(exist_ok=True)

    # 1. 保存固定文件（覆盖）
    fixed_filepath = output_dir / "style_factor.csv"
    factor_df.to_csv(fixed_filepath, index=False, float_format='%.4f')

    # 2. 保存历史备份文件
    today = datetime.now().strftime("%Y%m%d")
    history_filepath = history_dir / f"style_factor_{today}.csv"
    factor_df.to_csv(history_filepath, index=False, float_format='%.4f')

    # 输出结果
    print(f"数据范围: {factor_value.index[0].date()} ~ {actual_end_date}")
    print(f"最新因子值 ({actual_end_date}): {factor_value.iloc[-1]:.4f}")
    print(f"已保存固定文件: {fixed_filepath}")
    print(f"已保存历史文件: {history_filepath}")
    print("\n最新10期:")
    print(factor_df.head(10).to_string(index=False))


if __name__ == "__main__":
    main()
