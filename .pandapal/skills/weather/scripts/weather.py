"""
天气查询工具 — 使用中央气象台（www.nmc.cn）公开 REST API

数据来源：中国中央气象台 (National Meteorological Center)
接口说明：
  - 省份列表:  https://www.nmc.cn/f/rest/province
  - 城市列表:  https://www.nmc.cn/f/rest/province/{province_code}
  - 综合天气:  https://www.nmc.cn/rest/weather?stationid={city_code}
  - 实况天气:  https://www.nmc.cn/f/rest/real/{city_code}

特性：
  - 免费公开、无需 API Key
  - 默认查询中国城市天气
  - 时间以中国时间（Asia/Shanghai）为准
  - 内置 300+ 热门城市快速查找，其余城市在线动态获取

迁移自: capabilities/skills_group/weather/scripts/weather_tools.py
"""

import argparse
import asyncio
import json
import logging
import ssl
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from typing import Optional


logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────
# 常量
# ─────────────────────────────────────────────────────────────

_NMC_BASE = "https://www.nmc.cn"
_NMC_WEATHER_URL = f"{_NMC_BASE}/rest/weather"        # 综合天气（实况+预报）
_NMC_REAL_URL = f"{_NMC_BASE}/f/rest/real"             # 实况天气
_NMC_PROVINCE_URL = f"{_NMC_BASE}/f/rest/province"     # 省份列表
_REQUEST_TIMEOUT = 15
_HTTP_RETRIES = 2                 # 网络请求总尝试次数（1 次重试）
_HTTP_RETRY_DELAY = 1.0           # 重试前等待秒数

# 中央气象台 API 的"无数据"哨兵值：数值/文本字段缺失或无效时返回 9999
_NMC_NO_DATA = "9999"
_NMC_NO_DATA_INT = int(_NMC_NO_DATA)
# 气压有效值上限：低于此值才展示，用于忽略哨兵/异常值
_PRESSURE_VALID_MAX = 9000

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://www.nmc.cn/",
}

# ─────────────────────────────────────────────────────────────
# 内置热门城市 → nmc code 映射（覆盖全国省会、直辖市、主要城市）
# 通过 nmc.cn API 动态获取后缓存，这里预置常见城市以减少网络请求
# ─────────────────────────────────────────────────────────────

