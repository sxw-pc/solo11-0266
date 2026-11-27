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

## 预付券兑付

预付券按租户、资源与计价版本（`price` 为空表示跨版本券）登记，带批次、有效期、券面与可分次兑付的剩余额。声明 `group_id` 的子券只能整组动用：要么每张子券在本次按整额落到某一账单行，要么整组留存；不成组的券才允许拆开分次兑付。任何一行的抵扣都不得超过该行结算金额，且版本锁定券只能落到对应版本的行。

`authorize` 支持两种互斥授权，均返回本次实际扣款、应付额（`payable`）与仍可兑现的续期金额（`renewal_remaining`）：

- `reduce`（减付授权）：优先压低本次应付，再尽量保留续期可兑额。
- `renew`（续期授权）：先在未过期库存里为每条已登记的未来用量（`commitment_register`，带计价版与到期时点）保住全部承诺，余额才用于减本次应付；不同承诺不能共用同一份券余额，无法全部覆盖时整笔拒绝。

同一张账单重复提交不会再次扣余额（事件幂等且已兑付账单拒绝新授权）。计量更正使账单进入待复核时，未执行的兑付被阻断；已结算的扣券通过 `release_redemption` 以可追溯的反向记账恢复余额并进入与该账单绑定的隔离区（其他账单在两次动作之间无法动用），`reissue_reviewed` 形成新的可结算版本后，`reauthorize` 才能在隔离区内重新分配，`replay` 可完整还原净应付与库存。`settle` 的回执同时给出 `voucher_deduction` 与 `net_due`。

