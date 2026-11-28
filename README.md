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

## Licence seat transfer between two clusters

A licence may be held on one cluster and moved to another without ever letting
both clusters issue the same seat. The `license` protocol keeps these three
things apart:

* **approval** (`approve`) — an administrator approves exactly one seat,
  `source -> target`. It is permission, not a seat.
* **entitlement copy** (`transfer`) — the origin delivers a copy to the target.
  A copy grants nothing by itself; the target still cannot check out, and its
  own local rows prove nothing about the origin.
* **proof of stoppage** (`stop_evidence`) — both agents record the same signed
  proof naming the source session, its instance UID, the boundary, and the
  restore point. Recording it stops nothing.

The origin then chooses one of two lawful outcomes from that same input:

* `leave` keeps the session running at the origin and marks the target's copy
  `declined`. The origin remains the only issuer; the target can never complete
  or check out.
* `handoff` stops the origin's own session at the boundary, signs an
  attestation, and marks its copy `armed`. The origin may not reopen from then
  on, even before the target confirms. The target consumes its `delivered` copy
  plus the attestation with `complete`, opens exactly one session bound to its
  own metered instance UID, and returns a receipt. The origin accepts only that
  receipt (`complete` with `receipt=...`) and records the stop; it never opens
  or reopens a session. Only a destination that actually completed makes it the
  sole issuer.

Each agent runs against its own database file. Messages cross by re-submitting
the same `event_id`; nothing here writes another cluster's rows in one
transaction. `probe` is read-only and reports, per site, what stopped, which
restore point exists, and whether resuming is actually permitted — a restore
record never doubles as permission. Tests demonstrate both outcomes from the
same proof, a lost confirmation, a local restart, and a late revocation using
two independent databases (`tests/test_license_transfer.py`).

Schema note: `licenses` gained `origin`; `license_sessions` gained `cluster`,
`credential`, `stopped_at`, `checkpoint`, `resume_uid` and `transferred`; and
`license_evidence` gained `attested`. Older licence databases are upgraded in
place the first time `license` handles an event.

A single-site origin-side runnable example is
`fixtures/license_transfer_examples.json` (load `meter_normal.json` first). It
approves a transfer, delivers the copy, records the stoppable point, and elects
to stay at the origin. The cross-cluster completion arm needs two databases and
is demonstrated in `tests/test_license_transfer.py`.
