"""pytest 适配：tests/ 下的手写案例以 ``base`` 位置参数驱动临时目录。

案例同时支持 ``python -m tests.test_dictatype`` 的 run_all 入口（自带临时
目录）与 pytest 收集；这里仅把 pytest 内置 ``tmp_path`` 映射为 ``base``。
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir)))


def pytest_generate_tests(metafunc):
    if "base" in metafunc.fixturenames:
        # 每个测试函数一个独立临时子目录
        factory = metafunc.config._tmp_path_factory
        metafunc.parametrize("base", [str(factory.mktemp(metafunc.function.__name__))])
