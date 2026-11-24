"""pystepmap.__version__ 随每次分析存进平台的库（engine_version）：它和包的版本号（core/pyproject.toml）要一致。"""

import importlib.metadata

import pystepmap


def test_version_matches_package_metadata():
    assert pystepmap.__version__ == importlib.metadata.version("pystepmap-core")
