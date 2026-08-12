# WSL2 执行环境运维手册（DESKTOP-P7MGEIR）

> **For Claude / 零上下文接手者：** 本文是 2026-08-10 WSL2 执行环境搭建（Batch 0–4，Tier 1 双审）的收官交付物，
> 目标是让一个完全没有历史上下文的人照着就能进环境、跑长任务、出问题时定位、以及在需要时把整套环境干净拆掉。
> 每条结论都可回溯到 `/home/elfbob/claude-code/deploy_backups/2026-08-10-wsl2/evidence/` 下的原始证据文件。
> 建设过程与决策依据见同目录 `2026-08-10-wsl2-execution-environment.md`（实施计划）。
>
> **验收后修订（2026-08-12）**：补入两项验收后的宿主变更——H8 删除 staging 明文口令副本（§3.1 末）、
> H9 部署启动文件夹锚（§5.3，**已部署但尚未生效，需一次登录事件**）。证据在 `evidence/post-accept/`。

**机器：** DESKTOP-P7MGEIR，Windows 11 专业版 Build 26200（25H2），32 逻辑核 / 127.66 GiB RAM / D: 约 1.79T。
**身份：** `ghls`，Tailscale `100.120.152.1`。
**并存的生产负载（一律不动）：** Wind 终端 + Wind 网关（`D:\wind_gateway\start_gateway_interactive`，:8080）、
market monitor 采集、360 终端安全管理系统全家桶、以及 `D:\style_timing_signal` 上的 B3 原生批。

---

## ① 入口协议

### 1.1 默认通道（任何情况下都可用）

```bash
timeout 120 ssh -o BatchMode=yes -o ConnectTimeout=10 -p 2222 ghls@100.120.152.1 "<单条命令>"
```

2222 = **Windows 宿主的 OpenSSH**，默认 shell 是 **PowerShell**。进 WSL 再套一层：

```bash
timeout 180 ssh -o BatchMode=yes -o ConnectTimeout=10 -p 2222 ghls@100.120.152.1 \
  "wsl -d ubuntu2404 -e bash -lc '<linux 命令>'"
```

- 涉及唤醒 WSL 的调用用 `timeout 180`（冷唤醒有秒级开销），纯宿主只读命令 `timeout 120` 足够。
- 发行版名固定为 **`ubuntu2404`**。
- `wsl -e` 透传的**命令输出是 UTF-8**，直接读即可；只有 **wsl.exe 自身的消息**（`wsl -l -v`、错误提示）是 **UTF-16LE**，
  需要 `iconv -f UTF-16LE -t UTF-8`。宿主 PowerShell/cmd 的中文输出是 **GBK**，需要 `iconv -f GBK -t UTF-8`。
  三种编码混在一条命令里会互相污染，**一条命令只取一种**。

### 1.2 引号纪律（踩过坑，必须照做）

PowerShell 在给原生 exe（`wsl.exe`）拼命令行时会**吃掉/错拆嵌套的双引号**，
且 PowerShell **单引号字符串里的 `\"` 是字面反斜杠加引号**（不是转义）。两者叠加的后果是：
命令被截断成半截，bash 报语法错误后**整条静默返回空输出**——看起来像"连不上"，其实是引号被吃了。

三条可用姿势，按复杂度递增：

1. **无嵌套引号**：`wsl -d ubuntu2404 -e bash -lc '<不含引号的命令>'`（本地 zsh 用双引号包整串）。
2. **命令写进本地文件再展开**：把整条远端命令行存成文本文件，调用时 `ssh ... "$(cat /path/cmd.txt)"`。
   避开了本地 zsh 这一层的二次转义。
3. **base64 载荷（推荐，任意复杂脚本都稳）**：本地把脚本 `base64 -w0`，远端解码落盘再执行。
   base64 字符集只有 `[A-Za-z0-9+/=]`，PowerShell 和 bash 都不会动它：

   ```bash
   B64=$(base64 -w0 local_script.sh)
   ssh ... "wsl -d ubuntu2404 -e bash -lc 'echo $B64 | base64 -d > /home/ghls/s.sh; chmod +x /home/ghls/s.sh; md5sum /home/ghls/s.sh'"
   ```
   落盘后**必须比对 md5** 再执行。Batch 4 的心跳与内存试验脚本都走这条路。

另有一条独立的坑：`tmux new -d` 起的会话会**继承 ssh 的 stdout**，导致 ssh 通道不关闭、命令看似挂死。
起后台会话一律加重定向：`tmux new -d -s <name> <cmd> </dev/null >/dev/null 2>&1`。

### 1.3 2223 直登：attach-state，不是故障