_BUILTIN_CITY_CODES = {
    # ── 直辖市 ──
    "北京": "Wqsps", "上海": "WwcJd", "天津": "yqSjH", "重庆": "UkfaS",
    # ── 省会城市 ──
    "石家庄": "uJQqI", "太原": "jmHMK", "呼和浩特": "IlyWx", "沈阳": "xasvS",
    "长春": "mxFBj", "哈尔滨": "pzXOG", "南京": "CxOWZ", "杭州": "HIieJ",
    "合肥": "xqcji", "福州": "djeOy", "南昌": "JmxCz", "济南": "VITKz",
    "郑州": "YVItN", "武汉": "bSpCz", "长沙": "sgkrL", "广州": "DwzZf",
    "南宁": "dvHfb", "海口": "vmEKa", "成都": "yGYHR", "贵阳": "SKMHP",
    "昆明": "IayMy", "拉萨": "utJQK", "西安": "RfjCI", "兰州": "ARkZA",
    "西宁": "SHRvr", "银川": "LqTRN", "乌鲁木齐": "lIPam",
    # ── 特别行政区 ──
    "香港": "KnxKc", "澳门": "ZZhCJ",
    # ── 台湾 ──
    "台北": "VrMVF",
    # ── 主要城市（非省会） ──
    "深圳": "AhpEU", "大连": "nGPyb", "青岛": "Jrsbf", "苏州": "lqYjK",
    "厦门": "gDCDS", "珠海": "oPKiP", "汕头": "ZYprl", "宁波": "HpFDH",
    "温州": "KFwqQ", "无锡": "sMuIY", "常州": "pVjCL", "佛山": "LMDXA",
    "东莞": "hPVnv", "中山": "LPXPy", "惠州": "MBbyH", "烟台": "hGFsU",
    "威海": "TLFKx", "潍坊": "dpkae", "淄博": "JXwOO", "济宁": "IeLkw",
    "泰安": "GzOBK", "临沂": "MGWLR", "徐州": "KaNAp", "南通": "xRBVD",
    "扬州": "EoVUW", "镇江": "CcVHc", "盐城": "IZkFZ", "泰州": "lMCQN",
    "连云港": "NAOAX", "秦皇岛": "mFNRa", "唐山": "mJnJf", "保定": "MxiXb",
    "廊坊": "RxRFT", "邯郸": "VyNLZ", "洛阳": "nCSvW", "开封": "YBLMJ",
    "桂林": "oWFuO", "三亚": "PtnWb", "襄阳": "GIwHV", "宜昌": "vxPiA",
    "芜湖": "WjTHW", "岳阳": "fYDfM", "株洲": "SlDfk", "湘潭": "Sscnw",
    "绵阳": "UAlOF", "德阳": "swkVL", "乐山": "OyPiZ", "宜宾": "MuHoC",
    "泸州": "AwbsN", "遵义": "Nbwpk", "大理": "WjdEr", "丽江": "bgjCi",
    "西双版纳": "yZiQw", "延安": "RqXJc", "包头": "eLobB", "赤峰": "RPIQM",
    "大同": "XDWDQ", "吉林": "YEsDj", "大庆": "tcChk", "绍兴": "JblXQ",
    "嘉兴": "SHpPu", "金华": "MjHXw", "湖州": "KhDJt", "台州": "bMnDQ",
    "衢州": "BfXdj", "丽水": "xVuUK", "莆田": "dFFqb", "泉州": "JhriQ",
    "漳州": "bWgdU", "龙岩": "TqVnI", "南平": "fVKmT", "三明": "CZiRH",
    "景德镇": "qeSNx", "九江": "rfojE", "赣州": "Sknlq", "吉安": "uYtjA",
    "上饶": "MeJpU", "鹰潭": "nZdaF", "新余": "uJlFE", "萍乡": "GqPET",
    "抚州": "JLxHN", "宜春": "LqmKY", "淮安": "vifEB", "宿迁": "vsSJV",
    "马鞍山": "HfJnA", "安庆": "FmpMX", "六安": "JpyYR", "滁州": "pwNqz",
    "阜阳": "bGBIP", "亳州": "BNJNf", "黄山": "BaVFb", "蚌埠": "oXjaa",
    "淮南": "mhDUk", "宿州": "GjkTE", "铜陵": "mpJkc", "池州": "sQMZC",
    "日照": "kqZqp", "聊城": "NINaI", "德州": "YzOpU", "滨州": "eWHXW",
    "菏泽": "DzjOr", "枣庄": "DYhKn", "东营": "HWJwq", "莱芜": "tOaKp",
    "许昌": "nHnmP", "新乡": "InYgr", "周口": "HjEJF", "南阳": "JCLvA",
    "信阳": "hqDxc", "驻马店": "bAFAU", "商丘": "fHWBt", "安阳": "uoiQM",
    "焦作": "dBcnZ", "平顶山": "YbSxJ", "鹤壁": "WtSwT", "濮阳": "wdJEm",
    "漯河": "oWZjR", "孝感": "JVkBW", "荆州": "oJFZj", "荆门": "SsUfT",
    "黄冈": "bEfij", "咸宁": "ypKvJ", "随州": "PwCQQ", "十堰": "PuKCp",
    "黄石": "wCuIa", "鄂州": "wkLnK", "恩施": "ouTfx", "常德": "QBnqh",
    "衡阳": "FZqLk", "邵阳": "xvZGP", "益阳": "ChGhF", "郴州": "snJjL",
    "怀化": "RHeMU", "永州": "pFqwq", "娄底": "dGSKG", "湘西": "FzIkP",
    "张家界": "xdLOh", "韶关": "UTyuE", "梅州": "KeYol", "河源": "OhxrI",
    "肇庆": "FmJSb", "清远": "EsOSo", "阳江": "gEXCf", "茂名": "bqMjm",
    "湛江": "EPlyp", "江门": "LRnJC", "云浮": "fwsHw", "潮州": "JYkZY",
    "揭阳": "IPdQA", "汕尾": "BGJWV", "柳州": "IFEvp", "北海": "mCpoh",
    "玉林": "cPiLz", "梧州": "JivzW", "百色": "UPGkL", "河池": "bTOmv",
    "钦州": "WlqCz", "防城港": "FVpkZ", "贵港": "tLsRn", "贺州": "kOpzx",
    "来宾": "dSfVR", "崇左": "MUixh", "攀枝花": "ItVjl", "南充": "veyEB",
    "达州": "KDPAz", "广安": "RKVXW", "内江": "tqcjA", "自贡": "mrdAT",
    "资阳": "MlTrD", "眉山": "JmDEL", "广元": "Hkxwp", "雅安": "vCSKn",
    "巴中": "Ksrki", "遂宁": "yGGNF", "甘孜": "pWYSN", "凉山": "hhYDO",
    "阿坝": "mCEpj", "六盘水": "lhXhP", "安顺": "GnvVD", "铜仁": "yLLUp",
    "毕节": "XjJKn", "黔南": "kvbpt", "黔东南": "DnRqr", "黔西南": "jYIpC",
    "曲靖": "oqIgf", "玉溪": "bqxag", "保山": "SdAiN", "昭通": "xJyTR",
    "文山": "dbHCL", "红河": "wYRiY", "楚雄": "CtaJO", "普洱": "HqRiJ",
    "临沧": "kvnTl", "怒江": "fcAbQ", "迪庆": "JHBRf", "德宏": "PfGNF",
    "宝鸡": "DjDmn", "咸阳": "wUVLw", "渭南": "AhGJm", "汉中": "xbvFt",
    "榆林": "qxqLN", "安康": "vIkYD", "商洛": "qXSzZ", "铜川": "Phkly",
    "天水": "OEVgD", "酒泉": "DnpuS", "庆阳": "BhHiP", "平凉": "iOiPC",
    "武威": "tDJvn", "张掖": "CYkJI", "白银": "eIchS", "定西": "PJqkD",
    "陇南": "TdMos", "金昌": "DuOIs", "嘉峪关": "TevTZ", "鄂尔多斯": "HwVCJ",
    "通辽": "haxlc", "呼伦贝尔": "dVsMN", "巴彦淖尔": "bMELo", "乌兰察布": "jFFPC",
    "锡林郭勒": "JKAjw", "兴安": "nBvCH", "阿拉善": "RlTIK",
    "鞍山": "plKjq", "抚顺": "KBGov", "本溪": "cqsNR", "丹东": "MjxJQ",
    "锦州": "JyUCj", "营口": "WSHQO", "阜新": "yiFwV", "辽阳": "HPvfq",
    "盘锦": "GFLPP", "铁岭": "cHzOL", "朝阳": "bLmpM", "葫芦岛": "YHCrW",
    "延边": "GjqMH", "四平": "wIbzB", "通化": "Wdqqs", "白山": "LFZcF",
    "白城": "zyBcl", "辽源": "iuQOT", "松原": "Bxvmz",
    "齐齐哈尔": "wQdyP", "牡丹江": "AHTml", "佳木斯": "aByYw", "鸡西": "lBvtO",
    "鹤岗": "rCTIK", "双鸭山": "QrFKh", "伊春": "dKXfV", "黑河": "tXBBq",
    "绥化": "FkVDn",
    "吴忠": "sRxcD", "固原": "PBuMZ", "中卫": "IJSGh", "石嘴山": "vfKmO",
    "海东": "tpdSJ", "海西": "gJwqx", "海南州": "GWWEq", "海北": "bgqTg",
    "黄南": "uKYGK", "果洛": "cGPUy", "玉树": "YYEjH",
    "克拉玛依": "zxsPz", "吐鲁番": "zCJhR", "哈密": "ADCYg", "昌吉": "pSORy",
    "伊犁": "JfqBb", "塔城": "Uuiiv", "阿勒泰": "DGsfG", "喀什": "UVjOl",
    "和田": "eFNKf", "阿克苏": "IRFrV", "巴州": "LYVhy", "博州": "TQGjr",
    "日喀则": "nAnIM", "山南": "mPPRR", "林芝": "aJGIF", "昌都": "MsPHM",
    "那曲": "yVxrv", "阿里": "cIpjN",
}

