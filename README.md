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

## 探针交叠、校准接续与两种封存授权

主探针在窗口末尾掉线、备探针留有交叠样本时，交叠样本不会被当成已接受的收费样本（仍返回 `overlap_dispute`），但会以 `overlap` 状态保留下来供校准核验。样本可用 `units: [逐时间槽用量…]`、`trajectory/segments: [[start,end,units]…]` 或累计读数给出**原始单位轨迹**。

- `meter` `calibrate`（别名 `calibration`/`prove`，`bill` 子系统也可受理）按原始轨迹核验两个探针：换接点前必须存在连续共同观测窗；交叠窗穿过价格边界时，每个价格段（含换接前不足一段的片段）都分别核验用量一致。可用 `handoff/handoffs` 或 `candidates` 给出多个换接点/证据组；系统选择末端覆盖最远且能完整证明接续的一条，其余候选与分段核验结论保留在 `calibration_routes`、`calibration_segments` 中。
- `meter` `seal` 增加 `authorization`（或 `mode`）：
  - `strict`（严格溯源）只封存获批主探针的连续证据，未覆盖时间写入 `meter_gaps` 待证区间；已证实价格行可 `bill` `issue`，但 `settle` 以 `invoice_blocked`/`coverage_gap` 阻断全窗结算。
  - `backup`（备援授权）凭 `calibration` 凭证接入备用探针；凭证按租户、实例 UID、资源与采集代际绑定，新 UID 或代际不得继承。封存的 `meter_evidence` 分段证据让每个收费单位只归属一条样本。
- 晚到 `correct` 使原换接点失效时会自动重选仍可证明的接续点（也可显式调用 `replan`），重写封存证据，并只把金额或溯源（按更正谱系归一后）确实变化的 `invoice_lines` 置为 `review`，其余行保持 `issued`；确属同源同额的溯源迁移可用 `bill` `reconfirm` 确认后继续结算。
- 校准、分段证据、待证区间、行溯源与定向冻结全部落表并纳入 `store.TABLES`，因此余额、结算与封存状态都能由同一事件集 `replay` 重放。

`tests/test_calibration_regression.py` 用跨价格切点 50 的同一设备轨迹（主到 52、备到 60、交叠合计 6 单位而分段为 2/4 与 4/2）验收上述全流程，包括“仅破坏一个换接候选”的更正、已出账行冻结范围、结算门槛与事件重放。
