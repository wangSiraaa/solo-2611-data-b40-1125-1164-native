case.json每个basket对象是独立实物件，unit_id a/b/c与退货请求的b对应；金额为整数分。before_coupons是先发布快照，added_coupons加入后形成另一合法快照。grade按unit_id给出；原价与优惠D均为金额，requires是SKU件数。材料不提供优化器或售后实现。

笔尖修磨扩展见 `basket_service/nib.py` 与 `SOLUTION.md` 的 “Nib-grinding workshop” 一节：按 sale+unit 读取已售未退的笔尖，保留每个件的试写证据与可达形状，按班次单磨位排期，并只对能在截止前保证交回的工单签单。
