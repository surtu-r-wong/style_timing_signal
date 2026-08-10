# WSL2 执行环境搭建实施计划（DESKTOP-P7MGEIR）

> **For Claude:** 本计划由控制器会话按批次派发：实现与独立质量审查各派 opus 子代理，Tier 1 双审（独立 QA + 控制器裁决），每批次向用户报三行账（①本批做了什么 ②验证/QA 结论 ③遗留与下一步）。控制器不亲手跑实现命令。

**Goal:** 在 Windows 执行机（128G）上建立 WSL2 Ubuntu 环境，让 B3 / stock_selector 重活在 Linux 工具链下长跑（tmux 断线无感），根治 PowerShell 指令坑与 360 渐进封锁长跑进程的死结；Wind 终端、Wind 网关、market monitor 全部留在 Windows 宿主，不动。

**Architecture:** 离线导入 Ubuntu 24.04（`wsl --import`，VHDX 落 D:），`.wslconfig` 分约 100G 内存给 WSL、留约 27G 给宿主。网络首选 `networkingMode=mirrored`（复用宿主已验证的 Tailscale 路径），不通回退 NAT。仓库部署复用既有 git bundle + sha256 manifest 纪律。

**Tech Stack:** WSL2、Ubuntu 24.04 LTS、Python 3.13.9（与双机既有版本逐字对齐）、tmux、OpenSSH、pytest 平台闸门。

---

## 已确认的机器事实（2026-08-10 只读探测）

- Windows 11 **专业版** Build 26200；**VBS 正在运行 ⇒ Hyper-V 监控程序已在底层运行**，固件虚拟化已开，无需 BIOS 操作。
- WSL 未安装（`wsl --status` 返回未安装提示）。
- 开发机与 Windows 原生部署 Python 均为 **3.13.9**。
- 既有备份：`/home/elfbob/claude-code/deploy_backups/2026-08-06-b3-windows/`（bundle、wheels、安装包、`windows_deployment.json`、`make_deployment_manifest.py`）。
- 磁盘：C: 剩 223G，D: 剩 **1791.5G**（VHDX、镜像、deploy_stage 全放 D: 无压力）。
- 实测：PowerShell 经 ssh 跑复杂管道（`Get-PSDrive`+计算属性+`Format-Table`）耗时 >120s 才返回；简单命令（`wsl --status`）秒回。

## 子代理硬规（每个批次的实现与 QA 代理都必须遵守）

