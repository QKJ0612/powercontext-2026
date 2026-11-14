# 订单查询规格（SDD 样例）

本文件是样例工程的需求规格，用于演示"规格驱动"的上下文选择。

- 规格版本：v2（本版起上下文服务按 BM25 + 版本号排序；规格正文变更需重新导入 seekdb）。

## REQ-ORDER-1 状态筛选

`find_orders(orders, status=...)` 只返回状态完全相等的订单，且保持入参顺序。

## REQ-ORDER-2 金额合计

`total_amount(orders)` 返回所有订单金额（分）之和，不做筛选。

## REQ-ORDER-3 金额下限筛选

`find_orders` 新增可选关键字参数 `min_amount_cents`：

- 传入时只保留 `amount_cents >= min_amount_cents` 的订单。
- 边界值必须包含（等于下限的订单保留）。
- 与 `status` 同时传入时，两个条件同时生效。
- 不传时行为与旧版本完全一致，不得改变排序。
- 参数名、关键字调用的形态不得变更。

## REQ-ORDER-4 兼容性

既有测试 `test_status_filter_still_works`、`test_default_returns_every_order`、
`test_total_amount_unchanged` 必须继续通过。
