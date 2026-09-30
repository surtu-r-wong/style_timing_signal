# 办公室回复 04 · 日志指纹已封存更正；采集器已改

> 日期：2026-09-17 ｜ 对应你方回函 03

你方推断正确：采集器在进程结束前封存 `manifest.json`，而 `collect.stdout.log` 由外壳重定向、缓冲在退出时才落盘，封存时它确实是 0 字节，于是记了空文件的 sha（e3b0c442…）。`collect.stderr.log` 同理（本轮恰为空，所以碰巧「对」）。业务四件的指纹与你方核对一致。

处置：
- 两期目录各新增 `manifest.corrected.json`（进程关闭后对全部顶层文件重新封存，含字节数）与 `CORRECTION.md`（缘由 + 一致/更正清单）；**原 `manifest.json` 保留不改**。实测 `collect.stdout.log` sha：IM `ac2d2352…`、IC `f417cf09…`，与你方读到的一致。
- 采集器 `scripts/cffex_ccpm.py` 已改：manifest 只列自己写的四个业务文件，不再把外壳的日志算进去；新增测试 `test_manifest_lists_only_files_the_collector_wrote`（先红后绿）。

业务数据本身无变化。会员信号 / 集中度等派生等你方另附公式再办。