# 省份简称→ nmc 省份代码映射（用于在线查找城市）
_PROVINCE_CODES = {
    "北京": "ABJ", "天津": "ATJ", "河北": "AHE", "山西": "ASX",
    "内蒙古": "ANM", "辽宁": "ALN", "吉林": "AJL", "黑龙江": "AHL",
    "上海": "ASH", "江苏": "AJS", "浙江": "AZJ", "安徽": "AAH",
    "福建": "AFJ", "江西": "AJX", "山东": "ASD", "河南": "AHA",
    "湖北": "AHB", "湖南": "AHN", "广东": "AGD", "广西": "AGX",
    "海南": "AHI", "重庆": "ACQ", "四川": "ASC", "贵州": "AGZ",
    "云南": "AYN", "西藏": "AXZ", "陕西": "ASN", "甘肃": "AGS",
    "青海": "AQH", "宁夏": "ANX", "新疆": "AXJ", "香港": "AXG",
    "澳门": "AAM", "台湾": "ATW",
}

# 运行时缓存（在线查到的城市代码写入此 dict，减少重复请求）
_city_code_cache: dict = {}


# ─────────────────────────────────────────────────────────────
# 内部工具函数
# ─────────────────────────────────────────────────────────────

def _get_china_now() -> datetime:
    """获取当前中国标准时间（Asia/Shanghai）"""
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("Asia/Shanghai"))
    except Exception:
        # fallback: UTC+8
        return datetime.utcnow() + timedelta(hours=8)


