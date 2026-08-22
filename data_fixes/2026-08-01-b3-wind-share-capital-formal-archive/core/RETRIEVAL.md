# B3 正式跑产物取回与取证回执

**取回时间:** 2026-08-12 09:48–09:56 Asia/Shanghai
**执行机:** `DESKTOP-P7MGEIR`(`ghls@100.120.152.1:2222`)
**来源目录:** `D:\style_timing_signal\data_fixes\2026-08-01-b3-wind-share-capital\run`
**本地落点:** `/home/elfbob/claude-code/.worktrees/style_timing_signal/b3-wind-share-capital/data_fixes/2026-08-01-b3-wind-share-capital/run-windows-formal`

本次操作全程未做任何 git commit / push,未改动任何 tracked 文件;执行机侧除
新建 `D:\deploy_stage\b3_bundles\` 并写入一个 bundle 外为纯只读。

## 1. 前置核查(PASS)

| 检查项 | 期望 | 实测 | 结果 |
| --- | --- | --- | --- |
| 本地 worktree HEAD | `36eed77` | `36eed7776e70a719c05e357ba5c0b84f583300ca` | ✅ |
| 本地 worktree 分支 | `fix/b3-wind-share-capital-tail` | 同 | ✅ |
| 本地 worktree tracked 改动 | 无 | 无(untracked 仅 `run*` 目录,无 `run-windows-formal`) | ✅ |
| 执行机 HEAD | `937f66e` | `937f66e6769be3a59e0ebfcf0efdcc6b0f3372f1` | ✅ |
| 执行机分支 | `fix/b3-wind-share-capital-tail` | 同 | ✅ |
| 执行机 status | 仅 `?? .../run/` | `?? data_fixes/2026-08-01-b3-wind-share-capital/run/` | ✅ |
| 执行机存在 `36eed77` | 是 | `36eed7776e70a719c05e357ba5c0b84f583300ca` | ✅ |

## 2. Git bundle 抢救(PASS)

```text
执行机命令  git -C D:\style_timing_signal bundle create \
              D:\deploy_stage\b3_bundles\b3-formal-20260812.bundle \
              36eed77..fix/b3-wind-share-capital-tail