1. **有界输出**：一切命令输出必须 `head -N` / `wc` 限幅；禁 `cat` 大文件、禁无界 `git log -p`。（历史事故：无界输出撑爆内存丢过整段进度。）
2. **远程命令简式化**：`ssh -o BatchMode=yes -o ConnectTimeout=8 -p 2222 ghls@100.120.152.1`，外层包 `timeout 60`。PowerShell 不吃 `&&`（用 `;`）、把 stderr 正常输出当错误流、内联引号/`*` 会被吃——**复杂逻辑一律先 scp 脚本文件再执行**；优先 cmd 兼容简式命令。zsh 本地侧注意 `=` 开头裸词会触发 zsh 展开（实测 `echo ===` 炸）。
3. **装软件口径**：安装包一律先下到开发机、sha256 与上游官方值比对、入 `deploy_backups/2026-08-10-wsl2/`、写 manifest，再 scp 过去静默安装。不用 winget。GitHub 直连在 Windows 机是黑洞（开发机可下，卡代理时 `env -u HTTP_PROXY -u HTTPS_PROXY` 直连或走清华/华为镜像）。
4. **触产红线**：不重启（用户拍板窗口）；不动 360、Wind 终端、market monitor、宿主既有防火墙规则（新增规则须明示记录）；Windows 侧只写 `D:\wsl\`、`D:\deploy_stage\`、`C:\Users\ghls\.wslconfig`；**任何 360 拦截 → 立即停下如实报告，绝不自行绕过或重试轰炸**。
5. **证据落盘**：每条结论必须附命令原文+输出摘录，存 `/home/elfbob/claude-code/deploy_backups/2026-08-10-wsl2/evidence/batch<N>/`。QA 代理只认证据文件，不认转述。
6. **数据库连通验证**用 Tailscale MTU 四连测（ICMP 小包 / TCP 端口 / 大包 / 应用层查询），目标 Debian 主库 `100.65.111.79:5432`。

## 批次总览与检查点

| 批次 | 内容 | 触产 | 检查点 |
|---|---|---|---|
| 0 | 开发机备料 + 真机只读预检 | 否 | — |
| 1 | 启用虚拟化功能 + 装 WSL runtime | **是** | ⛔ 批末：用户选窗口重启 + 重启后人工登 Wind 终端 |
| 2 | 导入发行版 + 内存/网络/SSH 入口 | **是** | mirrored 不通时向用户报备网络回退方案 |
| 3 | Python 3.13.9 + 仓库部署 + 平台闸门 | **是** | — |
| 4 | 长跑/内存/360 共存验收 + runbook | **是** | 收尾报告 |

---

### Batch 0：开发机备料 + 真机只读预检（不触改 Windows）

**产物目录：** `/home/elfbob/claude-code/deploy_backups/2026-08-10-wsl2/`

**Step 1 — 真机只读预检**（每条独立简式命令 + 超时）：
- `(Get-WindowsOptionalFeature -Online -FeatureName VirtualMachinePlatform).State` 与 `Microsoft-Windows-Subsystem-Linux` 同式各查一条（管理员态若拒绝，如实记录）。
- C:/D: 剩余空间：`fsutil volume diskfree D:`（cmd 兼容，避免 PS 管道挂死）。
- CPU 核数：`echo %NUMBER_OF_PROCESSORS%` 经 `cmd /c`。
- 宿主当前内存占用：`wmic OS get FreePhysicalMemory,TotalVisibleMemorySize`（只读 CIM 查询，非进程创建，此前 WMI 被拒的是 `Win32_Process::Create`；若拒绝换 `systeminfo` 截取）。
- 产出 `evidence/batch0/preflight.md`。

**Step 2 — 下载 WSL 分发物到开发机并校验**：
- Microsoft/WSL 最新稳定版 x64 MSI（GitHub Releases，开发机可达；卡代理按硬规 3 处理）。
- Ubuntu 24.04 官方 WSL 镜像（`ubuntu-24.04.x-wsl-amd64.wsl`，或 cloud-images rootfs tar.gz），sha256 与上游 `SHA256SUMS` 比对。
- CPython 3.13.9 Linux x86_64 standalone 构建（python-build-standalone release），同样校验入库。**理由：mirrored 网络 = 宿主网络，GitHub 在那台机器是黑洞，WSL 内在线装 3.13.9 大概率失败，直接备离线件。**
- 写 `manifest.json`（文件名/sha256/来源 URL/上游校验值出处），`sha256sum -c` 自检通过。

**Step 3 — 模板起草**：`.wslconfig`（`memory=100GB, processors=<核数-2>, swap=16GB, networkingMode=mirrored`）与 `wsl.conf`（`systemd=true`、默认用户）入备份目录。

**验收：** manifest 自洽；预检报告含全部原始输出；ssh 历史零写操作。
**QA 审查点：** sha256 是"与上游公布值比对"而非自算自比；预检证据真实完整；确无触改。

### Batch 1：启用功能 + 装 WSL runtime（Tier 1）

**Step 1** `scp` MSI 至 `D:\deploy_stage\wsl2\`；远端 `certutil -hashfile <msi> SHA256` 与 manifest 比对。
**Step 2** `dism /online /enable-feature /featurename:VirtualMachinePlatform /all /norestart`；若 WSL 可选功能未启用则同式启用 `Microsoft-Windows-Subsystem-Linux`。记录"需要重启"提示。DISM 被拒/被拦 → 停，报告。
**Step 3** `msiexec /i D:\deploy_stage\wsl2\<wsl.msi> /qn /norestart`；验证 `wsl --version` 输出版本号。
**Step 4 ⛔ 用户检查点**：报告后停。用户择窗重启；重启后**人工登录 Wind 终端**（网关恢复）并确认 market monitor 采集恢复，然后放行 Batch 2。

**验收：** DISM 成功+挂起重启；`wsl --version` 出实际版本。
**QA 审查点：** 未触发重启；无越权写位置；360 交互如实记录。

### Batch 2：导入发行版 + 内存/网络/SSH 入口（Tier 1）

**Step 1** 重启后确认 `wsl --status`（默认版本 2）。
**Step 2** scp Ubuntu 镜像 + `.wslconfig`（→ `C:\Users\ghls\.wslconfig`）；`wsl --import ubuntu2404 D:\wsl\ubuntu2404 <镜像> --version 2`。
**Step 3** WSL 内初始化（scp 脚本执行）：建用户、`wsl.conf`、apt 换清华/华为源、装 `openssh-server tmux git`；`wsl --shutdown` 后重进使配置生效。
**Step 4** 资源验证：`free -g` 总量 ≈ 100G；`nproc` = 核数-2。
**Step 5** 网络四连测（WSL → Debian `100.65.111.79`）+ 到开发机 + 到宿主 Wind 网关端口（mirrored 下 `localhost`）。mirrored 不通 → 改 `.wslconfig` 为 NAT 复测；NAT 下网关经宿主网关 IP。两条都记录证据。若最终需要 WSL 内装 tailscale 独立节点，先报用户再动。
**Step 6** SSH 入口：WSL 内 sshd 监听 2223；mirrored 模式下从开发机 `ssh -p 2223 ghls@100.120.152.1` 直登。需新增 Windows 防火墙放行时，规则名/命令入证据并在三行账明示。失败回退入口 = ssh 宿主后 `wsl -d ubuntu2404 -e bash -lc '…'`，同样验证可用。

**验收：** 开发机一条命令直达 WSL bash；`free -g` 达标；四连测全绿（或回退路径全绿+差异说明）。
**QA 审查点：** mirrored/NAT 决策有证据链；防火墙改动零隐瞒；`.wslconfig` 生效证据（非仅文件存在）。

### Batch 3：Python 3.13.9 + 仓库部署 + 平台闸门（Tier 1）

**Step 1** scp CPython standalone 包进 WSL，装至 `/opt/python3.13.9`，`python3 --version` 逐字 `Python 3.13.9`。
**Step 2** 仓库部署：优先 WSL 内直接 `git clone ssh://elfbob@<开发机 Tailscale IP>/home/elfbob/claude-code/style_timing_signal`（连通性 Batch 2 已证）；不通则复用 bundle scp 流。HEAD 对齐开发机当前 commit（含 `d0462d2` 两条 POSIX 断言修正之后的提交），`git rev-parse HEAD` 比对。
**Step 3** venv + 依赖：清华 PyPI 镜像安装；版本清单与开发机 `pip freeze` diff，差异入证据。
**Step 4** 跑平台闸门 pytest 全量；与开发机同 commit 基线数字比对（当前参考 1,095 passed，以当次实测为准）。

