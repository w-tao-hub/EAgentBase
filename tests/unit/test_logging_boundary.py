"""日志依赖边界测试。"""

from __future__ import annotations  # 启用未来注解，统一测试文件语法风格

from pathlib import Path  # 导入路径工具，便于扫描源码目录


def test_upper_layers_do_not_import_infra_logging_get_logger() -> None:
    """验证 core、services、interfaces 不再直接依赖 infra 的 get_logger。"""
    repo_root = Path(__file__).resolve().parents[2]  # 回到仓库根目录，确保路径在不同执行目录下都稳定
    target_directories = [  # 需要遵守边界的上层目录
        repo_root / "app" / "core",
        repo_root / "app" / "services",
        repo_root / "app" / "interfaces",
    ]
    banned_import = "from app.infra.logging import get_logger"  # 禁止继续出现在上层代码中的导入语句

    violating_files: list[str] = []  # 收集所有违反边界的文件，失败时一次性给出完整清单

    for directory in target_directories:  # 逐个扫描受约束的源码目录
        for file_path in directory.rglob("*.py"):  # 遍历目录下全部 Python 文件
            file_content = file_path.read_text(encoding="utf-8")  # 读取源码文本，按 UTF-8 解析项目文件
            if banned_import in file_content:  # 命中禁止导入时记录相对路径，便于快速定位
                violating_files.append(str(file_path.relative_to(repo_root)))

    assert violating_files == [], f"以下文件仍直接依赖 app.infra.logging.get_logger: {violating_files}"
