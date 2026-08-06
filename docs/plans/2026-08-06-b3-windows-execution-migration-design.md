# B3 执行迁移到 Windows 128G 机（设计）

2026-08-06

## 背景

B3 三段流水（preflight → build → eval）在开发机上装不进内存。护栏上限 4 GiB 是
本机的约束而非作业的需求：ThinkPad 共 15 GiB，实测可用仅 5.5 GiB，连 4 GiB swap
都已用掉 3.6 GiB。2026-08-05 一轮 preflight 爬到约 8 GiB 时由用户手动杀掉，
天花板至今没人见过。

作业本身没有变小的义务。换一台有余量的机器比重构加载层便宜得多，也不必动任何
已经通过审查的 B3 代码。

迁移当天又添一条佐证：开发机上同时挂着 7 个会话，其中一个在跑 `cta_carry` 回测
（2.95 GB RSS），另一个在跑 stock_selector 全套，可用内存一度掉到 2.8 GiB。
这台机器不是 B3 一个作业的，把重活留在上面本身就是个持续的风险源。

## 决策

1. **B3 的执行平台正式定为 Windows `DESKTOP-P7MGEIR`**（Tailscale `100.120.152.1`，
   实测 127.67 GB 内存）。该机已是 stock_selector 重型回测的执行机，链路
   2026-06-18 打通，见 stock_selector `docs/operations/windows-backtest-runbook.md`。
2. **不改 B3 核心代码**。`signals/`、`backtest/`、`tools/` 经核实无 Linux-only 依赖
   （无 `resource` / `fcntl` / `malloc_trim` / `os.uname` / 硬编码 `/tmp`）。
3. **解释器完全对齐**：Windows 装 Python 3.13.9，与开发机
   `/home/elfbob/miniconda3/bin/python` 同版本，依赖按下方 lock 钉死。
4. **不做内存优化**。加载层的既有问题（见下）转为可选后续项，不再是阻断项。
5. **Windows 只做执行机，永不作为真源**；本机保留可离线重建它的全部件。

## 依赖与版本钉死

全仓库的第三方 import 只有四个：pandas、numpy、psycopg2、yaml。**没有 scipy、
sklearn、statsmodels、matplotlib**；requests / dash / plotly 只服务 `dashboard/`，
B3 三段用不到，不部署。

`requirements-windows.lock`（进 git，对齐 stock_selector 的做法）：

```
pandas==2.3.3
numpy==2.3.4
psycopg2-binary==2.9.11
PyYAML==6.0.3
pytest==9.0.2      # 平台闸门用
psutil==<部署时钉>  # win_peak_run.py 用
```

**陷阱：不要用 `pip freeze` 生成这份 lock。** 开发机的 miniconda site-packages 里
同时躺着 `numpy-2.3.4.dist-info` 与 `numpy-2.4.2.dist-info` 两份元数据，而实际
import 到的是 **2.3.4**。`importlib.metadata` / `pip freeze` 会报 2.4.2——照它部署
会把一个从未产出过任何 B3 产物的 numpy 装到执行机上，恰好毁掉本次迁移"完全对齐"
的目的。lock 一律钉 `<module>.__version__` 实际值。

（这份脏元数据建议日后单独清理。现在不动：现有全部 B3 产物就是这个环境产出的。）

## 本机备份纪律

原则：**代码改动一律发生在开发机，再重新部署过去**。Windows 侧出现任何本地修改都
视为事故，必须先回传再继续。产物方向相反——Windows 产出的 run 目录一律传回本机
进 campaign 目录，裁决在 Linux 出。

本机保留可**离线重建整台执行机**的全部件，落在 git 之外的
`/home/elfbob/claude-code/deploy_backups/2026-08-06-b3-windows/`：

| 件 | 备份形式 |
|---|---|
| 代码 | 实际部署的那个 tar 快照原件 + sha256 |
| 解释器 | Python 3.13.9 Windows 安装包原件 + sha256 |
| 依赖 | 按 lock `pip download --platform win_amd64` 下来的全部 wheel |
| 清单 | `windows_deployment.json`：部署了什么、版本、逐文件 sha256 |

