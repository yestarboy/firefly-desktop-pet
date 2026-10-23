# -*- coding: utf-8 -*-
"""
项目路径的唯一来源。

所有模块都从这里取路径，不要再各自写 Path(__file__).parents[N]——
目录一搬就全错。改结构时只改这一个文件。
"""

from pathlib import Path

# src/firefly_app/paths.py -> src/firefly_app -> src -> 项目根
ROOT = Path(__file__).resolve().parents[2]

CONFIG_PATH = ROOT / "firefly_config.json"
CONFIG_EXAMPLE = ROOT / "firefly_config.example.json"

SRC_DIR = ROOT / "src"
TOOLS_DIR = ROOT / "tools"
PANEL_DIR = ROOT / "panel"
ASSETS_DIR = ROOT / "assets"

DATA_DIR = ROOT / "data"
VOICES_DIR = DATA_DIR / "voices"
DB_PATH = DATA_DIR / "memory.db"

LOGS_DIR = ROOT / "logs"
ICONS_DIR = ASSETS_DIR / "icons"
MODELS_DIR = ASSETS_DIR / "models"
LIBS_DIR = ASSETS_DIR / "live2d-libs"

# 桌宠页面与入口（静态服务器根就是项目根）
PET_ENTRY = ROOT / "live2d_pet.py"
PET_PAGE = ROOT / "live2d_pet.html"


def ensure_dirs() -> None:
    """确保运行期需要的目录存在"""
    for d in (DATA_DIR, VOICES_DIR, LOGS_DIR):
        try:
            d.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