WSL 内 sshd 监听 **2223**（key-only，`permitrootlogin no`，`passwordauthentication no`，开机自启走 `ssh.service`）。
但 `ssh -p 2223 ghls@100.120.152.1` **只在宿主有活着的 `wsl.exe` 会话挂着时才通**：

| 场景 | 结果 |
|---|---|
| 有 `wsl -e` 会话挂着 | 通（多次 3/3） |
| 最后一个 `wsl.exe` 会话退出约 35 秒后 | `Connection timed out` |
| VM 明明活着（boot_id 不变、uptime 递增）但无会话挂着 | 仍然 timed out |

**这与防火墙无关，也与 VM 是否关机无关。** Hyper-V 放行规则 `WSL2-sshd-2223` 已在 Batch 3 实测证明**不必要并删除**
（删除后持会话态 3/3 通），宿主现在**没有**任何为 WSL 新增的防火墙规则或 portproxy。
证据：`evidence/batch3/batch3_manifest.txt` STEP 0d 的完整观测集。

**结论：2223 不是主入口，是便利入口。** 使用窗口 = 你已经通过 2222 起了一个长会话（例如 `wsl -e sleep 600`）时，
可以在旁边开 2223 直连做交互调试。**不要**为了修 2223 去碰防火墙——那是已排除的错误方向，且 `*-NetFirewall*` 全族在本机是分钟级毒药（见 ⑥）。

### 1.4 宿主重启后的唤醒流程

WSL 服务是 `Automatic` 启动，但 **VM 不会自己起**，需要一次 `wsl.exe` 调用把它拉起来：

1. `timeout 180 ssh ... -p 2222 "wsl -d ubuntu2404 -e bash -lc 'uptime -s; cat /proc/sys/kernel/random/boot_id'"`
   —— 这一条同时完成唤醒与取证。
2. 确认 sshd 自启：`wsl -d ubuntu2404 -e bash -lc 'sudo systemctl is-active ssh'` → `active`。
   （**注意**：以 `ghls` 身份不加 `sudo` 调 `systemctl` 会报 `Failed to connect to bus`，
   那是 systemd **user** session 的噪音，不是服务故障；冷唤醒后约 20 秒自行稳定。）
3. 之后 VM 会常驻（见 ②），无需反复唤醒。
4. **重启同时是 H9 锚的触发时机**：登录后按 §5.3 的三条验收跑一遍，通过了才算"断线无感"到手。

宿主重启同时会中断 Wind 终端与 market monitor，**Wind 需人工登录**才恢复——重启窗口必须由用户拍板。

---

## ② 资源配置

宿主文件 `C:\Users\ghls\.wslconfig`（ASCII，CRLF，无 BOM，2229 B，
sha256 `ae3979b401ffc0d44cd40a37c58350a9141ef409df8807341d0671523cc235cc`）。
字节级副本存 `evidence/batch3/wslconfig.after`（改前副本 `wslconfig.before`）。

| 键 | 段 | 现值 | WSL 内实测 | 调参说明 |
|---|---|---|---|---|
| `memory` | `[wsl2]` | `100GB` | `free -g` total = **98 GiB** | 上限而非预留，WSL 2.0+ 会回收空闲页。宿主若开始换页就降到 `80GB`。 |
| `processors` | `[wsl2]` | `30` | `nproc` = **30** | 32 核减 2，留给 Wind/monitor/360。 |
| `swap` | `[wsl2]` | `16GB` | `/dev/sdc` 16 GiB | 高于内存上限的缓冲，让瞬时尖峰换页而不是 OOM 掐掉长跑。vhdx 落 C:。 |
| `networkingMode` | `[wsl2]` | `mirrored` | `eth0 = 100.120.152.1/32` | 直接镜像宿主 Tailscale 地址。回退值 `nat`（本机未触发回退条件）。 |
| `vmIdleTimeout` | `[wsl2]` | `604800000`（7 天） | boot_id 跨长时间零接触不变 | **本部署的关键改动**，见下。 |
| `hostAddressLoopback` | `[experimental]` | `true` | 经 `100.120.152.1:8080` 可达宿主 Wind 网关 | **必须写在 `[experimental]`**，写进 `[wsl2]` 会被静默忽略。 |

### 2.1 vmIdleTimeout 的取舍（本部署最重要的一条）

WSL 默认 `vmIdleTimeout=60000`（60 秒）：**最后一个宿主 `wsl.exe` 会话退出约 60 秒后，整个 VM 被关掉。**
实测中 VM 内的 tmux 会话、后台 `sleep`、乃至一条活跃的入站 ssh 连接**都阻止不了它**
（Batch 2 实测：一条要跑 170 秒的 2223 命令在第 161 秒被远端掐断）。
**在默认值下，"ssh 进去开 tmux 跑长任务"这条常规路子根本走不通。**

