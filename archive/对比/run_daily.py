"""
风格择时因子计算 - 每日自动运行脚本

功能：
1. 打开 data.xlsx，让 Wind 自动更新数据
2. 等待更新完成后保存并关闭
3. 清除缓存
4. 计算并保存因子

依赖：pip install pywin32
"""

import sys
import time
import subprocess
from pathlib import Path
from datetime import datetime


def get_local_path(unc_path: str) -> str:
    r"""
    将 UNC 路径转换为本地驱动器路径
    例如: \\server\share\path -> W:\path
    """
    import os
    # 检查是否是 UNC 路径
    if unc_path.startswith('\\\\'):
        # 尝试使用已映射的 W: 盘
        drive_w = os.path.abspath('W:\\')
        if os.path.exists(drive_w):
            # 提取 UNC 路径中的相对路径部分
            parts = unc_path.replace('\\', '/').split('/')
            # 找到共享名后的路径（通常格式是 \\server\share\path）
            if len(parts) >= 4:
                rel_path = '/'.join(parts[4:])  # 跳过 \\server\share
                local_path = os.path.join(drive_w, rel_path)
                return local_path
    return unc_path


def update_excel_with_os_open(excel_path: str, wait_time: int = 30):
    """
    使用系统默认程序打开 Excel，让 Wind 自动更新数据
    不依赖 COM，简单可靠
    """
    import os
    import psutil

    print(f"正在使用系统默认程序打开 Excel...")
    print(f"文件: {excel_path}")
    print(f"等待 Wind 自动更新数据 ({wait_time} 秒)...")

    try:
        # 使用系统默认程序打开
        os.startfile(excel_path)

        # 等待 Excel 启动
        time.sleep(3)

        # 等待 Wind 更新
        for i in range(wait_time):
            time.sleep(1)
            if (i + 1) % 5 == 0:
                print(f"[{datetime.now().strftime('%H:%M:%S')}] 已等待 {i + 1} 秒...")

        # 查找并关闭 Excel 进程
        print("正在关闭 Excel...")
        for proc in psutil.process_iter(['name']):
            if proc.info['name'] and 'EXCEL.EXE' in proc.info['name'].upper():
                proc.terminate()
                break

        time.sleep(1)
        print("数据更新完成")
        return True

    except Exception as e:
        print(f"操作出错: {e}")
        return False


def update_excel_with_xlwings(excel_path: str, wait_time: int = 30):
    """
    使用 xlwings 打开 Excel，让 Wind 自动更新数据
    """
    try:
        import xlwings as xw
    except ImportError:
        print("错误: 需要安装 xlwings")
        print("请运行: pip install xlwings")
        return False

    print(f"正在使用 xlwings 打开 Excel: {excel_path}")
    print(f"等待 Wind 自动更新数据 ({wait_time} 秒)...")

    app = None
    wb = None

    try:
        app = xw.App(visible=True)  # 改为可见，方便调试
        wb = app.books.open(str(excel_path))

        # 关闭自动创建的空白工作簿（工作簿1/Book1）
        for book in app.books:
            if book.name in ["工作簿1", "Book1"]:
                book.close()
                break

        # 等待 Wind 更新
        for i in range(wait_time):
            time.sleep(1)
            if (i + 1) % 5 == 0:
                print(f"[{datetime.now().strftime('%H:%M:%S')}] 已等待 {i + 1} 秒...")

        wb.save()
        wb.close()
        print("数据更新完成")
        return True

    except Exception as e:
        print(f"Excel 操作出错: {e}")
        return False

    finally:
        if app:
            try:
                app.quit()
            except:
                pass


def build_cache(project_dir: Path):
    """构建 NPZ 二进制缓存"""
    print("\n构建 NPZ 缓存...")
    env = _subprocess_env(project_dir)
    result = subprocess.run(
        [sys.executable, "cli/build_cache.py"],
        cwd=project_dir,
        capture_output=False,
        env=env,
    )
    if result.returncode != 0:
        print("警告: NPZ 缓存构建失败，将从 Excel 直接加载")
    return result.returncode == 0


