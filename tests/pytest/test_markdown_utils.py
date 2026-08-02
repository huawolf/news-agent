import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from markdown_utils import parse_frontmatter


def test_parse_frontmatter_standard():
    text = """---
title: "Standard Title"
lead: "Standard Lead"
---
### 1. Item 1
Content 1"""
    meta, body = parse_frontmatter(text)
    assert meta == {"title": "Standard Title", "lead": "Standard Lead"}
    assert body == "### 1. Item 1\nContent 1"


def test_parse_frontmatter_relaxed():
    text = """title: "Relaxed Title"
lead: "Relaxed Lead"
highlights:
- "Point 1"
### 1. Item 1
Content 1"""
    meta, body = parse_frontmatter(text)
    assert meta == {
        "title": "Relaxed Title",
        "lead": "Relaxed Lead",
        "highlights": ["Point 1"],
    }
    assert body == "### 1. Item 1\nContent 1"


def test_parse_frontmatter_relaxed_chinese():
    text = """title："Relaxed Title"
lead："Relaxed Lead"
highlights：
- "Point 1"
1. Item 1
Content 1"""
    meta, body = parse_frontmatter(text)
    assert meta == {
        "title": "Relaxed Title",
        "lead": "Relaxed Lead",
        "highlights": ["Point 1"],
    }
    assert body == "1. Item 1\nContent 1"


def test_parse_frontmatter_none():
    text = "### 1. Item 1\nContent 1"
    meta, body = parse_frontmatter(text)
    assert meta == {}
    assert body == text