代价：VM 常驻不退，空闲态 vmmem 约 **3.1–3.4 GB** 常占。在这台同时跑 Wind + monitor 的生产机上，
这是用户 2026-08-10 明确拍板接受的取舍。

**回退到 60 秒**：删掉 `[wsl2]` 段里 `vmIdleTimeout=604800000` 这一行 → `wsl --shutdown` → 下次唤醒生效。
（回退后长跑能力随即消失，只适合"暂时把内存还给宿主"的场景。）

> ⚠️ **必要但不充分。** `vmIdleTimeout` 只保住 VM（内核）。发行版实例仍会在最后一个 `wsl.exe`
> 会话退出后约 8–15 秒被拆掉，把里面的 tmux / 后台进程一并带走。**长跑另需"锚"，见 ⑤。**

### 2.2 改配置的生效方式

改 `.wslconfig` 后必须 `wsl --shutdown`，并遵守官方的 "8 second rule"（等 VM 完全停止再拉起）。
`wsl --shutdown` **只影响 WSL VM，不碰宿主**，可以随时执行。

---

## ③ 文件通道与镜像

### 3.1 文件通道（唯一稳定路径）

```
开发机  --scp -P 2222-->  D:\deploy_stage\wsl2\  --WSL 内 cp-->  /home/ghls/...
```

```bash
scp -P 2222 <local_file> ghls@100.120.152.1:D:/deploy_stage/wsl2/
ssh ... -p 2222 "wsl -d ubuntu2404 -e bash -lc 'cp /mnt/d/deploy_stage/wsl2/<f> /home/ghls/ && sha256sum /home/ghls/<f>'"
```

- `D:\deploy_stage\wsl2\` 是**宿主写入白名单里唯一的目录**（另加 `C:\Users\ghls\.wslconfig` 与 `D:\wsl\`）。
- 实测吞吐约 5.8 MB/s；122 MB 载荷 22 秒，391 MB 约 70 秒。
- **scp 不做 CRLF 转换**，LF 脚本原样过（已用 md5 逐字节验证）。
- 判定完整性只认 **字节数 / md5 / sha256**；不要信被多层引号包裹的 `grep -c` 结果（Batch 2 曾因此误判 CRLF 污染）。

### 3.2 镜像源（都不是默认值，有原因）

| 用途 | 现用源 | 原因 |
|---|---|---|
| apt | `https://mirrors.aliyun.com/ubuntu/`（noble） | TUNA 封了本机 IP，见下 |
| pip | `https://mirrors.aliyun.com/pypi/simple/` | 同上 |

**TUNA 封 IP 始末**：Batch 2 首次 `apt-get update` 以 65 路并发在 8 秒内抓了 39.2 MB（5.1 MB/s），
触发 TUNA 的滥用限速，客户端 IP 被封；随后连 metadata 都 403。
判定"是 TUNA 封禁而非 360 拦截"的四条独立证据：响应头 `X-TUNA-MIRROR-ID: nanomirrors` + TUNA 品牌四语拒绝页、
**HTTPS 同样 403**（本地中间人注入改不了 TLS 内容）、同一时刻 Aliyun 与 archive.ubuntu.com 均返回 206、
刚成功的 metadata URL 也转 403（典型按 IP 封）。
处置是**换源而非重试**（滚动重试会加深封禁），并把 apt 调成礼貌客户端
`Acquire::Queue-Mode=host` + `Pipeline-Depth=0`。
原 sources 备份在 WSL 内 `/etc/apt/sources.list.d/ubuntu.sources.bak.20260810T085843Z`，
封禁过期后想换回 TUNA 直接改 URIs 即可。

> 注意这与开发机的记忆项相反：**开发机**上 pip 走清华最快、走代理必卡；
> **本执行机**上清华封 IP、必须走阿里云。两台机器的结论不要互相套用。

---

## ④ 环境清单

| 项 | 值 |
|---|---|
| 发行版 | Ubuntu 24.04.4 LTS（noble），WSL 名 `ubuntu2404`，VHDX 落 `D:\wsl\ubuntu2404` |
| WSL runtime | 2.7.11.0（`C:\Program Files\WSL`，服务 `WslService` Automatic/Running） |
| 默认用户 | `ghls`（uid 1000，sudo NOPASSWD），`/etc/wsl.conf` 含 `[boot] systemd=true` + `[user] default=ghls` |
| Python | **`/opt/python3.13.9/bin/python3`** → `Python 3.13.9`（python-build-standalone 20251120，与开发机/Windows 原生部署逐字对齐） |
| 仓库 | **`/home/ghls/style_timing_signal`**，HEAD `f9f3a673dacf1a6961932bd7ca6e50feb3c6926e`，branch `main`，tracked 259 文件，工作树干净 |
| venv | **`/home/ghls/style_timing_signal/.venv`**，43 包，pip 26.2.1 |
| 平台闸门 | `.venv/bin/python -m pytest -q -p no:cacheprovider` → **1043 passed, 18 warnings**（WSL 145s / 开发机 674s，**4.6×**） |
| 真实负载 | B3 preflight（`--data-end 2023-12-31`）→ wall **672 s** / peak RSS **18.23 GiB**（同机 Windows 原生基线 1509 s / 17.04 GiB，见 4.3） |
| 工具 | tmux 3.4、git 2.43.0、OpenSSH 9.6p1 |