def clear_cache(project_dir: Path):
    """清除缓存文件"""
    npz_file = project_dir / "data" / "cache" / "index_data.npz"
    parquet_file = project_dir / "data" / "cache" / "index_cache.parquet"
    cleared = False
    for f in [npz_file, parquet_file]:
        if f.exists():
            f.unlink()
            print(f"已清除缓存: {f.name}")
            cleared = True
    if not cleared:
        print("缓存文件不存在，将直接读取最新数据")


def _subprocess_env(project_dir: Path):
    """构建子进程环境变量，确保 PYTHONPATH 包含项目根目录"""
    import os
    env = os.environ.copy()
    project_str = str(project_dir)
    existing = env.get("PYTHONPATH", "")
    if project_str not in existing:
        env["PYTHONPATH"] = f"{project_str};{existing}" if existing else project_str
    return env


def export_xlsx_to_csv(project_dir: Path):
    """将 data.xlsx 导出为 data.csv"""
    try:
        import pandas as pd
    except ImportError:
        print("警告: 需要安装 pandas 才能导出 CSV")
        print("跳过 CSV 导出")
        return True

    xlsx_path = project_dir / "data" / "local" / "data.xlsx"
    csv_path = project_dir / "data" / "local" / "data.csv"

    if not xlsx_path.exists():
        print(f"警告: data.xlsx 不存在，跳过导出")
        return True

    try:
        print("正在将 data.xlsx 导出为 data.csv...")
        # 读取 Excel（假设是合并格式，从第5行开始是数据）
        df = pd.read_excel(xlsx_path, header=4)
        df.to_csv(csv_path, index=False, encoding='utf-8-sig')
        print(f"已导出到: {csv_path}")
        return True
    except Exception as e:
        print(f"警告: 导出 CSV 失败: {e}")
        print("将继续使用 data.xlsx")
        return True


def calculate_factor(project_dir: Path):
    """计算因子"""
    print("\n开始计算因子...")

    # 检查当前时间是否在15:00前
    current_time = datetime.now().time()
    cutoff_time = datetime.strptime("15:00", "%H:%M").time()

    if current_time < cutoff_time:
        print("当前时间在15:00前，将使用前一天的数据进行计算")
        cmd = [sys.executable, "generate_style_factor.py", "--use-previous-day"]
    else:
        print("当前时间在15:00后，使用当日数据进行计算")
        cmd = [sys.executable, "generate_style_factor.py"]

    env = _subprocess_env(project_dir)
    result = subprocess.run(
        cmd,
        cwd=project_dir,
        capture_output=False,
        env=env,
    )
    return result.returncode == 0


def main():
    """主函数"""
    print("=" * 50)
    print("风格择时因子自动计算")
    print(f"开始时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 50)

    project_dir = Path(__file__).parent
    excel_path = project_dir / "data" / "local" / "data.xlsx"
    cache_dir = project_dir / "data" / "local"

    if not excel_path.exists():
        print(f"错误: 文件不存在 - {excel_path}")
        return 1

    # 1. 更新 Excel 数据 (优先使用 xlwings，可以正确保存)
    try:
        import xlwings
        print("使用 xlwings 方法（推荐）")
        success = update_excel_with_xlwings(str(excel_path), wait_time=30)
    except ImportError:
        print("xlwings 未安装，使用系统默认程序打开...")
        success = update_excel_with_os_open(str(excel_path), wait_time=30)

    # 如果 xlwings 失败，尝试系统打开
    if not success:
        print("\n尝试使用系统默认程序...")
        success = update_excel_with_os_open(str(excel_path), wait_time=30)

    if not success:
        print("\n警告: Excel 自动更新失败，将使用现有数据计算")

    # 2. 导出为 CSV（确保数据一致性）
    export_xlsx_to_csv(project_dir)

    # 3. 清除缓存
    clear_cache(project_dir)

    # 4. 构建 NPZ 缓存
    build_cache(project_dir)

    # 5. 计算因子
    if calculate_factor(project_dir):
        print("\n因子计算完成")
    else:
        print("\n因子计算失败")
        return 1

    print("=" * 50)
    print(f"完成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 50)

    return 0


if __name__ == "__main__":
    sys.exit(main())
