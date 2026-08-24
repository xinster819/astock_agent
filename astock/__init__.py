"""astock_agent · A 股多策略模拟交易系统。

分层约定（依赖只允许自上而下，禁止回指）：

    cli / ops          命令行与运维脚本，唯一允许 print/sys.exit 的层
      └─ pipeline      轮次编排：prepare → agent → execute，以及规则组轮次
           └─ guards   闸门：风控、互斥、账实对账、新鲜度、市场状态
           └─ strategy 信号生成（纯计算，不碰 IO）
                └─ data     行情/新闻取数与多源交叉验证
                     └─ core     账本与撮合规则（钱的唯一真相）
                          └─ runtime  时钟、路径、配置（无业务语义）

`core` 与 `runtime` 不 import 上层任何模块——这条约束由 tests/unit/test_layering.py 强制。
"""

__version__ = "2.0.0"