### 4.1 依赖 lock（正式引用）

**`/home/elfbob/claude-code/deploy_backups/2026-08-10-wsl2/evidence/batch3/wsl_requirements.txt`**
是本环境的**正式依赖 lock**（43 个 pin，`pip freeze` 逐字节可复现）。重建环境时：

```bash
/opt/python3.13.9/bin/python3 -m venv ~/style_timing_signal/.venv
~/style_timing_signal/.venv/bin/pip install -i https://mirrors.aliyun.com/pypi/simple/ -r wsl_requirements.txt
```

**为什么不是开发机的 `pip freeze`**：开发机跑在 Anaconda base 环境里，其 freeze 的 160 行中有 66 行是
`name @ file:///croot/...` 本地 conda 构建路径与 `-e git+ssh://` 可编辑安装，**在任何别的机器上都装不上**
（实测 `OSError [Errno 2]`，留档 `evidence/batch3/devfreeze_attempt.log`）。
本 lock 是按仓库 import 闭包推导 + 已验收 Windows 原生部署包集交叉核对得出的，
QA 独立验证：**相对已验收生产包集缺包 = 0（严格超集）**，且历史暗雷 `scipy`（全仓无静态 import 但运行期必需）已正确纳入。

> 已知不覆盖：`xlwings`（仅 `archive/对比/*.py` 使用，Windows Excel 自动化，已验收的 Windows 原生部署同样没有，不参与 B3 主链路）。
> 首次真实开跑若出现 `ImportError`，按缺什么补什么即可，不构成部署阻断。

### 4.2 仓库里没带过来的东西（gitignored 或未跟踪空目录，需要时手工补；注意 `backtest/output/b3`、`data_fixes/2026-07-*` 实测并非 gitignored，只是未被跟踪——落文件前别当它们被忽略）

`config/settings.yaml`、`output/phase1_diff/`、`output/style_basket/cache/`、`output/style_basket/b3/`、
`backtest/output/b3/`、`data_fixes/2026-07-*/`。
**1043 项测试全都不需要它们**（`test_common_config.py` 在 `tmp_path` 里自建 settings.yaml）。
跑真实 B3 负载才需要，走 ③ 的文件通道补进去。

补齐 B3 真实负载的最小动作（Batch 4 实操，可照抄）：

```bash
# 1) 配置（228 B，含 DB 口令与 Wind token）
scp -P 2222 config/settings.yaml ghls@100.120.152.1:D:/deploy_stage/wsl2/settings.yaml
ssh ... -p 2222 "wsl -d ubuntu2404 -e bash -lc 'cp /mnt/d/deploy_stage/wsl2/settings.yaml \
  /home/ghls/style_timing_signal/config/settings.yaml; md5sum /home/ghls/style_timing_signal/config/settings.yaml'"

# 2) 空目录（只建目录，不搬 176 MB 的 cache 内容）
mkdir -p output/style_basket/cache output/style_basket/b3 backtest/output/b3
```

`settings.yaml` 指向 Debian 主库 `100.65.111.79:5432` 与 Wind 网关 `http://100.120.152.1:8080`，
两者从 WSL 内均已实测可达（Batch 2 的 PG 应用层握手 + Batch 4 全程 `GW8080=OPEN`）。

> ⚠️ **凭据卫生**：该文件是明文口令。传输后它会在宿主 `D:\deploy_stage\wsl2\settings.yaml`
> 留下一份副本，**用完必须删掉**（WSL 内那份才是运行时需要的）。
> 本次部署留下的那份已于 2026-08-11 删除（台账 H8，证据 `evidence/post-accept/settings-yaml-removal.txt`；
> 2026-08-12 复核 `Test-Path` = `False`）。**以后每次走这条通道补配置，都要在用完后重复这一步。**

### 4.3 真实负载实测（B3 preflight）与容量口径

```bash
cd /home/ghls/style_timing_signal
/usr/bin/time -v .venv/bin/python -m signals.style_basket.b3_build \
  --stage preflight --data-end 2023-12-31 --output-dir /home/ghls/b3_wsl_probe/research
```