def _http_get_json(
    url: str,
    timeout: int = _REQUEST_TIMEOUT,
    retries: int = _HTTP_RETRIES,
) -> dict:
    """发送 GET 请求并解析 JSON 响应（网络失败自动重试，最多 retries 次）"""
    ctx = ssl.create_default_context()
    last_error: Optional[Exception] = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url)
            for k, v in _HEADERS.items():
                req.add_header(k, v)
            with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
                raw = resp.read()
                return json.loads(raw.decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last_error = e
            if attempt < retries - 1:
                logger.info(
                    "[weather] 请求失败（%s），%.1fs 后重试: %s", e, _HTTP_RETRY_DELAY, url
                )
                time.sleep(_HTTP_RETRY_DELAY)
    raise last_error  # retries ≥ 1 且循环内未成功返回，此处必然非 None


def _resolve_city_code(city: str) -> Optional[str]:
    """
    将城市名解析为中央气象台的 station code。
    
    查找顺序：
    1. 内置热门城市映射
    2. 运行时缓存
    3. 在线从 nmc.cn 动态获取
    """
    # 0. 去掉可能附带的行政区后缀
    for suffix in ("市", "区", "县", "州", "盟"):
        if city.endswith(suffix) and len(city) > 2:
            stripped = city[:-1]
            if stripped in _BUILTIN_CITY_CODES:
                return _BUILTIN_CITY_CODES[stripped]
            if stripped in _city_code_cache:
                return _city_code_cache[stripped]

    # 1. 内置映射
    if city in _BUILTIN_CITY_CODES:
        return _BUILTIN_CITY_CODES[city]

    # 2. 运行时缓存
    if city in _city_code_cache:
        return _city_code_cache[city]

    # 3. 在线查找：遍历所有省份的城市列表
    # 尝试匹配原始名称和去后缀名称
    search_names = {city}
    for suffix in ("市", "区", "县", "州", "盟"):
        if city.endswith(suffix) and len(city) > 2:
            search_names.add(city[:-1])
    
    logger.info("[weather] 城市 '%s' 不在内置映射中，在线查找...", city)
    try:
        provinces = _http_get_json(_NMC_PROVINCE_URL)
        for prov in provinces:
            prov_code = prov["code"]
            try:
                cities = _http_get_json(f"{_NMC_BASE}/f/rest/province/{prov_code}")
                for c in cities:
                    # 缓存所有查到的城市
                    _city_code_cache[c["city"]] = c["code"]
                    if c["city"] in search_names:
                        logger.info("[weather] 在线查找到: %s -> %s (%s)", city, c["code"], prov["name"])
                        return c["code"]
            except Exception:
                continue
    except Exception as e:
        logger.warning("[weather] 在线查找城市失败: %s", e)

    return None


def _format_wind_direction(degree: float) -> str:
    """将风向角度转换为中文方位"""
    directions = ["北风", "东北风", "东风", "东南风", "南风", "西南风", "西风", "西北风"]
    idx = round(degree / 45) % 8
    return directions[idx]


def _format_realtime(real_data: dict) -> str:
    """格式化实况天气数据"""
    station = real_data.get("station", {})
    weather = real_data.get("weather", {})
    wind = real_data.get("wind", {})
    sunrise_sunset = real_data.get("sunriseSunset", {})

    city = station.get("city", "未知")
    province = station.get("province", "")

    lines = [
        f"{province} {city} — 实况天气",
        f"发布时间: {real_data.get('publish_time', '未知')}",
        "",
        f"温度: {weather.get('temperature', '--')}°C"
        f" (体感: {weather.get('feelst', '--')}°C)",
        f"天气: {weather.get('info', '--')}",
        f"湿度: {weather.get('humidity', '--')}%",
        f"风况: {wind.get('direct', '--')} {wind.get('power', '')} "
        f"({wind.get('speed', '--')} m/s)",
        f"降水: {weather.get('rain', 0)} mm",
    ]

    # 气压（API 无数据时返回哨兵 9999，缺失/非数值/超阈值一律不展示）
    pressure_raw = weather.get("airpressure")
    if pressure_raw not in (None, ""):
        try:
            pressure = int(pressure_raw)
        except (TypeError, ValueError):
            pressure = _NMC_NO_DATA_INT
        if 0 < pressure < _PRESSURE_VALID_MAX:
            lines.append(f"气压: {pressure} hPa")

    # 日出日落
    sunrise = sunrise_sunset.get("sunrise", "")
    sunset = sunrise_sunset.get("sunset", "")
    if sunrise and sunset:
        # 只取时间部分
        sr_time = sunrise.split(" ")[-1] if " " in sunrise else sunrise
        ss_time = sunset.split(" ")[-1] if " " in sunset else sunset
        lines.append(f"日出/日落: {sr_time} / {ss_time}")

    return "\n".join(lines)


def _format_forecast(predict_data: dict, target_date: Optional[str] = None) -> str:
    """格式化预报天气数据"""
    station = predict_data.get("station", {})
    details = predict_data.get("detail", [])
    publish_time = predict_data.get("publish_time", "")

    city = station.get("city", "未知")
    province = station.get("province", "")

    lines = [
        f"{province} {city} — 天气预报",
        f"发布时间: {publish_time}",
        "",
    ]

    now = _get_china_now()
    today_str = now.strftime("%Y-%m-%d")

    _WEEKDAY_CN = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]

    found_forecast = False
    for item in details:
        date_str = item.get("date", "")
        if not date_str:
            continue

        # 如果指定了目标日期，只返回该日期
        if target_date and date_str != target_date:
            continue

        # 计算星期和相对日期
        try:
            dt = datetime.strptime(date_str, "%Y-%m-%d")
            weekday = _WEEKDAY_CN[dt.weekday()]
            delta = (dt.date() - now.date()).days
            if delta == 0:
                rel = "（今天）"
            elif delta == 1:
                rel = "（明天）"
            elif delta == 2:
                rel = "（后天）"
            elif delta == -1:
                rel = "（昨天）"
            else:
                rel = ""
        except ValueError:
            weekday = ""
            rel = ""

        day = item.get("day", {})
        night = item.get("night", {})
        day_w = day.get("weather", {})
        night_w = night.get("weather", {})
        day_wind = day.get("wind", {})
        night_wind = night.get("wind", {})
        precipitation = item.get("precipitation", 0)

        day_info = day_w.get("info", "")
        night_info = night_w.get("info", "")
        day_temp = day_w.get("temperature", "")
        night_temp = night_w.get("temperature", "")

        # 跳过无效数据（9999 表示无数据）
        if day_info == _NMC_NO_DATA:
            day_info = ""
        if night_info == _NMC_NO_DATA:
            night_info = ""
        if day_temp == _NMC_NO_DATA:
            day_temp = ""
        if night_temp == _NMC_NO_DATA:
            night_temp = ""

        # 格式化天气描述
        if day_info and night_info and day_info != night_info:
            weather_desc = f"{day_info}转{night_info}"
        elif day_info:
            weather_desc = day_info
        elif night_info:
            weather_desc = night_info
        else:
            weather_desc = "暂无数据"

        # 温度范围
        if day_temp and night_temp:
            temp_desc = f"{night_temp}°C ~ {day_temp}°C"
        elif day_temp:
            temp_desc = f"最高 {day_temp}°C"
        elif night_temp:
            temp_desc = f"最低 {night_temp}°C"
        else:
            temp_desc = "暂无数据"

        # 风况
        day_wind_str = day_wind.get("direct", "")
        night_wind_str = night_wind.get("direct", "")
        day_power = day_wind.get("power", "")
        if day_wind_str == _NMC_NO_DATA:
            day_wind_str = ""
        if night_wind_str == _NMC_NO_DATA:
            night_wind_str = ""

        wind_desc = ""
        if day_wind_str and day_power:
            wind_desc = f"{day_wind_str} {day_power}"
        elif night_wind_str:
            night_power = night_wind.get("power", "")
            wind_desc = f"{night_wind_str} {night_power}"

        line = f"{date_str} {weekday}{rel}  |  {weather_desc}  |  {temp_desc}"
        if wind_desc:
            line += f"  |  {wind_desc}"
        if precipitation and precipitation > 0:
            line += f"  |  降水: {precipitation}mm"
        lines.append(line)
        found_forecast = True

    if not found_forecast:
        lines.append("暂无该日期的预报数据")

    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────