本地归档    /home/elfbob/claude-code/deploy_backups/2026-08-12-b3-formal-run/b3-formal-20260812.bundle
大小        63,197 bytes(两端一致)
SHA-256     a29e7d74e3245b2def980dfc1bac1085fe286a79cd8b13deccde96ebaa3ccb9a
```

执行机侧哈希用 `Get-FileHash -Algorithm SHA256` 取得,与本地 `sha256sum` 逐字符
一致(忽略大小写)。

`git bundle verify` 结果:

```text
含引用    937f66e6769be3a59e0ebfcf0efdcc6b0f3372f1 refs/heads/fix/b3-wind-share-capital-tail
需引用    36eed7776e70a719c05e357ba5c0b84f583300ca
哈希算法  sha1
判定      可以(OK)
```

## 3. 入库与提交链核对(PASS)

`git -C /home/elfbob/claude-code/style_timing_signal fetch <bundle> fix/b3-wind-share-capital-tail`
→ **fetch 这一步本身只写 `FETCH_HEAD`,不移动任何分支引用**(未给 refspec 目的端;
该分支检出在 worktree 中)。

需明确:worktree 与主仓库**共享同一 ref 库**,因此随后 §4 的快进确实改写了
`refs/heads/fix/b3-wind-share-capital-tail`(`36eed777` → `937f66e6`;ref 文件
mtime `2026-08-12 09:49:19`,reflog 记为 `merge 937f66e6…: Fast-forward`)。这是
任务授权的动作。**`main` 及其余任何分支引用自始至终未动**,主仓库工作树未被触碰。

`36eed777..937f66e6` 区间共 **14 个提交**,与交接文档对账完全吻合。归因如下:
"Commit chain" 段共列 **12 个哈希**(基线 `1426a073` + 10 个实现提交 + `9a18090`),
全部在列;第 13 个 `5e7a902` 只以全哈希出现在 "Deployment state" 散文中
(`run_manifest.code_commit`);第 14 个 `937f66e` 在交接文档中从未以哈希形式出现,
系执行机 HEAD 实测所得。三者相加 12 + 1 + 1 = 14,与区间提交数一致:

```text
937f66e docs(b3): finalize formal run handoff
5e7a902 docs(b3): record formal run handoff        <- run_manifest.code_commit
9a18090 fix(b3): scope SalG freshness to dependencies
14efbfe fix(b3): scope control grid to model calendar
bf04f4c docs(b3): align eval plan with cutoff semantics
a0f4841 fix(b3): validate eval cutoff formations
e37ee82 fix(b3): align frozen evidence with cutoff
4210003 fix(b3): score eval on the frozen model calendar
71938b1 fix(b3): split structural and model calendars
cebe8a5 refactor(b3): centralize frozen model windows
f04703c fix(b3): keep zero-variance state windows neutral
0cc8095 docs(b3): plan state feature calendar fix
918fe34 docs(b3): freeze state feature calendar semantics
1426a07 fix(b3): bind equal-weight control provenance
```

## 4. worktree 快进(PASS)

```text
git -C <worktree> merge --ff-only 937f66e6769be3a59e0ebfcf0efdcc6b0f3372f1
结果    Fast-forward,11 files changed, 3045 insertions(+), 220 deletions(-)
HEAD    937f66e6769be3a59e0ebfcf0efdcc6b0f3372f1
```

快进后 `git status --porcelain` 无任何 tracked 改动(untracked 仍仅 `run*` 与
`__pycache__`)。交接文档已落地:
`docs/superpowers/plans/2026-08-11-b3-state-calendar-handoff.md`(12,380 bytes)。

## 5. 产物拷回与文件数比对(PASS)

| 项 | 执行机 | 本地 | 结果 |
| --- | --- | --- | --- |
| 文件数 | 31 | 31 | ✅ |
| 目录数 | 4(`backtest`/`logs`/`research`/`research/manifests`) | 4 | ✅ |
| 字节总量 | 126,006,584 | 126,006,584 | ✅ |

传输方式:`scp -P 2222 -r`(二进制流,无换行改写)。

## 6. 哈希对照表(全部 ✅)

### 6.1 审计输出五件(交接文档 "Audit output hashes",run at `5e7a902`)

| 文件 | 期望 SHA-256 | 实测 | 结果 |
| --- | --- | --- | --- |
| `backtest/verdicts.csv` | `1413c5ea…29b9` | `1413c5ea3aeff7a0ea649e4a5b142462bc0518fab60b03a2631da257489729b9` | ✅ |
| `backtest/production_metrics.csv` | `70966656…410e` | `709666560e13faea5e5e2677297b97f74dee66f1a9c65ec3b626917f0498410e` | ✅ |
| `backtest/yearly_contribution.csv` | `822a981a…b006a` | `822a981a3678a986c64c34ab5bfc7e10584b19a0e47c01809a38151e484b006a` | ✅ |
| `backtest/bootstrap.csv` | `92bb98b5…b051` | `92bb98b5e726c570c531863853fc6a0253f6699e00116cd3c19a7dd32db9b051` | ✅ |
| `backtest/run_manifest.json` | `382ec014…dd56` | `382ec014e2e033ec147452df6b0c832f0e6c72d740191a5b98746df176dcdd56` | ✅ |

### 6.2 manifest / input 九件(交接文档 "Every manifest/input hash")

| 条目 | 本地路径 | 期望 SHA-256 | 实测 | 结果 |
| --- | --- | --- | --- | --- |
| preflight manifest | `research/manifests/preflight.json` | `f3e8bfec…eb82` | `f3e8bfecf268f2bc7ae1008d00dc31d2146959c529054b795fdd4cf4be7aeb82` | ✅ |
| exposures manifest | `research/manifests/exposures.json` | `efd078f1…5bc43` | `efd078f1b6d1ec37ebd21a9810e7f11256d48cf51dd84946c6c4f03eef15bc43` | ✅ |
| states manifest | `research/manifests/states.json` | `e912b596…3073a2` | `e912b596cfe78791ef453c65ce79c873d2491d831bf5673db2a95d49513073a2` | ✅ |
| structure manifest | `backtest/structure_manifest.json` | `0621d96c…917de` | `0621d96c4be830a48756627fcc9b65088b7bf1636a434d098cd4b406239917de` | ✅ |
| monthly_exposures | `research/monthly_exposures.csv.gz` | `8c336550…28db` | `8c3365508abef5cff12230da12ce4b8e1377f4df06c9a18f77b3af76d0c928db` | ✅ |
| state_components | `research/state_components.csv` | `77dc3a2f…7ec1f4` | `77dc3a2f79ed01248ac87e444adbde897e06fc89c3db2d72a8c24aa0107ec1f4` | ✅ |
| model_comparison | `backtest/model_comparison.csv` | `aee6ea29…b30b9f` | `aee6ea2977904e16a6cdaa31dd6be90ea07202c703686fecb189a914fba30b9f` | ✅ |
| structure_coefficients | `backtest/structure_coefficients.csv` | `4bae4444…0083a` | `4bae44445e144dc409fc54a1bb0683f02f92e8b1cd3514abc7d9b8bea160083a` | ✅ |
| equal-weight 源文件 | 未拷回(在执行机 `D:\style_timing_signal\output\equal_weight\`) | `5d3fb8c9…4f64` | 执行机侧就地校验 `5d3fb8c90c836f40b86918f649b3c8844ec905b8f22bc0c4f5efdc26e25e4f64` | ✅(远端只读核验,本地 N/A) |

补充核对(交接文档 structure 段另列):

| 文件 | 期望 | 实测 | 结果 |
| --- | --- | --- | --- |
| `research/hard_sort_surface.csv` | `d6b923e3…80cac9` | `d6b923e3ebd6023b5f84fc45d2d421f53c2e4a20658f30abbc70d9878180acc9` | ✅ |

`backtest/structure_manifest.json` 内绑定的 equal-weight 溯源与交接文档逐字一致:
`path=/mnt/d/style_timing_signal/output/equal_weight/equal_weight_signal_20d40z.csv`、
`sha256=5d3fb8c9…4f64`、`source_kind=file`、`date_column=date`、`value_column=factor_value`。

`backtest/run_manifest.json` 的 `stage_manifest_hashes` 与 `input_file_hashes`
八项与上表逐字一致,`code_commit = 5e7a9025a97674034584923772b8d3f0479bb32c`。

### 6.3 未在交接文档列出、本次一并留痕的文件

```text
b3_execution_receipt.json                 9ed33b1087b1754e8c0badc48ea46625cc42be58b938f1dcc2babf5aaac353de
logs/preflight.stderr.log                 78cce52fe412fa159d3d5c30decbe48435b2c11e8566e62d69e660c18ebced27
logs/preflight.stdout.log                 e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855  (空文件)
logs/preflight.time.txt                   70027fdf59da931c03c20dad6b8684ce4ff14b81f13fa2d0dbbfa8d0091971b7
logs/build.stderr.log                     67bbdb1a547094ea739215b1ff983b576b3b7a81e87011bc8dc15368cf2d5e6b
logs/build.stdout.log                     e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855  (空文件)
logs/build.time.txt                       036818763c2409a4d7a68b72ecee2d69f526f128934cace9733a9fefea7d6070
logs/eval.stderr.log                      98d18b3b488a54afa7c469e8a2309885b99c2dbcfc76d1fc7efbb5b7afbf432a
logs/eval.stdout.log                      e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855  (空文件)
logs/eval.time.txt                        8ea97dc092bd2e680081f76af6be78a07b7a091781f95660f88b8a1c7e781fca
research/axis_returns.csv                 ca592ca032759068cffc84dd39889bf73ecca9e3519d3b5b11ac8dfb3d8e3ccd
research/conditional_leg_returns.csv      308d3c3b5d085d984a9981bcec59fd8424a96d49ba030c434e223e3b689d461c
research/coverage_audit.csv               cd7af025bcc77741e65d99bf1772f4d84502399365006329f903def619b04719
research/exposure_diagnostics.csv         f3a6f91d7fa2e3932124bb3e24e255b71f339834983c90f7dc40acebd88cd4a8
research/manifests/portfolios.json        c42393cd5eb48d6b12d82133d0bc584529c450a1ff74d47e7c55cefb1c1b4f47
research/stock_period_returns.csv.gz      c76246ca46ad35d47e3d69d9234c4302004e18f1caf7dd2006357fcfb700c4a5
research/suspension_interval_evidence.csv e3a339a23b1157ea8c0fca2a38fa9b86a15cf58b309b7a3b3869057a653861cd
```

## 7. `b3_execution_receipt.json` 三段摘要

回执元数据:`schema=b3-wind-share-capital-execution`、`version=1`、
`complete=true`、`stopped_at=null`、`data_end=2023-12-31`、`platform=windows`、
`python=3.13.9`、`memory_max=null`(无内存帽)、
`numpy 2.3.4 / pandas 2.3.3 / psutil 7.2.2 / psycopg2 2.9.11 / pyyaml 6.0.3`。

| 阶段 | exit_code | allowed | wall_seconds | peak_rss_kib | ≈ GiB |
| --- | --- | --- | --- | --- | --- |
| preflight | 0 | `[0]` | 1564.908 | 19,079,344 | 18.20 |
| build | 0 | `[0]` | 2505.115 | 19,091,524 | 18.21 |
| eval | 2 | `[0, 2]` | 0.534 | 68,752 | 0.07 |

回执内声明的 **9 个 log 文件 SHA-256 与本地拷回件逐一一致(9/9 ✅)**。

### 7.1 ⚠️ 回执被后续两次补跑部分取代(不止 eval 段)

回执里的 `eval` 段 wall 仅 0.5 秒、峰值 67 MiB,其 `logs/eval.stderr.log` 内容为:

```text
DATA_BLOCKED (pre-audit rejection, no audit evidence written):
STRUCTURE_PROVENANCE_MISSING: structure_manifest.json is missing
```

执行机侧原始 mtime 序列印证了这一点:

```text
2026-08-10 19:49–20:57  preflight → build → (前置失败的 eval) → 回执写盘 20:57:39
2026-08-11 15:49:02     state_components.csv / states.json      (run_states_stage 重建 states)
2026-08-11 15:50:55     structure_manifest / structure_coefficients / model_comparison / hard_sort_surface
2026-08-11 17:27:38     verdicts / production_metrics / yearly_contribution / bootstrap / run_manifest
```

因此回执被取代的**不只是 eval 段**,逐腿盘点如下:

| 回执阶段 | 该腿产物 | 是否仍是现行件 |
| --- | --- | --- |
| preflight | `preflight.json` + coverage_audit / exposure_diagnostics / suspension_interval_evidence(08-10 20:41) | ✅ 现行 |
| build · exposures 腿 | `exposures.json` + `monthly_exposures.csv.gz`(08-10 20:42) | ✅ 现行 |
| build · **states 腿** | `states.json` + `state_components.csv` | ❌ **已被 08-11 15:49:02 的 `run_states_stage` 就地重建覆盖**,溯源改由 states manifest `e912b596…` 承接 |
| build · portfolios 腿 | `portfolios.json` + axis_returns / conditional_leg_returns / stock_period_returns(08-10 20:57) | ✅ 现行 |
| eval | 无(前置失败,未写审计证据) | ❌ **整段被 08-11 17:27:38 的成功 eval 取代** |

structure 阶段(08-11 15:50:55)根本不在回执的三段之内。

结论:**回执可信承载的只有 preflight 全段、build 的 exposures 与 portfolios 两腿;
build 的 states 腿与整个 eval 段均已被后续补跑取代。** 五件审计输出的溯源由
`backtest/run_manifest.json`(`code_commit=5e7a902`,其 `stage_manifest_hashes`
绑定的正是重建后的 states manifest `e912b596…` 与 structure manifest `0621d96c…`)
承担,而非由回执承担。这与交接文档叙述一致(states 直接用 `run_states_stage`
重建、eval 共三次尝试),不构成产物不一致,但用回执做溯源闸门时必须知道其覆盖边界。

## 8. `verify_post_write.py`:重映射一个字段后可完整跑通,判定 `accepted:false`(真实闸 3 失败)

脚本路径:`data_fixes/2026-08-01-b3-wind-share-capital/verify_post_write.py`(383 行)。

**它验什么:** 纯文件级收口验证,**不连任何数据库**(全文无 `psycopg2` /
`sqlalchemy` / 连接串)。七个必填 CLI 入参对应三类证据:

1. 运行产物 —— 执行回执(三段顺序/退出码/log 哈希)、preflight manifest
   (status=OK + 声明产物哈希)、`coverage_audit.csv`(`DATA_MISSING_SHARES`
   必须归零、两 PIT policy 各 111 个 required formation、无仍被 block 的
   `monthly_exposure`)、`run_manifest.json`(候选判定非空、
   `invalid_formation_months` 为空、逐字抄回 `final_verdict`)。
2. 提案链 —— `proposal_manifest.json` / `apply_receipt.json` /
   `post_write_canonical_verification.json` 三者互绑同一哈希且 mismatch 均为 0
   (这三个文件是**数据库写入**环节的既有回执,脚本只读它们,不重连库)。
3. 输出 —— 写一份 `final_verification.json`。

**移植性障碍(只有一处,且不影响结论):** `verify_execution_receipt()` 第 100 行对
回执内嵌的 `entry["path"]` 求哈希,而那是绝对 Windows 路径
`D:\style_timing_signal\...\logs\*.log`,在 Linux 上直接跑会
`missing evidence file`。**只需把 `files[].path` 这一个字段重映射到本地
`logs/<basename>` 即可**,其余六个入参本就全由 CLI 传入,
`verify_preflight_manifest` 用 `manifest_path.parent.parent` + 相对名,亦可移植。

**实测结果(独立 QA 复核本批次时,在重映射该字段后完整运行所得):**

| 闸门 | 内容 | 结果 |
| --- | --- | --- |
| 闸 1 `verify_execution_receipt` | 三段顺序、各段 exit 在 allowed 内、9 个 log 哈希 | ✅ 通过 |
| 闸 2 `verify_preflight_manifest` | `status=OK` + 3 个声明产物哈希 | ✅ 通过 |
| 闸 3 `verify_coverage_audit` | `DATA_MISSING_SHARES` 必须归零 | ❌ **失败** |
| 闸 4/5(run_manifest、提案链) | —— | 未达(闸 3 已中止) |

失败信息逐字为:

```text
DATA_MISSING_SHARES is not cleared: {'all': 6656, 'required': 0}
```

**这是真实且特定于闸 3 的判定,不是路径伪影** —— 脚本要求
`shares["all"] == 0 and shares["required"] == 0`,而现行 `coverage_audit.csv` 的
`all` 为 6,656(`required` 已为 0)。本次取回后我已用同一口径在本地拷回件上
独立复算,与 QA 数值逐项吻合:

```text
DATA_MISSING_SHARES        all=6656   required=0
DATA_MISSING_CLOSE         all=232    required=64
required formations        legal_deadline=111、legal_deadline_plus_one_month_end=111
blocked monthly_exposure   0(另有 30 条 MEASURE_WITH_EXCLUSION)
invalid_formation_months   []
final_verdict / family     DATA_BLOCKED / STOP
```

即除闸 3 外,其余各项验收条件在现行产物上均满足。脚本最终输出
`accepted:false`,`failure` 字段即上述 SHARES 未清零信息。

**本回执作者(实现代理)未运行该脚本**,原因是任务给的运行闸门要求"纯只读":
`--output` 默认写 `data_fixes/2026-08-01-b3-wind-share-capital/final_verification.json`,
落在 tracked 的 campaign 目录内,而本批次禁止改动 tracked 文件;且当时判断内嵌
Windows 路径不可解析。**后一条判断经 QA 实测证伪**(重映射一个字段即可跑通,
且失败原因是真实的闸 3),已据实更正如上。作为补偿,脚本闸 1 的 9 个 log 哈希
校验已由本代理按 basename 重映射手工完整执行(9/9 一致,见 §7)。

**附带发现:** `allowed_exit_codes=[0,2]` 使回执里那次前置失败的 eval(exit 2)
照样通过闸 1,因此闸 1 并不能证明五件审计输出的溯源;真正覆盖它们的是
`verify_run_manifest`(闸 4),而本次因闸 3 中止未被执行。

## 9. 异常清单

1. **(信息级,非缺陷)** `b3_execution_receipt.json` 的覆盖边界小于其
   `complete:true` 的字面含义:eval 段是 08-10 那次
   `STRUCTURE_PROVENANCE_MISSING` 前置失败,build 的 **states 腿**产物亦已被
   08-11 15:49 的 `run_states_stage` 就地重建覆盖。仅 preflight 全段与 build 的
   exposures / portfolios 两腿仍由回执可信承载。详见 §7.1。
2. **(实质发现,非取回缺陷)** `verify_post_write.py` 在重映射 `files[].path`
   一个字段后可完整运行,判定 `accepted:false`,失败于闸 3:
   `DATA_MISSING_SHARES is not cleared: {'all': 6656, 'required': 0}`。该结果已由
   本代理在本地拷回件上独立复算确认,属现行产物的真实状态(上游 SHARES 尾巴未
   清零),**不是本次取回或传输造成的**。详见 §8。

**哈希不一致:无。文件数/字节数差异:无。ff 冲突:无。取回保真度:完好。**

## 10. 本次新建的文件与目录

```text
/home/elfbob/claude-code/deploy_backups/2026-08-12-b3-formal-run/b3-formal-20260812.bundle
<worktree>/data_fixes/2026-08-01-b3-wind-share-capital/run-windows-formal/        (31 个拷回件)
<worktree>/data_fixes/2026-08-01-b3-wind-share-capital/run-windows-formal/RETRIEVAL.md  (本文件)
执行机 D:\deploy_stage\b3_bundles\b3-formal-20260812.bundle                       (唯一远端写入)
```
