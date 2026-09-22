# -*- coding: utf-8 -*-
"""
图谱关系类型共用常量与工具函数（图谱子域）。

RelationType 枚举在 agent.rag.schema 单点定义，本模块从 schema 引用；
本模块提供关键词/展示映射及工具函数。关系类型分组（RELATION_GROUPS）仅 query_intent_classifier 使用，已放在该模块内。
使用方：search.query_intent_classifier、reasoning.graph_summary_for_prompt 等。
"""

from typing import Dict, Optional, Tuple

import logging

# 显式 re-export：RelationType 单点定义在 schema，本模块对外转出供 store.graph 等使用
from ...schema import RelationType as RelationType

logger = logging.getLogger(__name__)

# ==================== 关键词与展示映射 ====================

_RELATION_KEYWORD_MAP: Dict[str, str] = {
    "师徒": "MASTER", "师父": "MASTER", "师傅": "MASTER", "老师": "MASTER",
    "弟子": "MASTER", "徒弟": "MASTER", "学生": "MASTER", "MASTER": "MASTER",
    "朋友": "FRIEND", "好友": "FRIEND", "伙伴": "FRIEND", "FRIEND": "FRIEND",
    "敌人": "ENEMY", "仇敌": "ENEMY", "敌对": "ENEMY", "ENEMY": "ENEMY",
    "家族": "FAMILY", "血缘": "FAMILY", "父子": "FAMILY", "母子": "FAMILY",
    "兄弟": "FAMILY", "姐妹": "FAMILY", "FAMILY": "FAMILY",
    "夫妻": "SPOUSE", "道侣": "SPOUSE", "配偶": "SPOUSE", "SPOUSE": "SPOUSE",
    "同门": "SCHOOLFELLOW", "同派": "SCHOOLFELLOW", "同帮": "SCHOOLFELLOW",
    "同盟": "SCHOOLFELLOW", "同伙": "SCHOOLFELLOW", "SCHOOLFELLOW": "SCHOOLFELLOW",
    "上下级": "SUBORDINATE", "上级": "SUBORDINATE", "下属": "SUBORDINATE",
    "老板": "SUBORDINATE", "员工": "SUBORDINATE", "主人": "SUBORDINATE",
    "奴隶": "SUBORDINATE", "君主": "SUBORDINATE", "臣子": "SUBORDINATE",
    "将军": "SUBORDINATE", "士兵": "SUBORDINATE", "掌门": "SUBORDINATE",
    "SUBORDINATE": "SUBORDINATE",
    "盟友": "ALLY", "合作": "ALLY", "ALLY": "ALLY",
    "前辈": "MENTOR", "指导": "MENTOR", "提携": "MENTOR", "传授": "MENTOR",
    "MENTOR": "MENTOR",
    "对手": "RIVAL", "竞争": "RIVAL", "较劲": "RIVAL", "RIVAL": "RIVAL",
    "恋人": "LOVER", "情侣": "LOVER", "暧昧": "LOVER", "LOVER": "LOVER",
    "邻居": "NEIGHBOR", "同乡": "NEIGHBOR", "同村": "NEIGHBOR", "NEIGHBOR": "NEIGHBOR",
    "债主": "CREDITOR", "债权人": "CREDITOR", "CREDITOR": "CREDITOR",
    "债务人": "DEBTOR", "欠债": "DEBTOR", "DEBTOR": "DEBTOR",
    "居住": "RESIDES_IN", "修炼": "RESIDES_IN", "闭关": "RESIDES_IN",
    "住在": "RESIDES_IN", "定居": "RESIDES_IN", "RESIDES_IN": "RESIDES_IN",
    "出生": "BORN_IN", "生于": "BORN_IN", "出生地": "BORN_IN", "BORN_IN": "BORN_IN",
    "活动": "ACTIVITY_AT", "学习": "ACTIVITY_AT", "偷听": "ACTIVITY_AT",
    "读书": "ACTIVITY_AT", "劳作": "ACTIVITY_AT", "学艺": "ACTIVITY_AT",
    "ACTIVITY_AT": "ACTIVITY_AT",
    "途经": "PASSES_THROUGH", "经过": "PASSES_THROUGH", "路过": "PASSES_THROUGH",
    "路过某地": "PASSES_THROUGH", "穿过": "PASSES_THROUGH", "走过": "PASSES_THROUGH",
    "PASSES_THROUGH": "PASSES_THROUGH",
    "统治": "RULES", "管理": "RULES", "管辖": "RULES", "掌控": "RULES", "RULES": "RULES",
    "守护": "GUARDIAN_OF", "保护": "GUARDIAN_OF", "镇守": "GUARDIAN_OF",
    "守卫": "GUARDIAN_OF", "GUARDIAN_OF": "GUARDIAN_OF",
    "属于": "BELONGS_TO", "加入": "BELONGS_TO", "成员": "BELONGS_TO",
    "门人": "BELONGS_TO", "BELONGS_TO": "BELONGS_TO",
    "创立": "FOUNDED", "创建": "FOUNDED", "建立": "FOUNDED", "成立": "FOUNDED",
    "FOUNDED": "FOUNDED",
    "领导": "LEADER_OF", "宗主": "LEADER_OF", "长老": "LEADER_OF", "首领": "LEADER_OF",
    "LEADER_OF": "LEADER_OF",
    "离开": "LEFT", "叛出": "LEFT", "脱离": "LEFT", "退出": "LEFT", "LEFT": "LEFT",
    "背叛": "BETRAYED", "叛变": "BETRAYED", "BETRAYED": "BETRAYED",
    "参与": "PARTICIPATES_IN", "参与事件": "PARTICIPATES_IN", "经历": "PARTICIPATES_IN",
    "PARTICIPATES_IN": "PARTICIPATES_IN",
    "触发": "TRIGGERS", "TRIGGERS": "TRIGGERS",
    "导致": "CAUSES", "因果": "CAUSES", "CAUSES": "CAUSES",
    "发生地": "OCCURS_AT", "发生地点": "OCCURS_AT", "发生在": "OCCURS_AT",
    "OCCURS_AT": "OCCURS_AT",
    "叙事焦点": "NARRATIVE_FOCUS", "视角": "NARRATIVE_FOCUS",
    "NARRATIVE_FOCUS": "NARRATIVE_FOCUS",
    "被选中": "SELECTED_BY", "际遇": "SELECTED_BY", "SELECTED_BY": "SELECTED_BY",
}

