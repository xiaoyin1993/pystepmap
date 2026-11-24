"""让测试不论从哪里启动都能导入示例模块：把示例目录（tests 的上一级）放到 sys.path 最前面。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
