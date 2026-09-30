# 封存更正（2026-09-17）

原 `manifest.json` 保留不改。提出方回函 03 指出 `collect.stdout.log` 的指纹是空文件的 sha（e3b0c442…）：采集器在进程结束前封存 manifest，
而 stdout 由外壳重定向、缓冲在退出时才落盘，所以封存时它确实是 0 字节。`collect.stderr.log` 同理（本轮恰为空）。

- 与原 manifest 指纹一致：collect.stderr.log, fetch_log.csv, member_alias.csv, not_disclosed.csv, rank_long.csv
- 更正：collect.stdout.log（见 `manifest.corrected.json`）
- 采集器已改：manifest 只列自己写的四个业务文件（`scripts/cffex_ccpm.py`，测试 `test_manifest_lists_only_files_the_collector_wrote`）。
