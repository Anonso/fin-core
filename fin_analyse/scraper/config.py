"""爬虫配置"""

from datetime import timedelta, timezone
from functools import lru_cache
from pathlib import Path

# 知识星球 URL
GROUP_URL = "https://wx.zsxq.com/group/15522441811252"
DIGESTS_URL = "https://wx.zsxq.com/digests/15522441811252"
COLUMNS_URL = "https://wx.zsxq.com/columns/15522441811252"

# 项目路径
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


@lru_cache(maxsize=1)
def _kb_root() -> Path:
    # BUG-007: knowledge paths resolve through the single knowledge_root seam
    # (env → XDG shared root, fail closed) — never the stale repo mirror.
    # Lazy at first attribute access so importing this module has no
    # filesystem side effects and no import-order coupling.
    from fin_analyse.runtime.knowledge_root import default_knowledge_base_root

    return default_knowledge_base_root()


def __getattr__(name: str) -> Path:
    # PEP 562: the five legacy path constants stay importable unchanged, but
    # now resolve lazily against the seam instead of the repo copy.
    if name == "KB_ROOT":
        return _kb_root()
    if name == "ARTICLES_DIR":
        return _kb_root() / "articles"
    if name == "IMAGES_DIR":
        return _kb_root() / "images"
    if name == "INDEX_FILE":
        return _kb_root() / "index.json"
    if name == "DEBUG_DIR":
        return _kb_root() / "debug"
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

# Playwright
USER_DATA_DIR = PROJECT_ROOT / ".browser-profile"
EXECUTABLE_PATH = "/home/ypk/.cache/ms-playwright/chromium-1223/chrome-linux64/chrome"

# 时区
TZ = timezone(timedelta(hours=8))

# 滚动设置
MAX_SCROLLS = 40
SCROLL_PX = 4000
SCROLL_WAIT = 1
STALL_LIMIT = 4

# 增量抓取时间窗口（天）
INCREMENTAL_WINDOW_DAYS = 3

# 栏目识别
COLUMN_PATTERNS = [
    (r"星大派每日热点", "星大派每日热点"),
    (r"星大派人脉", "星大派人脉"),
    (r"星大派特刊", "星大派特刊"),
    (r"星大派锐评", "星大派锐评"),
    (r"星大派好问题", "星大派好问题"),
    (r"版本强势英雄", "版本强势英雄"),
    (r"问题回答", "问题回答"),
    (r"回答问题", "回答问题"),
    (r"凤仙郡小故事", "凤仙郡小故事"),
    (r"重中之重", "重中之重"),
    (r"大锅饭的宏观思考", "大锅饭的宏观思考"),
]

KNOWN_COMPANIES = [
    "华为",
    "海思",
    "平头哥",
    "海光信息",
    "壁仞",
    "摩尔线程",
    "中矿资源",
    "金银河",
    "英伟达",
    "国星宇航",
    "蓝箭",
    "中科宇航",
    "安克创新",
    "华宝新能",
    "康宁",
    "藤仓",
    "宝丰能源",
    "宁德时代",
    "中芯国际",
    "寒武纪",
    "九方智投",
    "国机精工",
    # from article content auto-extraction
    "澜起科技",
    "安路科技",
    "裕太微",
    "滨化股份",
    "菲利华",
    "江丰电子",
    "红星发展",
    "湘潭电化",
    "华勤技术",
    "伟测科技",
    "龙净环保",
    "埃科光电",
    "科达利",
    "阳光电源",
    "东方电气",
    "隆基绿能",
    "华峰测控",
    "恒烁股份",
    "海博思创",
    "百利天恒",
    "泰豪科技",
    "三安光电",
    "三一重工",
    "上海电气",
    "三美股份",
    "万泽股份",
    "壹连科技",
    "先导智能",
    "国瓷材料",
    "极兔速递",
    "顺丰同城",
    "嘉友国际",
    "申通快递",
    "圆通速递",
    "中通快递",
    "中国国航",
    "华夏航空",
    "春秋航空",
    "吉祥航空",
    "海航控股",
    "中远海特",
    "招商轮船",
    "海通发展",
    "宏川智慧",
    "密尔克卫",
    "伯恩斯坦",
    "阿里巴巴",
    "雅克科技",
    "鼎龙股份",
    "南大光电",
    "上海新阳",
    "晶瑞电材",
    "彤程新材",
    "容大感光",
    "飞凯材料",
    "广信材料",
    "扬帆新材",
]
