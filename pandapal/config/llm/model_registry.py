"""pandapal/config/model_registry.py — 可用模型清单（从用户凭据派生）。

设计约束（与 PRD §模型管理 三条规则一致）：
    1. provider 固定：只支持 BUILTIN_PROVIDERS 内的 provider（前端下拉选取）。
    2. model_id 完全用户填：系统不内置任何模型数据，用户填什么存什么、用什么。
       可用模型清单 = 用户已配置的凭据列表，每组凭据对应一个可用模型。
    3. 系统配置 toml 与用户配置 toml 分开：本模块不含任何模型数据，
       模型数据来自用户配置的 llm_credentials.toml（通过 CredentialStore 读取）。

本模块替代旧设计：
    - 删除 _DECLARED_MODELS 白名单（规则 2：系统不内置模型）
    - 删除「已声明 ∩ 已配置凭证」的交集逻辑（规则 2：用户填什么就能用什么）
    - resolve_available_models 改为从用户凭据直接派生
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from pandapal.config.llm.model_prices import ModelPrice, resolve_effective_price

logger = logging.getLogger("pandapal.config.model_registry")


@dataclass(frozen=True)
class AvailableModel:
    """一个可用模型的声明（从用户凭据派生，不可变）。

    展示名 = model_id 本身（规则 2：系统不内置模型数据，不做美化映射）。
    """

    model_id: str        # 路由键；用户填的 model_id
    display_name: str    # 前端展示名 = model_id
    provider: str        # dashscope / volcengine / openai / deepseek
    price_source: str    # "user" | "system" | "missing"
    price: ModelPrice | None = None  # 生效单价（CNY/1k）；missing 时为 None

    @property
    def needs_price(self) -> bool:
        """是否处于「待补价」状态。

        正常路径下恒为 False——保存期已拦下无单价来源的模型。为 True 只可能是
        系统默认表升级后移除了该模型（PRD·R10），此时不阻断已有会话，
        但界面须显示「待补价」徽标，且其消费进未定价兜底桶。
        """
        return self.price_source == "missing"


def resolve_available_models(
    credentials: list[dict[str, Any]],
) -> list[AvailableModel]:
    """从用户凭据派生可用模型清单。

    - 遍历凭据列表，每组凭据生成一个 AvailableModel
    - display_name = model_id（规则 2：系统不内置模型数据）
    - is_default 的凭据对应的模型置于首位
    - 空凭据返回空列表

    这是「装配 LLMRouter」与「下发前端 MODEL_LIST」共同的真相源，二者调用同一函数，
    保证「能选的」与「能路由的」严格一致。

    Args:
        credentials: ``CredentialStore.load_all()`` 返回的凭据字典列表，
            每项含 provider / api_key / model_id / base_url / is_default
    """
    if not credentials:
        return []

    result: list[AvailableModel] = []
    default_model: AvailableModel | None = None

    for cred in credentials:
        model_id = cred.get("model_id", "")
        provider = cred.get("provider", "")
        if not model_id or not provider:
            continue

        # 单价三级回落。保存期已保证有解，此处再解一次是为了拿到「来源」标记
        # （前端要区分「系统默认价」与「我填的价」），同时兜住 R10：系统默认表
        # 升级后移除了某模型 → price 为 None → 转「待补价」，但**不阻断使用**。
        try:
            price = resolve_effective_price(
                model_id,
                cred.get("input_price_per_1k"),
                cred.get("output_price_per_1k"),
                cred.get("cache_read_price_per_1k"),
                user_peak_input_price=cred.get("peak_input_price_per_1k"),
                user_peak_output_price=cred.get("peak_output_price_per_1k"),
                user_peak_cache_price=cred.get("peak_cache_read_price_per_1k"),
            )
        except ValueError as e:
            # 半套价等非法组合：不阻断装配（用户已能用这个模型了），
            # 但按「待补价」处理并留痕，由界面引导补填。
            logger.warning(
                "[model-registry] %s 单价非法，按待补价处理：%s", model_id, e
            )
            price = None

        model = AvailableModel(
            model_id=model_id,
            display_name=model_id,  # 规则 2：展示名 = model_id
            provider=provider,
            price_source=price.source if price is not None else "missing",
            price=price,
        )
        if price is None:
            logger.warning(
                "[model-registry] %s 无单价来源，转「待补价」；"
                "其消费将进入未定价兜底桶，不计入预算",
                model_id,
            )
        if cred.get("is_default"):
            default_model = model
        else:
            result.append(model)

    if default_model is not None:
        result.insert(0, default_model)

    return result


def get_default_model_id(available: list[AvailableModel]) -> str:
    """从可用模型清单提取默认 model_id（清单首项，空清单返回空串）。

    清单首项是 is_default 凭据对应的模型（由 resolve_available_models 保证）。
    """
    return available[0].model_id if available else ""


def is_available(model_id: str, available: list[AvailableModel]) -> bool:
    """model_id 是否在给定可用清单内（Layer 3 入站校验用）。

    替代旧 ``find_declared``：旧逻辑查白名单，新逻辑查用户已配置的凭据清单。
    """
    return any(a.model_id == model_id for a in available)


def find_available(
    model_id: str, available: list[AvailableModel]
) -> AvailableModel | None:
    """按 model_id 查可用模型，未命中返回 None。

    替代旧 ``find_declared``：返回 provider 供入站校验使用。
    """
    for m in available:
        if m.model_id == model_id:
            return m
    return None


def to_model_list_payload(
    available: list[AvailableModel], default_model_id: str
) -> dict:
    """组装下发前端的 MODEL_LIST 消息体。

    字段与前端 types/api.ts 的 ModelListPayload 严格一致（协议契约，须同步维护）。
    """
    return {
        "models": [
            {
                "model_id": m.model_id,
                "display_name": m.display_name,
                "provider": m.provider,
                # 供前端区分「系统默认价 / 我填的价 / 待补价」——用户需要知道
                # 账目反映的是估算价还是自己的真实采购价（PRD·Story 4）。
                "price_source": m.price_source,
            }
            for m in available
        ],
        "default_model_id": default_model_id,
    }


def build_price_book(
    available: list[AvailableModel],
) -> dict[str, ModelPrice]:
    """从可用模型清单构建价格账本，供 `install_price_book` 装配。

    只收录有确定单价的模型；「待补价」模型不进账本——其消费会在 `cost_of_call`
    落入未定价兜底桶并告警，这正是我们希望被观测到的信号。

    「能路由的」与「能计费的」由同一份 available 清单派生，保证口径一致。
    """
    return {m.model_id: m.price for m in available if m.price is not None}
