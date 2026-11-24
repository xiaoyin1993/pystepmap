"""测试夹具：一个很小的下单流程（只被分析，不被导入或执行）。"""

from pystepkit import condition, outcome, step, workflow


@step(title="查询库存")
def load_stock(order: dict) -> dict:
    return {"left": 3, "wanted": order["count"]}


@condition(title="库存不够？")
def lacks_stock(stock: dict) -> bool:
    return stock["left"] < stock["wanted"]


@step(title="创建订单")
def create_order(order: dict) -> str:
    return f"O-{order['id']}"


@workflow(title="下单", group="订单")
def place_order(order: dict):
    stock = load_stock(order)
    if lacks_stock(stock):
        return outcome("failed", title="库存不够，没有下单")
    order_no = create_order(order)
    return outcome("ok", title="下单完成", order_no=order_no)
