# 集群用量结算异议台

面向云平台计量和租户财务运营的离线模拟。只使用合成实例、价格、计量和仲裁回执；不调用真实集群、账单或支付系统。Python 3.11+ 标准库与 SQLite 即可运行。

计价契约簿按租户、资源和时窗保存不可重叠的价格版本，折扣需财务与所有者分别批准。用量证据庭绑定命名空间、实例 UID、租户和集群；重叠片段不能双计，平台停机回执与样本更正会重新打开受影响账单。账单异议台按跨时窗价格拆行，保留原行、冲正行、新行和仲裁身份。结算发现未决分项时会阻断，旧结算回执在新证据到达后标为待复核。

在仓库根目录运行：

```sh
python -m usagecourt demo --db demo.sqlite
python -m usagecourt replay --db demo.sqlite
python -m unittest discover -s tests -v
```

单独调用可用 `python -m usagecourt tariff --db state.sqlite --fixture fixtures/tariff_normal.json`，`meter` 和 `bill` 类推。六份正常与失败样例以 tariff-normal、meter-normal、bill-normal、tariff-failure、meter-failure、bill-failure 的顺序共享状态。`report` 输出不可变事件；`replay` 在空库重放并逐业务表比较。

For operational reports, run `meter` with `fixtures/meter_normal.json` in a
fresh database. The `carbon` and `quota` commands can then consume
`fixtures/carbon_examples.json` and `fixtures/quota_examples.json` respectively.
They report simulated emissions and compute allowance from metered facts, and
surface later meter corrections on inspection. `replay` validates both sets of
operational tables as well as the original billing tables.

After loading `meter_normal.json`, `license` manages per-cluster software seats
bound to the existing tenant and instance UID. `inspection` records positions
received from metering collectors and reports missing position ranges. For each
command, `fixtures/<command>_examples.json` is a runnable example. Neither
command calculates an invoice amount. Replay checks both sets of tables.