| 指标 | Windows 原生（同一台物理机） | WSL2 | 差异 |
|---|---|---|---|
| wall | 1509 s | **672 s** | **快 2.25×** |
| peak RSS | 17.04 GiB | **18.23 GiB** | **高 7.0%** |

- 该跑 `Percent of CPU = 50%`，约一半 wall 在等 DB I/O；纯计算段的加速比高于 2.25×
  （与平台闸门 pytest 的 4.6× 同向）。
- **容量口径：Linux 侧峰值内存比 Windows 高约 7%，规划时按 Windows 实测峰值 × 1.1 留余量。**
  本机 98 GiB 配额对此类负载绰绰有余。
- 该跑退出码 2、`status=DATA_BLOCKED`、`reason_code=DATA_CONTRACT` —— 这是 **B3 主线
  已知的 CLOSE 口径遗留**，不是环境故障。环境侧的结论是"真实负载能完整跑完并正常落盘产物"。

---

## ⑤ 长跑姿势

> ⚠️ **先读这一段，否则你的长任务会在断线后 10 秒内被静默杀掉。**

### 5.1 硬事实：发行版实例会被拆，`vmIdleTimeout` 管不着

`vmIdleTimeout=604800000` 保住的是 **VM（内核）**。它保不住 **发行版实例**。
**最后一个宿主 `wsl.exe` 会话退出后约 8–15 秒，发行版内的全部 Linux 进程被连根拔掉**——
tmux 会话、`setsid nohup` 的后台进程、sshd，全部一起死。Batch 4 实测：

| 场景 | 存活 |
|---|---|
| 无锚，30 秒心跳，零接触 31 分钟 | **只跳了 1 次**（起跑那一跳），tmux server 消失 |
| 无锚，5 秒心跳，tmux 与 `setsid nohup` 双记录器 | 各 4 跳，**同一秒一起停摆**，会话退出后约 11 秒 |
| **有锚**（宿主挂着 `wsl -e sleep 240`），5 秒心跳 | **51 跳零断点**，最大间隔 6 秒；锚释放后 8 秒才死 |

**`setsid nohup` 也救不了**——死的不是进程树，是整个发行版实例。

**为什么以前没发现**：`boot_id` 与 `/proc/uptime` 都是**内核级**的量，
WSL2 所有发行版共用一个内核，所以发行版被拆掉重建时**这两个量纹丝不动**。
Batch 2/3 用 boot_id 判定"VM 常驻"是对的，但它对本失效模式是**盲的**。

**正确判据**：`ps -p 1 -o etimes=`（systemd 已存活秒数）——数值小于你的任务时长 = 实例被重建过。
实测对照：内核 uptime 5098 秒（连续 85 分钟），而 systemd 是 **11 秒前**才起来的。
**别用 `lstart=`（QA F-2）**：本机 WSL 内核时钟比墙钟**慢约 2.7%**，`lstart` 的表观时间每小时前漂
约 100 秒，挂锚约 19 分钟后就会让表观 systemd 时间反超任务起始时间、**假报"任务已死"**；
`etimes` 与心跳日志不受此漂移影响。（这个 2.7% 同样意味着 WSL 内 `sleep N` 实际比墙钟 N 秒更长。）

> 这同时解释了 ①1.3 的 2223 attach-state：不是端口被挡，是 **sshd 随发行版一起被拆了**。两者是同一个机制。

### 5.2 起长跑（当前唯一可靠的姿势）

**必须先有锚。** 锚 = 宿主上任何一个长命 `wsl.exe` 进程。

```bash
# 0. 起锚（当前形态：由发起方从开发机后台挂一条。断线即失锚，见 5.3）
ssh ... -p 2222 "wsl -d ubuntu2404 -e sleep 86400" &

# 1. 投递脚本（base64 载荷，见 1.2），落盘后比对 md5
# 2. 起会话（tmux new -d 必须重定向，否则 ssh 通道不关、命令零输出看似挂死）
ssh ... -p 2222 "wsl -d ubuntu2404 -e bash -lc 'tmux new -d -s <job> /home/ghls/<script>.sh </dev/null >/dev/null 2>&1'"

# 3. 回来查（每次 wsl -e 调用本身也短暂充当锚）
ssh ... -p 2222 "wsl -d ubuntu2404 -e bash -lc 'ps -p 1 -o etimes=; tmux ls </dev/null; tail -5 /home/ghls/<job>.log'"
```

