"""weather skill 脚本单元测试（mock 网络，不产生真实请求）。

覆盖 2026-08-19 优化引入的功能点：
- _http_get_json 网络重试（URLError/HTTPError 重试、JSON 解析失败不重试）
- _format_realtime 气压字段健壮性（str "9999" / 缺失 / 非数值不崩溃）
- _format_forecast 哨兵常量收编 + found_forecast 空结果提示
- main() 命令行入口（argparse）
- get_weather 集成路径（mock _http_get_json）

运行方式（从项目根）：
    pytest .pandapal/skills/weather/scripts/tests
"""

import importlib.util
import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

_SCRIPT = Path(__file__).resolve().parent.parent / "weather.py"
_spec = importlib.util.spec_from_file_location("weather_skill", _SCRIPT)
weather = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(weather)


# ─────────────────────────────────────────────
# 测试替身
# ─────────────────────────────────────────────

class FakeResp:
    """模拟 urllib.response 上下文管理器（with + read()）"""

    def __init__(self, payload: bytes):
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return self._payload


def _json_resp(obj: dict) -> FakeResp:
    return FakeResp(json.dumps(obj).encode("utf-8"))


def _make_http_error(url: str = "https://www.nmc.cn/rest/weather"):
    return __import__("urllib.error", fromlist=["HTTPError"]).HTTPError(
        url, 500, "server error", {}, None
    )


def _make_realtime_data() -> dict:
    return {
        "station": {"city": "深圳", "province": "广东省"},
        "publish_time": "2026-08-19 13:55",
        "weather": {
            "temperature": "32.7",
            "feelst": "36.1",
            "info": "多云",
            "humidity": "56.0",
            "rain": "0.0",
        },
        "wind": {"direct": "东南风", "power": "微风", "speed": "2.7"},
        "sunriseSunset": {"sunrise": "06:02", "sunset": "18:52"},
    }


def _make_forecast_data() -> dict:
    return {
        "station": {"city": "深圳", "province": "广东省"},
        "publish_time": "2026-08-19 12:00",
        "detail": [
            {
                "date": "2026-08-19",
                "day": {
                    "weather": {"info": "中雨", "temperature": "32"},
                    "wind": {"direct": "无持续风向", "power": "微风"},
                },
                "night": {
                    "weather": {"info": "雷阵雨", "temperature": "26"},
                    "wind": {"direct": "", "power": ""},
                },
                "precipitation": 10.6,
            }
        ],
    }


def _make_full_api_response() -> dict:
    return {"code": 0, "data": {"real": _make_realtime_data(), "predict": _make_forecast_data()}}


# ─────────────────────────────────────────────
# U1: _http_get_json 重试逻辑
# ─────────────────────────────────────────────

class TestHttpRetry:
    def test_success_no_retry(self):
        """一次成功：urlopen 只调用 1 次，直接返回解析后的 dict"""
        with patch("urllib.request.urlopen", return_value=_json_resp({"a": 1})) as mock_open:
            result = weather._http_get_json("https://www.nmc.cn/x")
        assert result == {"a": 1}
        assert mock_open.call_count == 1

    def test_urlerror_then_success_retries(self):
        """首次 URLError → 重试 → 第二次成功；urlopen 调用 2 次"""
        with patch("time.sleep") as mock_sleep, patch(
            "urllib.request.urlopen", side_effect=[__import__("urllib.error", fromlist=["URLError"]).URLError("boom"), _json_resp({"ok": True})]
        ) as mock_open:
            result = weather._http_get_json("https://www.nmc.cn/x")
        assert result == {"ok": True}
        assert mock_open.call_count == 2
        mock_sleep.assert_called_once_with(weather._HTTP_RETRY_DELAY)

    def test_http_error_then_success_retries(self):
        """HTTPError（URLError 子类）同样触发重试"""
        with patch("time.sleep"), patch(
            "urllib.request.urlopen", side_effect=[_make_http_error(), _json_resp({"ok": 1})]
        ) as mock_open:
            result = weather._http_get_json("https://www.nmc.cn/x")
        assert result == {"ok": 1}
        assert mock_open.call_count == 2

    def test_all_retries_fail_raises_last_error(self):
        """连续失败：重试 retries 次后抛出最后一次异常"""
        err = __import__("urllib.error", fromlist=["URLError"]).URLError("always down")
        with patch("time.sleep"), patch("urllib.request.urlopen", side_effect=err) as mock_open:
            with pytest.raises(__import__("urllib.error", fromlist=["URLError"]).URLError):
                weather._http_get_json("https://www.nmc.cn/x")
        assert mock_open.call_count == weather._HTTP_RETRIES

    def test_json_decode_error_no_retry(self):
        """返回非法 JSON：解析失败不重试（urlopen 只调 1 次），异常向上抛"""
        with patch("time.sleep"), patch(
            "urllib.request.urlopen", return_value=FakeResp(b"not-json{{{")
        ) as mock_open:
            with pytest.raises(json.JSONDecodeError):
                weather._http_get_json("https://www.nmc.cn/x")
        assert mock_open.call_count == 1


# ─────────────────────────────────────────────
# U2: _format_realtime 气压健壮性
# ─────────────────────────────────────────────