`config\settings.yaml` 含凭据，已在本机 `config/` 下，不入 git、不入备份目录。

留 wheel 而不只留 lock，是因为这台机器有过下载黑洞的前科；离线可重建才算备份。

## 可复现性口径

跨平台**不保证浮点逐位一致**：Windows 的 numpy 走不同的 BLAS 与归约顺序。

- **preflight 的阻断判定不受影响**——它走的是计数与 reason code，不是浮点比较。
- **build / eval 的统计量以 Windows 为准**。执行回执记录平台、解释器版本与三个
  数值库版本，使任何一次产出都能定位到它的执行环境。

## 变更范围

只动 campaign 目录下的执行器，B3 本体零改动。

### `run_guarded_b3.py`：加平台分支

`guarded_command(argv, time_path, *, platform)`：

- `linux`（默认，行为逐字节不变）：
  `systemd-run --user --scope -p MemoryMax=4G /usr/bin/time -v -o <report> <argv>`
- `windows`：`<python> win_peak_run.py --report <report> -- <argv>`，不套帽——
  128 GB 下内存帽既无必要，也没有 systemd 可用。

平台由 `os.name` 自动判定，`--platform` 可显式覆盖（测试用）。

回执新增 `platform`、`python_version`、`library_versions` 三个字段；`memory_max`
在 Windows 下记 `null`。`verify_post_write.py` 不消费这两个字段，无涟漪。

### `win_peak_run.py`：新增

`Popen` 起子进程，psutil 轮询 `peak_wset`，退出后把峰值**按 GNU time 的行格式**
写进 report 文件：

```
	Maximum resident set size (kbytes): <N>
```

这样 `peak_rss_kib()` 一行都不用改，`run_stages` 也继续用 `subprocess.run`——
平台差异被收在一个文件里，不渗进执行器的主干。

**必须量整棵进程树**（平台闸门在执行机上抓出来的）：Windows 上 venv 的
`Scripts\python.exe` 会把 `sys._base_executable` 作为**子进程**拉起来，直接子
进程只是个转发器。实测子进程 `wset` 6 MB、`private` 1.2 MB，而真正持有 200 MB
的是孙进程（`peak_wset` 212,107,264）。只量直接子进程会把每一段都记成空壳——
B3 三段同样以 venv 解释器启动，不修则整份峰值证据作废。

取值 = `max(采样得到的整树同时驻留最大值, 树中单进程历史峰值最大者)`。前者在
真有并发时不低估，后者在采样错过瞬时高点时兜底（`peak_wset` 单调，采到一次
就够）。回归测试把"孙进程持有内存"这个形状复现出来，因此在 Linux 上也会红。

### 测试

- 现有执行器测试的**断言逐字不动**；只有 5 处 `run_stages(...)` 调用显式补上
  `platform="linux"`。这一步是必要的：平台闸门要在 Windows 上跑这同一个文件，
  若让它们走自动判定，那几条钉 `systemd-run` 前缀的断言会在执行机上全红。
- 新增：两个平台的命令拼装断言；`win_peak_run.py` 写出的 report 能被
  `peak_rss_kib()` 解析回一个合理的峰值；子进程退出码逐字透传；
  `describe_interpreter` 读的是 `module.__version__` 而**不是**发行元数据
  ——这条测试在开发机上直接压住 numpy 2.3.4/2.4.2 那个偏差。

## 迁移步骤

0. **在本机把部署件备齐**：tar 快照、Python 3.13.9 Windows 安装包、按 lock
   `pip download --platform win_amd64 --only-binary=:all: --python-version 3.13`
   下来的 wheel，逐件 sha256 记进 `windows_deployment.json`。**先备份，后部署**——
   传过去的必须就是备份里的那一份。
1. Windows 装 Python 3.13.9（静默安装，`InstallAllUsers=0 PrependPath=0`）。
   **不动系统 Python**——Wind 终端的 WindPy 在那里，网关依赖它。
2. 代码走快照：把第 0 步那个 tar 传到 `D:\style_timing_signal`。不装 git，因此
   也不必现在决定是否 push style origin。