# ─────────────────────────────────────────────────────────────

async def get_weather(city: str, date: str = "today") -> str:
    """查询中国城市的天气预报信息（数据来源：中央气象台）。

    默认查询中国城市，返回实况天气和未来7天预报。
    时间以中国标准时间（北京时间 UTC+8）为准。

    Args:
        city: 中国城市名称，如"北京"、"上海"、"深圳"、"成都"等
        date: 查询日期，支持以下格式：
              - "today" 或 "今天"：返回实况 + 当日预报（默认）
              - "tomorrow" 或 "明天"：返回明天的预报
              - "后天"：返回后天的预报
              - "week" 或 "一周" 或 "7天"：返回未来7天预报
              - "YYYY-MM-DD" 格式：返回指定日期的预报

    Returns:
        格式化的天气信息字符串
    """
    if not city or not city.strip():
        return "无法获取天气：请提供要查询的城市名称，例如「北京」、「上海」"

    city = city.strip()

    # 去掉可能附带的"市"/"区"/"县"后缀
    for suffix in ("市", "区", "县"):
        if city.endswith(suffix) and len(city) > 2:
            city_short = city[:-1]
            if city_short in _BUILTIN_CITY_CODES or city_short in _city_code_cache:
                city = city_short
                break

    # 解析城市代码
    code = _resolve_city_code(city)
    if not code:
        return (
            f"无法获取天气：未找到城市「{city}」。\n"
            "请确认城市名称是否正确（直接输入城市名即可，如「北京」「成都」「三亚」）。\n"
            "目前仅支持中国大陆、香港、澳门、台湾的城市。"
        )

    # 获取当前中国时间
    now = _get_china_now()
    today_str = now.strftime("%Y-%m-%d")

    # 解析日期参数
    date = (date or "today").strip().lower()
    show_realtime = False
    target_date = None
    show_all_forecast = False

    if date in ("today", "今天"):
        show_realtime = True
        target_date = today_str
    elif date in ("tomorrow", "明天"):
        target_date = (now + timedelta(days=1)).strftime("%Y-%m-%d")
    elif date in ("后天",):
        target_date = (now + timedelta(days=2)).strftime("%Y-%m-%d")
    elif date in ("week", "一周", "7天", "七天", "all", "全部"):
        show_all_forecast = True
        show_realtime = True
    else:
        # 尝试解析为 ISO 日期
        try:
            parsed = datetime.strptime(date, "%Y-%m-%d")
            target_date = parsed.strftime("%Y-%m-%d")
            # 如果是今天，也显示实况
            if target_date == today_str:
                show_realtime = True
        except ValueError:
            return (
                f"无法获取天气：日期格式「{date}」无法识别。\n"
                "支持的格式：今天、明天、后天、一周/7天、YYYY-MM-DD"
            )

    # 调用中央气象台 API
    try:
        data = _http_get_json(f"{_NMC_WEATHER_URL}?stationid={code}")
    except urllib.error.HTTPError as e:
        logger.error("[weather] HTTP 错误: %s (city=%s, code=%s)", e, city, code)
        return f"无法获取天气：网络请求失败（HTTP {e.code}），请稍后重试"
    except urllib.error.URLError as e:
        logger.error("[weather] 网络错误: %s (city=%s, code=%s)", e, city, code)
        return "无法获取天气：网络连接失败，请检查网络后重试"
    except Exception as e:
        logger.error("[weather] 未知错误: %s (city=%s, code=%s)", e, city, code)
        return f"无法获取天气：查询出错（{e}），请稍后重试"

    if data.get("code") != 0 or not data.get("data"):
        return f"无法获取天气：中央气象台未返回「{city}」的数据，请稍后重试"

    weather_data = data["data"]

    # 构建输出
    parts = []

    # 当前时间标注
    parts.append(f"查询时间: {now.strftime('%Y-%m-%d %H:%M')} (北京时间)\n")

    # 实况天气
    if show_realtime and weather_data.get("real"):
        parts.append(_format_realtime(weather_data["real"]))

    # 预报天气
    if weather_data.get("predict"):
        if show_all_forecast:
            parts.append("")
            parts.append(_format_forecast(weather_data["predict"]))
        elif target_date:
            parts.append("")
            parts.append(_format_forecast(weather_data["predict"], target_date))

    result = "\n".join(parts).strip()

    if not result or result == f"查询时间: {now.strftime('%Y-%m-%d %H:%M')} (北京时间)":
        return f"无法获取天气：暂无「{city}」的天气数据"

    return result


def main() -> int:
    """命令行入口：python weather.py <城市> [日期]"""
    parser = argparse.ArgumentParser(
        description="查询中国城市天气（数据来源：中央气象台 www.nmc.cn）"
    )
    parser.add_argument("city", help="城市名，如：北京、深圳（不带「市」后缀）")
    parser.add_argument(
        "date",
        nargs="?",
        default="today",
        help="日期：today/今天、tomorrow/明天、后天、week/一周/7天、YYYY-MM-DD（默认 today）",
    )
    args = parser.parse_args()

    print(asyncio.run(get_weather(args.city, args.date)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