- 查进度时**先看 `ps -p 1 -o etimes=`**：如果 systemd 存活秒数比任务已运行时长还小，实例被重建过、任务已经死了，别再看日志被骗（勿用 `lstart=`，见 5.1 的时钟漂移）。
- 让长跑脚本**自己写带时间戳的心跳日志**——这是唯一不会骗人的存活证据。
- 取峰值内存用 `/usr/bin/time -v <cmd>` 包裹，读 `Maximum resident set size`。
- `tmux ls` 实测不加重定向也正常；本文统一加 `</dev/null` 只是把纪律拉齐。

### 5.3 抗断线的锚：**已部署，但尚未生效**（台账 H9）

5.2 的锚由开发机的 ssh 持有，**开发机一断线锚就没了**，等于没有"断线无感"。
宿主侧抗断线的锚已于 2026-08-11 用户批准落地，形态是**启动文件夹脚本**（不走被 360 封过的 schtasks / WMI）：

```
%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\wsl2-anchor.vbs   (706 B, 2026-08-11 18:33)
  -> WScript.Shell.Run "C:\Windows\System32\wsl.exe -d ubuntu2404 -e sleep infinity", 0, False
```

副本与说明见 `evidence/post-accept/wsl2-anchor.vbs`。代价 = 发行版常驻及其内存占用；
回滚 = 从启动文件夹删掉该文件，再 `wsl --shutdown`。

> 🚧 **它到现在一次都没跑过。** 宿主自 2026-08-10 16:19 起是同一个登录会话，而脚本是 8-11 18:33 才放进去的，
> **启动文件夹只在登录时触发**。2026-08-12 实测 `Get-Process wsl` 为空 = 当前无锚。
> **在下一次登录事件（注销重登 / 重启）之前，本环境仍然只支持"有人值守 / 发起方保持连接"的长跑。**

**不要试图从 ssh 远端把它激活**——已实测是死路（2026-08-12，证据 `evidence/post-accept/anchor-activation-attempt.txt`）：
`wscript` 拉起来的 `wsl.exe` 在同一 ssh 会话内能稳活 ≥90 s，但**会话一断就随之被杀**（< 97 s 内消失）。
根因是 Windows OpenSSH 把会话内派生的进程放进一个 job object，会话关闭时整组终结；
`WScript.Shell.Run` 的分离式启动逃不出去——与 Batch 2 实测 `Start-Process wsl … sleep` **不留存**是同一类失效。
登录触发路径的父进程是交互会话的 explorer/userinit，不在该 job object 内，因此**只有真实登录才能验收**。

**下次登录后的验收（三条，缺一不可）**：

```powershell
# 1) 锚起来了，且起始时刻 ≈ 登录时刻
Get-Process wsl | Select-Object Id,StartTime
# 2) 断开所有 ssh，隔 >=5 分钟再连——它必须还在（这才证明不依赖远端会话）
# 3) 发行版实例真的常驻（勿用 lstart=，内核时钟慢 2.7%，见 5.1）
wsl -d ubuntu2404 -e bash -lc 'ps -p 1 -o etimes='
```

三条全过之前，**别把 H9 当成已生效**，也别据此安排无人值守长跑。

---

## ⑥ 已知宿主怪癖

### 6.1 PowerShell / .NET 内核锁风暴（已挂案，待 IT）

**现象**：本机所有 PowerShell / .NET 进程共享一个内核串行点。新开 PowerShell 首次调用
`[System.IO.File]::Exists()` 实测 **18.9–70 秒**（同功能 cmd 原生命令 0.8 秒）；
`*-NetFirewall*` 类 cmdlet **280 秒以上甚至不返回**。cmd、Python、WSL 内 Linux 进程**完全不受影响**。

**已测量的性质**：卡住进程累计 CPU 的 98.8–99.0% 在内核态，线程 `ThreadWaitReason=27 (WrResource)`（等内核 ERESOURCE 读写锁）；
六个互不相关的卡住进程在同一 144 秒窗口的 IOCTL 增量彼此相差 <0.03%（单一内核串行点按人头均分）；
对不存在的盘符做 `File.Exists` 同样卡 70 秒且零磁盘 I/O。**不是资源瓶颈，是锁。**
最强嫌疑是 360（文件过滤驱动 360Box64/360FsFlt/360AvFlt 各挂 10 个实例，驱动链接日期 2016–2022，跑在 2026 年的 Build 26200 内核上）。
**已排除**：磁盘/内存/CPU、SearchIndexer、ConPTY、PSReadLine/profile、HVCI、WSL 本身（现象早于安装）、网络。

**IT 证据包**：`/home/elfbob/claude-code/deploy_backups/2026-08-10-wsl2/evidence/diagnostics/it-escalation-package.md`
（配套三轮诊断报告 `2026-08-10-post-reboot-lag.md` / `-round2.md` / `-round3.md`、探针清理记录 `cleanup-probes.md`）。

### 6.2 宿主侧命令快慢路径清单