**验收：** pytest exit 0 且数字与基线一致；HEAD 一致；Python 版本逐字一致。
**QA 审查点：** 依赖 diff 逐项有解释；闸门数字与开发机同 commit 实测对照（QA 代理自己在开发机跑或查证据，不认口头）。

### Batch 4：长跑/内存/360 共存验收 + runbook（Tier 1）

**Step 1** tmux 存活试验：tmux 内起每 30s 追加心跳文件的进程 → 断开全部 ssh → ≥30 分钟后重连查心跳连续性。
**Step 2** 内存试验：WSL 内 python 分配并持有 30GB 5 分钟；同时宿主侧 `tasklist /fi "imagename eq vmmem*"`（或 vmmemWSL）看占用，确认宿主剩余 ≥20G、Wind/monitor 无异常。
**Step 3** 真实负载验收：在 WSL 跑一次 B3 preflight（与 Windows 原生第四跑对照：peak 17.04 GiB / wall 1509s）。**仅作环境验收证据，不改变 B3 主线状态、不覆盖既有产物目录。**全程 360 在岗，记录有无进程被杀/文件被隔离。
**Step 4** runbook：`docs/plans/2026-08-10-wsl2-runbook.md` —— 重启后唤醒流程（WSL 不自启，ssh 触发 `wsl -d ubuntu2404` 即拉起）、入口命令、`.wslconfig` 调参位、故障回退（原生部署仍保留为 fallback）。
**Step 5** 备份目录 manifest 终版 + 收尾三行账。

**验收：** 心跳零断点；B3 preflight 完跑、峰值/时长记录在案；360 零击杀（或如实记录+处置建议）。
**QA 审查点：** 对照数字引用双方产物原文；runbook 可被零上下文者照做。

---

## 风险与回退

- **360 拦 DISM/MSI/wsl.exe**：立即停 → 报用户 → 用户在 360 放行（与既有 venv 白名单同一决定域）。绝不代用户动杀软。
- **mirrored 网络不通/不稳**：回退 NAT；再不行 WSL 内 tailscale 独立节点（用户知情后）。MTU 黑洞按四连测手册诊断。
- **整体失败兜底**：Windows 原生部署（`D:\style_timing_signal` + 计划任务/前台 ssh 起法）原样保留，本计划不动它。
- **回执口径**：此后在 WSL 产出的回执 `platform` 将为 linux；`verify_post_write.py` 本就要求 Linux 侧执行，口径反而归一。差异入 runbook 备注。
