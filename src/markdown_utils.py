"""Markdown / YAML frontmatter 公共辅助。

供 storage、llm 等模块复用,避免重复实现。本模块零业务依赖,仅依赖 yaml/json/re。
"""

import json
import re
from typing import Any, Dict, List, Tuple

import yaml

__all__ = [
    "yaml_value",
    "dump_frontmatter",
    "parse_frontmatter",
    "normalize_str_list",
]


def yaml_value(v: Any) -> str:
    """把单个值序列化为 YAML 合法的 token,借道 JSON 语法。

    依据:JSON 是 YAML 1.2 的真子集,任何 json.dumps 的输出都是合法 YAML 标量/序列/映射。
    始终带引号的字符串可以避免 PyYAML 的若干怪癖(折行、未引号字符串歧义、unicode 转义)。
    """
    if isinstance(v, (dict, list, str)):
        return json.dumps(v, ensure_ascii=False)
    if isinstance(v, bool):
        return "true" if v else "false"
    if v is None:
        return ""
    return str(v)


def dump_frontmatter(meta: Dict) -> str:
    """把扁平 metadata dict 序列化为 frontmatter 文本(不含包围的 `---`)。

    保留插入顺序(title 在前,bookkeeping 字段在后)。仅支持扁平结构 —— 当前所有
    metadata 都是扁平的,无需处理嵌套。
    """
    return "".join(f"{k}: {yaml_value(v)}\n" for k, v in meta.items())


_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", re.DOTALL)


def parse_frontmatter(text: str) -> Tuple[Dict, str]:
    """从 markdown 文本中分离 YAML frontmatter 与正文。

    返回 (metadata_dict, body)。无 frontmatter / YAML 解析失败 / 非 dict 时返回
    ({}, 原文) —— 保证降级路径不丢内容,调用方可凭 metadata_dict 是否为空判定。
    """
    text_stripped = text.strip()
    match = _FRONTMATTER_RE.match(text_stripped)
    if match:
        try:
            meta = yaml.safe_load(match.group(1)) or {}
            if isinstance(meta, dict):
                return meta, match.group(2).strip()
        except yaml.YAMLError:
            pass

    # 宽松解析：处理模型直接输出 key: value 元数据且不带 --- 包围的情况
    lines = text_stripped.split("\n")
    if lines:
        first_line = lines[0].strip()
        meta_keys = ("title:", "title：", "lead:", "lead：", "highlights:", "highlights：")
        if any(first_line.lower().startswith(k) for k in meta_keys):
            yaml_lines = []
            body_lines = []
            in_body = False
            for line in lines:
                if not in_body:
                    stripped_line = line.strip()
                    # 识别到正文的起始标志
                    if stripped_line.startswith(("#", "###", "##", "1. ", "### 1.")):
                        in_body = True
                        body_lines.append(line)
                    else:
                        yaml_lines.append(line)
                else:
                    body_lines.append(line)
            
            if yaml_lines and body_lines:
                # 统一替换中文冒号为英文冒号+空格，并确保所有 YAML 键的冒号后有空格
                yaml_content = "\n".join(yaml_lines).replace("：", ": ")
                yaml_content = re.sub(r"^(\s*\w+):([^\s])", r"\1: \2", yaml_content, flags=re.MULTILINE)
                try:
                    meta = yaml.safe_load(yaml_content) or {}
                    if isinstance(meta, dict) and any(k in meta for k in ("title", "lead", "highlights")):
                        return meta, "\n".join(body_lines).strip()
                except yaml.YAMLError:
                    pass

    return {}, text


def normalize_str_list(value: Any) -> List[str]:
    """将 str/list/None 规整为非空字符串列表。

    用于兜底 LLM 输出的列表型字段(如 frontmatter 中的 highlights):单字符串包裹为单元素列表,
    null/非列表/非字符串返回空列表,列表中夹杂的空白项过滤掉。不做数量截断。
    """
    if not value:
        return []
    if isinstance(value, str):
        items = [value]
    elif isinstance(value, list):
        items = value
    else:
        return []
    return [str(x).strip() for x in items if str(x).strip()]
