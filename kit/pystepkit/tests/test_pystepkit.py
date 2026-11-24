from pystepkit import condition, node, outcome, step, workflow


def test_bare_and_called_forms_return_same_object():
    async def a(): ...
    def b(): ...
    assert step(a) is a and a.__flow_kind__ == "step" and a.__flow_meta__ == {}
    assert step(title="取回原文")(b) is b and b.__flow_meta__ == {"title": "取回原文"}

def test_node_is_step():
    assert node is step

def test_condition_and_workflow_on_function():
    @condition(title="余额充足？")
    def has_balance(ctx): return True
    @workflow(title="账户转账", group="转账")
    async def transfer(ctx): ...
    assert has_balance.__flow_kind__ == "condition"
    assert transfer.__flow_kind__ == "workflow"
    assert transfer.__flow_meta__ == {"title": "账户转账", "group": "转账", "entry": "run"}

def test_workflow_on_class():
    @workflow(name="TransferWorkflow", title="账户转账", version="1.0")
    class TransferWorkflow:
        async def run(self, ctx): ...
        @node(title="参数校验")
        async def validate(self, ctx): ...
    assert TransferWorkflow.__flow_kind__ == "workflow"
    assert TransferWorkflow.__flow_meta__["version"] == "1.0"
    assert TransferWorkflow.validate.__flow_meta__ == {"title": "参数校验"}

def test_outcome_is_plain_dict():
    assert outcome("skipped", reason="whitelisted") == {"kind": "skipped", "reason": "whitelisted"}
    assert outcome(title="完成") == {"kind": "ok", "title": "完成"}