# 预排序的关键词列表（按长度降序），避免 extract_relation_keyword_from_text 每次调用重新排序
_SORTED_RELATION_KEYWORDS: Tuple[str, ...] = tuple(
    sorted(_RELATION_KEYWORD_MAP.keys(), key=len, reverse=True)
)

_RELATION_TYPE_DISPLAY_MAP: Dict[str, str] = {
    "RESIDES_IN": "居住/修炼（长期居住地）",
    "BORN_IN": "出生地",
    "ACTIVITY_AT": "活动/学习地点",
    "PASSES_THROUGH": "途经",
    "MASTER": "师徒", "FRIEND": "朋友", "ENEMY": "敌人", "FAMILY": "家族",
    "SPOUSE": "夫妻/道侣", "SCHOOLFELLOW": "同门", "SUBORDINATE": "上下级",
    "ALLY": "盟友", "MENTOR": "前辈指导", "RIVAL": "对手", "LOVER": "恋人",
    "NEIGHBOR": "邻居", "CREDITOR": "债主", "DEBTOR": "债务人",
    "RULES": "统治/管理", "GUARDIAN_OF": "守护",
    "BELONGS_TO": "属于", "FOUNDED": "创立", "LEADER_OF": "领导",
    "LEFT": "离开/叛出", "BETRAYED": "背叛",
    "PARTICIPATES_IN": "参与事件", "TRIGGERS": "触发", "CAUSES": "导致",
    "OCCURS_AT": "发生地点", "NARRATIVE_FOCUS": "叙事焦点", "SELECTED_BY": "被选中/际遇",
}


# ==================== 工具函数 ====================

def get_relation_type_display_label(relation_type: str) -> str:
    """
    获取关系类型的展示标签(检索/推理时对 LLM 的说明)。
    
    Args:
        relation_type: 关系类型(如 "MASTER")
        
    Returns:
        展示标签(如 "师徒"),未配置则返回原字符串
    """
    if not relation_type:
        return ""
    
    key = str(relation_type).strip().upper()
    return _RELATION_TYPE_DISPLAY_MAP.get(key, relation_type)


def match_relation_type_by_keyword(keyword: str, verbose: bool = False) -> Optional[str]:
    """
    根据关键词匹配关系类型(支持中文关键词与英文枚举值,含模糊包含匹配)。
    使用场景:查询分析阶段,从查询文本提取的关系关键词 → RelationType 枚举值。
    
    Args:
        keyword: 关键词(中文或英文)
        verbose: 是否打印调试信息
        
    Returns:
        匹配的关系类型,未匹配返回 None
    """
    if not keyword:
        return None
    
    keyword_original = keyword.strip()
    keyword_upper = keyword_original.upper()
    
    # 精确匹配
    if keyword_original in _RELATION_KEYWORD_MAP:
        matched = _RELATION_KEYWORD_MAP[keyword_original]
        if verbose:
            logger.debug("  关键词 '%s' 直接匹配: %s", keyword_original, matched)
        return matched
    
    if keyword_upper in _RELATION_KEYWORD_MAP:
        matched = _RELATION_KEYWORD_MAP[keyword_upper]
        if verbose:
            logger.debug("  关键词 '%s' 直接匹配: %s", keyword_original, matched)
        return matched
    
    # 模糊匹配
    for kw, rel_type in _RELATION_KEYWORD_MAP.items():
        if kw in keyword_original or keyword_original in kw:
            if verbose:
                logger.debug("  关键词 '%s' 模糊匹配: %s", keyword_original, rel_type)
            return rel_type
    
    if verbose:
        logger.warning("  无法匹配关系类型关键词: '%s'", keyword_original)
    return None


def extract_relation_keyword_from_text(text: str, verbose: bool = False) -> Optional[str]:
    """
    从文本中提取关系类型关键词(按长度从长到短优先匹配)。
    使用场景:查询分析阶段,从查询/分词结果中提取关系类型关键词。
    
    Args:
        text: 待提取文本
        verbose: 是否打印调试信息
        
    Returns:
        提取到的关键词,未找到返回 None
    """
    if not text:
        return None
    
    for keyword in _SORTED_RELATION_KEYWORDS:
        if keyword in text:
            if verbose:
                logger.debug("  从文本提取到关系关键词: '%s'", keyword)
            return keyword
    
    if verbose:
        logger.debug("  未从文本中提取到关系关键词")
    return None
