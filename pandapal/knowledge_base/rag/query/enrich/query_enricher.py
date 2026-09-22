# -*- coding: utf-8 -*-
"""
查询富化模块（检索流程第二步）

在「意图识别」之后、「多路召回」之前执行一次，对 QueryAnalysis 做实体标准名/别名扩展。
产出 expanded_entities、standard_entity_names，并可将扩展名补入 keywords/segmented_words，
供向量、BM25、图谱三种检索共用。

流程位置：query → QueryIntentClassifier(理解) → enrich_query_analysis_once(富化) → 多路召回
"""

from typing import List, Optional, Set

import logging

from ...schema import QueryAnalysis, SchemaKeys
from ...store.graph.graph_store import GraphStore

logger = logging.getLogger(__name__)


def enrich_query_analysis_once(
    query_analysis: QueryAnalysis,
    graph_store: Optional[GraphStore] = None,
    verbose: bool = False,
) -> QueryAnalysis:
    """
    对 query_analysis 做一次富化（标准名/别名扩展），可被 Retriever 或仅图谱测试等调用。

    富化内容：
    - expanded_entities：标准名 + 该实体在图谱中的全部别名（去重）
    - standard_entity_names：每个查询实体对应的标准名（图谱关系/路径查询用）
    - keywords / segmented_words：在原有基础上追加 expanded 中的名字；向量使用 keywords 等，BM25 检索仅使用 segmented_words

    无图谱时降级：不查图，仅做去重，expanded_entities = standard_entity_names = raw_entities。
    """
    K = SchemaKeys
    raw_entities = []
    for e in query_analysis.get(K.QA_ENTITIES, []):
        if isinstance(e, dict) and e.get(K.ENT_TEXT):
            raw_entities.append(str(e.get(K.ENT_TEXT, "")).strip())
        elif isinstance(e, str) and e.strip():
            raw_entities.append(e.strip())


    if not graph_store:
        expanded = list(dict.fromkeys(raw_entities))
        return {**query_analysis, K.QA_EXPANDED_ENTITIES: expanded, K.QA_STANDARD_ENTITY_NAMES: expanded}

    expanded: List[str] = []
    standard_names: List[str] = []
    seen: Set[str] = set()
    for name in raw_entities:
        if not name:
            continue
        try:
            info = graph_store.get_entity_info(name)
            if info:
                std = info.get(K.ENT_NAME, name)
                if std not in standard_names:
                    standard_names.append(std)
                if std and std not in seen:
                    expanded.append(std)
                    seen.add(std)
                for alias in info.get(K.ENT_ALIASES) or []:
                    if alias and alias not in seen:
                        expanded.append(alias)
                        seen.add(alias)
            else:
                if name not in seen:
                    expanded.append(name)
                    seen.add(name)
                if name not in standard_names:
                    standard_names.append(name)
        except (KeyError, ValueError, AttributeError) as e:
            # 预期的数据问题（实体不存在、格式错误等）
            if verbose:
                logger.debug(" 扩展实体 %s 失败: %s，使用原名", name, e)
            if name not in seen:
                expanded.append(name)
                seen.add(name)
            if name not in standard_names:
                standard_names.append(name)
        except Exception as e:
            # 意外错误（数据库连接问题、系统错误等）
            logger.warning("扩展实体 %s 时发生意外错误: %s", name, e, exc_info=True)
            # 降级处理，使用原名
            if name not in seen:
                expanded.append(name)
                seen.add(name)
            if name not in standard_names:
                standard_names.append(name)

    expanded = expanded if expanded else raw_entities
    standard_names = standard_names if standard_names else raw_entities
    if verbose and raw_entities:
        logger.info(
" 上层统一富化（标准名+别名）: %d 实体 -> expanded %d, standard %d",
            len(raw_entities),
            len(expanded),
            len(standard_names),
        )

    # 优化：直接从原始数据创建集合，避免不必要的列表复制
    kw_raw = query_analysis.get(K.QA_KEYWORDS) or []
    seg_raw = query_analysis.get(K.QA_SEGMENTED_WORDS) or []
    
    kw_set = {k.strip().lower() for k in kw_raw if k}
    seg_set = {str(s).strip() for s in seg_raw if s and str(s).strip()}
    
    # 仅在需要修改时才复制列表
    kw = list(kw_raw)
    seg = list(seg_raw)
    for e in expanded:
        if not e:
            continue
        e_stripped = e.strip()
        if e_stripped.lower() not in kw_set:
            kw.append(e)
            kw_set.add(e_stripped.lower())
        if e_stripped not in seg_set:
            seg.append(e)
            seg_set.add(e_stripped)

    return {
        **query_analysis,
        K.QA_EXPANDED_ENTITIES: expanded,
        K.QA_STANDARD_ENTITY_NAMES: standard_names,
        K.QA_KEYWORDS: kw,
        K.QA_SEGMENTED_WORDS: seg,
    }