class TestRealtimePressure:
    def test_pressure_missing_no_line(self):
        """airpressure 缺失：不显示气压行，不崩溃"""
        data = _make_realtime_data()
        data["weather"].pop("airpressure", None)
        out = weather._format_realtime(data)
        assert "气压" not in out

    def test_pressure_str_no_data_sentinel(self):
        """airpressure 为字符串 '9999'（API 无数据哨兵）：不显示，不崩溃（原 bug 会 TypeError）"""
        data = _make_realtime_data()
        data["weather"]["airpressure"] = weather._NMC_NO_DATA
        out = weather._format_realtime(data)
        assert "气压" not in out

    def test_pressure_str_valid_shown(self):
        """airpressure 为字符串 '1005'：显示 1005 hPa"""
        data = _make_realtime_data()
        data["weather"]["airpressure"] = "1005"
        out = weather._format_realtime(data)
        assert "气压: 1005 hPa" in out

    def test_pressure_int_valid_shown(self):
        """airpressure 为 int 1005：显示 1005 hPa"""
        data = _make_realtime_data()
        data["weather"]["airpressure"] = 1005
        out = weather._format_realtime(data)
        assert "气压: 1005 hPa" in out

    def test_pressure_non_numeric_hidden(self):
        """airpressure 为非数值字符串：不显示气压行，不崩溃"""
        data = _make_realtime_data()
        data["weather"]["airpressure"] = "abc"
        out = weather._format_realtime(data)
        assert "气压" not in out


# ─────────────────────────────────────────────
# U3: _format_forecast 哨兵 + 空结果
# ─────────────────────────────────────────────

class TestForecast:
    def test_normal_forecast_format(self):
        """正常预报：包含日期、天气、温度，且不含 9999"""
        out = weather._format_forecast(_make_forecast_data(), "2026-08-19")
        assert "2026-08-19" in out
        assert "中雨转雷阵雨" in out
        assert "26°C ~ 32°C" in out
        assert weather._NMC_NO_DATA not in out

    def test_no_matching_date_shows_empty_hint(self):
        """target_date 无匹配：输出「暂无该日期的预报数据」"""
        out = weather._format_forecast(_make_forecast_data(), "2030-01-01")
        assert "暂无该日期的预报数据" in out

    def test_no_data_sentinel_fields_blanked(self):
        """天气/温度/风向为 9999：输出中不出现 9999"""
        data = _make_forecast_data()
        item = data["detail"][0]
        item["day"]["weather"] = {"info": weather._NMC_NO_DATA, "temperature": weather._NMC_NO_DATA}
        item["night"]["weather"] = {"info": weather._NMC_NO_DATA, "temperature": weather._NMC_NO_DATA}
        item["day"]["wind"] = {"direct": weather._NMC_NO_DATA, "power": ""}
        out = weather._format_forecast(data, "2026-08-19")
        assert weather._NMC_NO_DATA not in out
        assert "暂无数据" in out  # 天气信息全哨兵 → 回退"暂无数据"


# ─────────────────────────────────────────────
# U4: main() 命令行入口
# ─────────────────────────────────────────────

class TestMain:
    def test_help_exits_zero(self):
        """--help：SystemExit(0)"""
        with patch.object(sys, "argv", ["weather.py", "--help"]):
            with pytest.raises(SystemExit) as exc_info:
                weather.main()
        assert exc_info.value.code == 0

    def test_missing_city_exits_two(self):
        """缺 city 参数：argparse 报错 SystemExit(2)"""
        with patch.object(sys, "argv", ["weather.py"]):
            with pytest.raises(SystemExit) as exc_info:
                weather.main()
        assert exc_info.value.code == 2

    def test_valid_args_prints_weather(self, capsys):
        """city + date 正常：print 输出 get_weather 结果"""
        with patch.object(sys, "argv", ["weather.py", "深圳", "today"]), patch.object(
            weather, "get_weather", return_value="深圳天气OK"
        ):
            rc = weather.main()
        assert rc == 0
        assert capsys.readouterr().out.strip() == "深圳天气OK"


# ─────────────────────────────────────────────
# U5: get_weather 集成（mock 网络）
# ─────────────────────────────────────────────

class TestGetWeather:
    async def test_full_success(self):
        """完整成功路径：返回实况 + 预报文本"""
        with patch.object(weather, "_http_get_json", return_value=_make_full_api_response()):
            out = await weather.get_weather("深圳", "today")
        assert "实况天气" in out
        assert "天气预报" in out
        assert "32.7" in out
        assert "中雨转雷阵雨" in out

    async def test_empty_city_rejected(self):
        """空城市名：返回引导提示"""
        out = await weather.get_weather("  ", "today")
        assert out.startswith("无法获取天气：")

    async def test_unknown_city_rejected(self):
        """未找到城市：在线查找失败后返回「未找到城市」提示"""
        with patch.object(weather, "_http_get_json", side_effect=RuntimeError("offline")):
            out = await weather.get_weather("不存在城市x", "today")
        assert "未找到城市" in out

    async def test_bad_date_rejected(self):
        """非法日期格式：返回日期格式提示"""
        with patch.object(weather, "_http_get_json", return_value=_make_full_api_response()):
            out = await weather.get_weather("深圳", "bad-date")
        assert "日期格式" in out

    async def test_api_error_payload(self):
        """API 返回非 0 code：返回失败提示"""
        with patch.object(weather, "_http_get_json", return_value={"code": 500, "data": None}):
            out = await weather.get_weather("深圳", "today")
        assert out.startswith("无法获取天气：")

    async def test_network_error_returns_hint(self):
        """网络异常（_http_get_json 抛 URLError）：返回失败提示而非崩溃"""
        err = __import__("urllib.error", fromlist=["URLError"]).URLError("net down")
        with patch.object(weather, "_http_get_json", side_effect=err):
            out = await weather.get_weather("深圳", "today")
        assert out.startswith("无法获取天气：")
