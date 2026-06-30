#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Keyword group manager — persist named keyword sets so users can switch
between different crawl targets without losing the previous config.
"""
import json
import os
from datetime import datetime

GROUPS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "groups.json")


def _load():
    if not os.path.exists(GROUPS_FILE):
        return {"active": "", "groups": {}}
    try:
        with open(GROUPS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {"active": "", "groups": {}}


def _save(data):
    with open(GROUPS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _normalize_group(g):
    return {
        "keywords": list(dict.fromkeys(g.get("keywords", []) or [])),
        "hidden_keywords": list(dict.fromkeys(g.get("hidden_keywords", []) or [])),
        "max_notes": g.get("max_notes", 200),
        "updated_at": g.get("updated_at", ""),
    }


def list_groups():
    data = _load()
    groups = []
    for name, g in data["groups"].items():
        normalized = _normalize_group(g)
        groups.append({
            "name": name,
            "keywords": normalized["keywords"],
            "hidden_keywords": normalized["hidden_keywords"],
            "max_notes": normalized["max_notes"],
            "updated_at": normalized["updated_at"],
            "active": name == data["active"],
        })
    return groups


def get_active_group():
    data = _load()
    active = data["active"]
    if active and active in data["groups"]:
        g = _normalize_group(data["groups"][active])
        return g.get("keywords", []), g.get("max_notes", 200)
    return None, None


def save_group(name, keywords, max_notes, hidden_keywords=None):
    data = _load()
    if keywords is None:
        keywords = []
    old_group = data["groups"].get(name, {})
    if hidden_keywords is None:
        hidden_keywords = old_group.get("hidden_keywords", [])
    data["groups"][name] = {
        "keywords": list(dict.fromkeys(keywords)),
        "hidden_keywords": list(dict.fromkeys(hidden_keywords or [])),
        "max_notes": max_notes,
        "updated_at": datetime.now().isoformat(timespec="seconds"),
    }
    _save(data)
    return True


def rename_group(old_name, new_name):
    data = _load()
    old_name = (old_name or "").strip()
    new_name = (new_name or "").strip()
    if not old_name or not new_name or old_name not in data["groups"]:
        return False, "group not found"
    if new_name != old_name and new_name in data["groups"]:
        return False, "target group exists"
    if new_name == old_name:
        return True, ""
    items = {}
    for name, group in data["groups"].items():
        if name == old_name:
            items[new_name] = group
        else:
            items[name] = group
    data["groups"] = items
    if data.get("active") == old_name:
        data["active"] = new_name
    _save(data)
    return True, ""


def copy_group(source_name, new_name):
    data = _load()
    source_name = (source_name or "").strip()
    new_name = (new_name or "").strip()
    if not source_name or source_name not in data["groups"]:
        return False, "source group not found"
    if not new_name:
        return False, "name required"
    if new_name in data["groups"]:
        return False, "target group exists"
    copied = _normalize_group(data["groups"][source_name])
    copied["updated_at"] = datetime.now().isoformat(timespec="seconds")
    data["groups"][new_name] = copied
    data["active"] = new_name
    _save(data)
    return True, ""


def activate_group(name):
    data = _load()
    if name and name not in data["groups"]:
        return False
    data["active"] = name
    _save(data)
    return True


def delete_group(name):
    data = _load()
    if name not in data["groups"]:
        return False
    del data["groups"][name]
    if data["active"] == name:
        data["active"] = ""
        # Re-activate first remaining group
        if data["groups"]:
            data["active"] = next(iter(data["groups"].keys()))
    _save(data)
    return True
