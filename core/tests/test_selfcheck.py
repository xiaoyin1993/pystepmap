from conftest import analyze_files, find_fn
from pystepmap.selfcheck import classify_unresolved


def test_selfcheck_counts(tmp_path):
    files = {"app/a.py": """
        class Peer:
            def ping_once(self): ...
        def only_here(): ...
        def f(other):
            return 1
            only_here()
        async def g(other):
            other.ping_once()
            await other.mystery()
    """, "app/bad.py": "def (:\n"}
    g = analyze_files(tmp_path, files)
    f = find_fn(g, "f")
    ids = [nid for k, nid in g.selfcheck.unreachable if k == f.key]
    assert [n.subtitle for n in f.nodes if n.id in ids] == ["only_here"]  # 未捕获异常的隐含结局不算
    assert g.selfcheck.unresolved_calls == 1
    assert g.selfcheck.guessed_calls == 1
    assert g.selfcheck.skipped_files and g.selfcheck.skipped_files[0][0] == "app/bad.py"
    assert g.selfcheck.unreachable == sorted(g.selfcheck.unreachable)
    assert g.selfcheck.dead_ends == [(f.key, ids[0])]  # 死代码末尾也没有出路


def test_unresolved_count_covers_calls_not_on_nodes(tmp_path):
    g = analyze_files(tmp_path, {"app/a.py": """
        def f(p):
            p.nothing()
            p.nothing()
            return p.mystery()
        def g(p):
            p.nothing()
    """})
    # 都没有被 await，不会留在节点上；每个函数按 (行, 名字) 去重后求和：f 三处，g 一处
    assert g.selfcheck.unresolved_calls == 4


def test_classify_unresolved_splits_blind_spots_from_third_party_and_logs(tmp_path):
    """目标不明的调用分三类：方法名在项目里有定义的（可能落在项目代码上）、项目里没有这个名字的、日志。"""
    g = analyze_files(tmp_path, {"app/a.py": """
        import logging
        logger = logging.getLogger(__name__)
        class Repo:
            def render(self): ...
        class Other:
            def render(self): ...
        def f(session, svc):
            logger.info("hi")
            session.commit()
            svc.render()
    """})
    groups = classify_unresolved(g.functions)
    assert [s.name for s in groups.blind] == ["svc.render"]  # 项目里有两处 render，不推测，但可能就是它们
    assert [s.name for s in groups.outside] == ["session.commit"]  # 项目里没有 commit
    assert [s.name for s in groups.logs] == ["logger.info"]
    assert len(groups.blind) + len(groups.outside) + len(groups.logs) == g.selfcheck.unresolved_calls


def test_classify_unresolved_takes_log_receivers_from_config(tmp_path):
    """哪些接收者算日志由项目配置的 noise_receivers 决定，不写死 logger / log / logging。"""
    g = analyze_files(tmp_path, {"app/a.py": """
        def f(audit):
            audit.write("x")
    """})
    assert [s.name for s in classify_unresolved(g.functions).outside] == ["audit.write"]
    assert [s.name for s in classify_unresolved(g.functions, ["audit"]).logs] == ["audit.write"]


def test_classify_unresolved_keeps_too_common_names_out_of_blind_spots(tmp_path):
    """get、add 这类太常见的名字（引擎有意不按名字推测）：项目里虽有同名定义，但多半是字典、列表的方法，不算盲点。"""
    g = analyze_files(tmp_path, {"app/a.py": """
        class Cache:
            def get(self): ...
        class A:
            def render(self): ...
        class B:
            def render(self): ...
        def f(d, svc):
            d.get("k")
            svc.render()
    """})
    groups = classify_unresolved(g.functions)
    assert [s.name for s in groups.blind] == ["svc.render"]
    assert [s.name for s in groups.common] == ["d.get"]