3. `py -3.13 -m venv .venv`，`pip install --no-index --find-links <wheel 目录>
   -r requirements-windows.lock`。离线装，与网关的系统 Python 完全隔离。
4. 传 `config\settings.yaml`（含凭据，gitignored）。主库 `pg_hba.conf` 已有
   `host all admin 100.64.0.0/10 scram-sha-256`，整个 tailnet 放行，零服务端改动。
5. 连通性自测：psycopg2 直连 `100.65.111.79`，再走仓库自身配置读一张表。
6. 平台闸门：Windows 上跑 `tests/test_b3_wind_share_capital_postwrite.py`。
7. 跑三段（长跑，后台 + 输出重定向），产物传回开发机，在 Linux 上跑
   `verify_post_write.py` 出裁决。

## 顺带取得的证据

上一轮把峰值归因给 `build_policy_snapshots`，依据是"13 条查询全部加载完峰值仅
474 MiB"。但该探针包的是 `_read_sql`，而财务事实由 `_fetch_raw_financial`
（`b3_build.py:1369`）自开 cursor 加载，**不走 `_read_sql`**，且它是
`_formation_inputs` 的**最后一个**加载器（`:2197`）。那 474 MiB 是财务事实开始
加载之前的峰值，最大的那次加载从未被测到。

嫌疑：`facts` 每行带一个 `data` 列，装的是一个 Python dict（`:1440`）。100 票分批
只限制了在途的原始 JSON，翻译后的 dict 全部留在 `parts` 里最后 concat 成一整帧
（`:1441`、`:1449`），3.49M 行一个不落地常驻。且 `memory_usage(deep=True)` 对 dict
不递归，任何按帧大小估内存的探针都会系统性低估这一列。

无帽运行会让真实峰值直接显形。这不是本次迁移的目标，但是它的免费副产品。

## 平台闸门抓到的问题（2026-08-06 首次打通）

按顺序记下来，因为每一条都只有在真机上跑才会暴露，而重新发现的成本很高。

1. **离线 wheel 少了 `colorama`**。pytest 在 win32 上条件依赖它，而
   `pip download --platform win_amd64` **不按目标平台求值环境标记**——marker 用的
   是本机的 `sys_platform`。补下载即可，但说明"只留 lock 不留 wheel"不算备份。
2. **venv 的 python 是转发器**，见上节。峰值测量必须连子孙一起量。
3. **`scipy` 是运行期必需但无处 import**。pandas 在 `nanops` 里惰性 import 它算
   秩相关，B3 到处用 spearman。漏装不在 import 期报错，只在跑到秩 IC 时炸——
   真实三段上就是几小时后才炸。首次全套 37 个失败里 16 个是它。
4. **`git` 不在执行机上，而快照又没有 `.git`**。`b3_eval.git_commit()` 调裸
   `git rev-parse HEAD` 记溯源，取不到就 `DataBlocked`，三段会立刻全灭。
   处置：装 Git for Windows（PATH 集成）+ **改用 `git bundle` 部署**——`.git`
   才 17 MB，clone 出来的 HEAD 就是开发机那个 commit，既不动 B3 的 fail-closed
   闸门，也不必先 push origin。
5. **两条测试断言写死了 POSIX 约定**（`/tmp/x` 在 Windows 上不绝对；
   `endswith("a/b/c")` 撞上反斜杠）。被测代码两处都正确，是断言不可移植。
6. **长跑的起法**。`Start-Process` 起的进程随 SSH 会话结束而死；
   `Win32_Process::Create` 被 360 拒绝（`ReturnValue=2`，它是典型横向移动手法）。
   可用的是**计划任务**。另外 `.cmd` 必须 **CRLF + 纯 ASCII + 绝对路径**：
   cmd.exe 按 OEM 代码页读 .cmd，且任务从 `System32` 起步。

## 不做

- 不改 `_fetch_raw_financial` / `build_policy_snapshots`。
- 不 push style origin；分支仍只在本地与 Windows 快照。
- 不在 Windows 上做任何数据库写入。
