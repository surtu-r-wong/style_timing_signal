# 消费回函03：会员业务数据结构核验通过，日志指纹待封存更正
2026-09-17，style_timing_signal → 数据办公室。

两期业务CSV指纹均匹配；IC2779日/473580行、IM1009日/219600行，抓取日历无缺。每张披露榜名次1..20且唯一，未披露行显式保留空值，会员ID按4位字符串保留，别名ID覆盖通过。IC有560日仅1张合约披露，IM5日；不能将未披露当0。

完整manifest未通过，具体仅两份collect.stdout.log：
- phase1-IM/collect.stdout.log：manifest写空文件SHA e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855；实际341字节，SHA ac2d2352a62356f215d9d7b3c02f386090ed44a9feac3a89589fd3a961651393。
- phase2-IC/collect.stdout.log：manifest同样写空文件SHA；实际341字节，SHA f417cf090709e414236a52f530b875da31aa8e735c078e049e63a4989e83cceb。

形状像封存manifest后stdout缓冲才落盘，但这是推断，请办公室确认。请待日志关闭/刷新后另提供封存更正manifest或补充receipt，并保留原manifest及更正缘由；本项目不擅自修改office指纹，也不把日志问题说成业务行情错误。
当前固定文件足够后续研究准备，暂不新增落表需求或治理裁决。会员信号和集中度等派生尚未定义，后续另附固定公式请求办公室办理，不在研究侧自行派生数据。
附件consumer-acceptance-03/记录两项不一致及业务结构核查；目前状态为业务CSV结构可用、完整封存待修核。
