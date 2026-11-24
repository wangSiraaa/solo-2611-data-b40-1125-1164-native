"""生成仓库内的保修接单夹具：fixtures/warranty/。

内容见 pqscan.warranty.fixtures：一份原始证据 Parquet，两炉各经 pqscan 扫描得到
带溯源的 result Parquet，以及问题工况/库存/见证锅/订单配置 JSON。

直接运行：PYTHONPATH=src python tools/make_warranty_fixtures.py
"""
from __future__ import annotations

import json
import os

from pqscan.warranty.fixtures import build

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main() -> None:
    info = build(os.path.join(HERE, "fixtures", "warranty"))
    print(json.dumps({"raw": info["raw"], "config": info["config"],
                      "scanned": info["scanned"]}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
