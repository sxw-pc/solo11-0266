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

## 离线配重工单

两台转子、两个平面（A/B）、两个离线工况（低速/高速），全部判定只在离线台架内完成，不连接真实设备。合成配重件按编号入库（`trim_inventory`），同一编号同一时刻最多属于一个安装平面，每个平面最多两枚。

`workorder` 子系统在同一事件库内签发配重工单：

- `open_order` 按合同（两台编号、联运相位、每台与底座平方上限）求解。先由两个平面的实际安装枚数推出高低工况的有向响应；联运时先按相对相位转动第二台响应再矢量合成，合成平方按实际矢量计算，不用各台平方和替代，也不接受只在单一相位名义抵消的组合。两种委托都优先使用最少的不同配重件。
- `issue` 按实物编号领用；跨工单占用、未释放的在位件、编号不存在、与工单方案不符都会拒发，重复回执幂等。
- `accept` 绑定真实 `spinstand` 试转事件：轮换委托按台验收单机试转，联运委托按合同相位验收两台共台试转，并核对当前修订；改装后旧修订的共台回执判为 `stale_revision`，原始 `rotor_runs` 记录仍保留可查，新试转以 `acceptance_superseded` 接管槽位。
- `deliver` 校验验收闭环与在位状态。轮换交付物是一套可原样换机的实体包（换机不改变各件的平面归属，任一时刻只交付一台可用能力）；联运交付物是两套同时在位的包，全部相位共台验收通过才交付双机同时可用。
- `replan` 让拒发工单在补料或释放后用同一工单重新求解；重开同一临时库时由事件日志保证重复回执不重复领件。
- 无可用组合时返回 `workorder_refused` 与证据：库存短缺（含最佳候选所需枚数、可用编号）以及所有候选在超限工况/相位上的平方值。

```sh
python -m usagecourt workorder --db state.sqlite --fixture fixtures/workorder_rotation.json
python -m usagecourt workorder --db state.sqlite --fixture fixtures/workorder_joint.json
python -m usagecourt workorder --db state.sqlite --fixture fixtures/workorder_shortage.json
python -m usagecourt replay --db state.sqlite
```

`workorder_rotation.json` 演示同一两枚编号件在 A 验收、释放、装到 B 再验收的轮换闭环；`workorder_joint.json` 演示五枚件两套包四相位共台验收；`workorder_shortage.json` 演示四枚库存下联运拒发、补入第五枚后同一工单 `replan` 并完工。合成夹具中的每条命令带 `subsystem` 字段，由同一事件库路由到 `rotor`、`spinstand` 或 `workorder`。
