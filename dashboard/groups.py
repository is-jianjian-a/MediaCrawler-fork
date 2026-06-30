#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Keyword group manager — persist named keyword sets so users can switch
between different crawl targets without losing the previous config.
"""
import json
import os

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


def list_groups():
    data = _load()
    groups = []
    for name, g in data["groups"].items():
        groups.append({
            "name": name,
            "keywords": g.get("keywords", []),
            "max_notes": g.get("max_notes", 200),
            "active": name == data["active"],
        })
    return groups


def get_active_group():
    data = _load()
    active = data["active"]
    if active and active in data["groups"]:
        g = data["groups"][active]
        return g.get("keywords", []), g.get("max_notes", 200)
    return None, None


def save_group(name, keywords, max_notes):
    data = _load()
    if keywords is None:
        keywords = []
    data["groups"][name] = {"keywords": keywords, "max_notes": max_notes}
    _save(data)
    return True


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
