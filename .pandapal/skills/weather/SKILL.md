---
name: "weather"
description: "查询中国城市的实况天气和未来7天天气预报信息，数据来源中央气象台"
when_to_use: >
  Use when the user asks about weather, temperature, forecast, or climate conditions.
  Examples: '北京今天天气', '上海明天会下雨吗', '今天冷不冷',
  '需要带伞吗', '天气怎么样', '查天气', '这周天气如何'
allowed-tools: Bash
---

# 天气预报技能

## 数据来源

中国中央气象台（www.nmc.cn）公开 REST API，免费、无需 API Key。

## 处理规则

1. 从用户消息中提取**城市名称**和**日期**
2. 用 bash 调用 `scripts/weather.py` 获取天气（见「实现脚本」）
3. 如果返回以"无法获取天气："开头的提示：**原样回复该提示**，不要调用其它联网工具
4. 否则用自然语言回复天气情况，可适当加入穿衣、出行建议

## 实现脚本（必须使用，勿删）

天气查询的唯一实现是 `scripts/weather.py`（中央气象台免费 API，无需 Key），**必须通过它查询，不要用网络搜索替代**。

**推荐：命令行入口**（输出纯文本，兼容 Windows GBK / UTF-8 终端）：

```bash
cd <skill_dir>/scripts && python weather.py 深圳 today
```

- 参数 1 = 城市名（直接传中文，如 `北京`、`深圳`，不带"市"后缀）
- 参数 2 = 日期，可选（默认 `today`）：`today`/`今天`、`tomorrow`/`明天`、`后天`、`week`/`一周`/`7天`、`YYYY-MM-DD`
- 失败时返回以 `无法获取天气：` 开头的提示 → 原样回复，不换其它工具

**备选：库式调用**（需要对结果二次加工时）：

```bash
cd <skill_dir>/scripts && python -c "import asyncio; import weather as w; print(asyncio.run(w.get_weather('北京', 'today')))"
```

- `<skill_dir>`：本技能目录。通常为 `.pandapal/skills/weather`（相对工作区根目录）；定位不到时用 `dir /s /b weather.py` 搜索
- 脚本内部已处理城市不存在 / 网络失败 / 日期格式错误等异常，返回以 `无法获取天气：` 开头 → 原样回复，不换其它工具

## 参数说明

### city 参数
- 直接传入中文城市名，如 `北京`、`深圳`、`成都`
- 不需要加"市"后缀，直接传城市名即可
- 默认查询**中国**城市（覆盖大陆、香港、澳门、台湾）
- 内置 300+ 热门城市快速匹配，其他城市自动在线查找

### date 参数
- `today` 或 `今天`：返回实况天气 + 当日预报（**默认值**）
- `tomorrow` 或 `明天`：返回明天的预报
- `后天`：返回后天的预报
- `week` 或 `一周` 或 `7天`：返回未来7天完整预报
- ISO 日期格式 `YYYY-MM-DD`：返回指定日期的预报

## 注意事项

- 所有时间均以**中国标准时间（北京时间 UTC+8）**为准
- 如果用户未指定具体城市（如只说"天气怎么样"），需向用户追问具体城市
- 若网络不可达或 API 请求失败，将返回"无法获取天气"的提示，不会输出模拟数据