| 快路径（秒级，随便用） | 慢路径/毒药（禁用或走文件轮询） |
|---|---|
| `cmd /c dir` / `type` / `findstr` / `tasklist` / `netstat` | **`*-NetFirewall*` 全族**（分钟级，可能永不返回） |
| `wsl --status` / `wsl -l -v` / `wsl -d ... -e ...` | **`[System.IO.*]` 全族**（19–70 秒） |
| 单条 `Get-CimInstance Win32_OperatingSystem`（可加 `-OperationTimeoutSec`） | 复杂 PS 管道（计算属性 + `Format-Table`，>120 s） |
| 单条 `Get-CimInstance Win32_Process -Filter "ProcessId=<pid>"` | 裸 `sc`、`Win32_Product` |
| `certutil -hashfile` | |

**慢命令的正确姿势**（不得已必须跑时）：写成 `.ps1` → `scp` 到 `D:\deploy_stage\wsl2\` → ssh 只负责发起 →
结果 `Out-File` 到同目录 `.txt` → **另开会话 `cmd /c type` 轮询结果文件**。
脚本必须**幂等**（先 `Get` 判存在再 `New`），因为你很可能因超时而重跑。
样板：`evidence/batch2/fw_probe.ps1` / `fw_apply.ps1` / `fw_apply2.ps1` / `cleanup_fw.ps1`（脚本与其 `.txt` 结果配对留档）。
Batch 3 的 `fw_check3.ps1` / `fw_remove.ps1` 只留在宿主 `D:\deploy_stage\wsl2\`（未回收），其结果 `.txt` 已归档到 `evidence/batch3/`。

> 血的教训：Batch 2 因为在 ssh 前台等 `New-NetFirewallHyperVRule`、被 120 秒 timeout 掐断且零输出，
> **误判"建规则失败"，实际早已成功**，由此引出一整轮错误的"mirrored 单向"结论。

### 6.3 其他

- **在这台机器上探 MTU，上限是 1280（Tailscale eth0），不是 1500。**
  `ping -M do -s 1372`（=1400 B）必然本地失败（`+N errors` 是本地错误计数，不是网络丢包），
  那是探针设计错误而非 MTU 黑洞。正确判据：`ping -M do -s 1252`（=1280 B）通即无黑洞。
- **每次 `wsl -e` 都会打印** `wsl: Failed to start the systemd user session for 'ghls'`。
  无害噪音（`systemctl is-system-running` = running，1043/1043 测试全过）。
- **`.cmd` 脚本下发**（若确需）：CRLF + ASCII + 绝对路径，三者缺一不可。
- 360 在本部署全程（Batch 1–4）**零拦截迹象**：无进程被杀、无文件被隔离、无命令被拒。
  6.1 的慢是既有病态，不是拦截。

---

## ⑦ 回退（逐条，完全可复原）

**原生 Windows 部署 `D:\style_timing_signal` 自始至终原样保留、未被触碰。**
下面每条都可独立执行，顺序按"影响从小到大"。

| # | 回退动作 | 命令 | 说明 |
|---|---|---|---|
| 1 | 撤掉开机自启锚（H9） | 从启动文件夹删 `wsl2-anchor.vbs`，再 `wsl --shutdown` | 只影响"断线无感"，不影响其余任何能力；副本留在 `evidence/post-accept/`。 |
| 2 | 停 WSL VM（临时还内存） | `wsl --shutdown` | 只影响 VM，随时可做，下次 `wsl -e` 自动拉起。 |
| 3 | 恢复 60 秒空闲关机 | 删 `C:\Users\ghls\.wslconfig` 中 `vmIdleTimeout=604800000` 一行，再 `wsl --shutdown` | 改前副本 `evidence/batch3/wslconfig.before`（2204 B）。 |
| 4 | 整份 `.wslconfig` 复原 | 把 `wslconfig.before` scp 回 `C:\Users\ghls\.wslconfig` | 该文件在本部署前**不存在**，也可直接删除。 |
| 5 | 删除发行版（连数据） | `wsl --unregister ubuntu2404` | **不可逆**，会删掉 `/opt/python3.13.9`、`/home/ghls/style_timing_signal` 及 `.venv`。 |
| 6 | 删 VHDX 残留 | `cmd /c rmdir /s /q D:\wsl\ubuntu2404` | `--unregister` 后若目录仍在。 |
| 7 | 清 staging | `cmd /c rmdir /s /q D:\deploy_stage\wsl2` | 36+ 个安装/证据文件，删前确认已归档到 `deploy_backups/`。**注意该目录已被另一条 B3 model-calendar 工作线在用**（H8 记录），不是本部署独占。 |
| 8 | 卸载 WSL runtime | `msiexec /x D:\deploy_stage\wsl2\wsl.2.7.11.0.x64.msi /qn /norestart` | 需在第 7 步之前做（要用到 MSI 文件）。 |
| 9 | 关闭虚拟机平台功能 | `dism /online /disable-feature /featurename:VirtualMachinePlatform /norestart` | **需重启生效**，必须走用户拍板的重启窗口。本机 VBS 已在运行，理论上不受影响，但仍属系统级操作。 |

> ⚠️ 第 9 行的 "VBS" 指 Windows 的 **Virtualization-Based Security**，与 H9 那个 `wsl2-anchor.vbs`
> （VBScript 脚本）毫无关系，别看串。

**防火墙无需回退**：Hyper-V 规则 `WSL2-sshd-2223` 已在 Batch 3 删除且证明不必要；
本部署**从未**创建经典 Windows 防火墙规则或 portproxy 条目（`netsh` 双双核实为空）。

---

## ⑧ 生产共存

WSL 与宿主生产负载共用一台机器。**动手前先识别，永远零杀零扰。**

### 8.1 Wind 网关

```bash
ssh ... -p 2222 'cmd /c tasklist /fi "pid eq 14720"'          # 进程存活
ssh ... -p 2222 "wsl -d ubuntu2404 -e bash -lc '</dev/tcp/127.0.0.1/8080 && echo GW_OPEN'"   # 端口从 WSL 侧可达
```
- `powershell.exe` pid **14720**，命令行 `D:\wind_gateway\start_gateway_interactive`，监听 **:8080**。
- 健康判据：HTTP 请求回 `HTTP/1.1 404 Not Found`（网关在跑但路径不存在，属正常）。
- **pid 会随宿主重启变化**，以命令行而非 pid 为准。

### 8.2 B3 正式批（识别方法，不要沿用旧 pid）

历史上这个进程的标识**变过两次**（8052 → 27936），**每次动手前必须重新识别**：

```bash
# 1) 拉回本地筛（tasklist 输出是 GBK）
ssh ... -p 2222 'tasklist /fo csv' | iconv -f GBK -t UTF-8 | grep -i python
#    关注 Services 会话 + 内存占用达 GB 级的那个

# 2) 取命令行确认身份（单条 CIM，快路径）
ssh ... -p 2222 'Get-CimInstance Win32_Process -Filter "ProcessId=<pid>" | Select-Object -ExpandProperty CommandLine'
```

**判据**：命令行含 `signals.style_basket.b3_build` 或 `run_guarded_b3.py`，
且 `--output-dir` 指向 `D:\style_timing_signal\data_fixes\...` → 就是 B3 正式批。
2026-08-10 收官时的实例：pid **27936**，Services 会话，
`D:\style_timing_signal\.venv\Scripts\python.exe -m signals.style_basket.b3_build --stage all --data-end 2023-12-31 --output-dir D:\style_timing_signal\data_fixes\2026-08-01-b3-wind-share-capital\run\research`。

B3 长跑走**计划任务**（Services 会话即由此而来；WMI 方式被 360 拒，是既有约束）。

### 8.3 market monitor

采集进程为 Console 会话下的多个 `python.exe`。宿主重启会中断日更，重启前需与用户确认无正在写库的长事务。

### 8.4 共存纪律

- **禁止**任何 `taskkill` / `Stop-Process` 触及上述进程。清理只杀自己这一批起的进程，并显式跳过白名单 pid。
- 跑重负载前先取宿主空闲内存：
  `ssh ... -p 2222 'Get-CimInstance Win32_OperatingSystem | Select-Object -ExpandProperty FreePhysicalMemory'`（单位 KB）。
  经验阈值：WSL 侧大内存作业期间宿主空闲**不低于 20 GB**。
- WSL 的 vmmem 进程名是 **`vmmemWSL`**（不是 `vmmem`），用 `tasklist /fi "imagename eq vmmemWSL"` 观察其工作集。
- 任何 360 拦截迹象（进程被杀 / 文件被隔离 / 命令被拒）→ **立即停下如实报告**，不自行绕过、不重试轰炸。

---

## 证据索引

全部原始证据在 `/home/elfbob/claude-code/deploy_backups/2026-08-10-wsl2/evidence/`，
索引与用途见同目录上一级的 `manifest.json`（`evidence_index` 段）。

- `batch0/` – `batch4/`、`diagnostics/`：建设期证据（Batch 0–4 双审，含各批 `qa_review.md` 与 `adjudication.md`）。
- `post-accept/`：**验收之后**发生的宿主变更与观测（H8 删除 staging 明文口令副本、H9 启动文件夹锚及其
  2026-08-12 激活尝试的失败取证）。这部分不属于 Batch 0–4 的双审范围，按变更逐条留证。
