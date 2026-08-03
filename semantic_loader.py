"""
Carga la capa semántica desde siigo-api/core/semantic (montada o ruta local).

Env:
  SEMANTIC_CATALOG_DIR — directorio con catalog.yaml y schema_context.md
"""

from __future__ import annotations

import logging
import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:
    yaml = None  # type: ignore

logger = logging.getLogger("agents-poc.semantic")

_CATALOG_FILENAME = "catalog.yaml"
_CONTEXT_FILENAME = "schema_context.md"


def _candidate_dirs() -> list[Path]:
    here = Path(__file__).resolve().parent
    env_dir = os.getenv("SEMANTIC_CATALOG_DIR", "").strip()
    candidates = []
    if env_dir:
        candidates.append(Path(env_dir))
    candidates.extend(
        [
            here / "semantic",
            here.parent / "siigo-api" / "core" / "semantic",
            Path("/app/semantic"),
        ]
    )
    out: list[Path] = []
    seen: set[str] = set()
    for p in candidates:
        key = str(p.resolve()) if p.exists() else str(p)
        if key not in seen:
            seen.add(key)
            out.append(p)
    return out


@lru_cache(maxsize=1)
def get_semantic_dir() -> Path:
    for directory in _candidate_dirs():
        if (directory / _CATALOG_FILENAME).is_file():
            logger.info("semantic catalog dir=%s", directory)
            return directory
    tried = ", ".join(str(d) for d in _candidate_dirs())
    raise FileNotFoundError(
        f"Semantic catalog not found (catalog.yaml). Tried: {tried}. "
        "Set SEMANTIC_CATALOG_DIR or mount siigo-api/core/semantic."
    )


@lru_cache(maxsize=1)
def load_catalog() -> dict[str, Any]:
    if yaml is None:
        raise RuntimeError("PyYAML required: add pyyaml to requirements.txt")
    path = get_semantic_dir() / _CATALOG_FILENAME
    with path.open(encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        raise ValueError(f"Invalid catalog: {path}")
    return data


@lru_cache(maxsize=1)
def load_schema_context_text() -> str:
    path = get_semantic_dir() / _CONTEXT_FILENAME
    if not path.is_file():
        raise FileNotFoundError(f"Missing schema context: {path}")
    return path.read_text(encoding="utf-8").strip()


def get_kpi_entity_key(catalog: dict[str, Any] | None = None) -> str:
    cat = catalog or load_catalog()
    return str(cat.get("kpi_entity") or "vw_kpis_financiero")


def get_gold_schema(catalog: dict[str, Any] | None = None) -> str:
    cat = catalog or load_catalog()
    return str(cat.get("gold_schema") or "gold")


def get_table_map(catalog: dict[str, Any] | None = None) -> dict[str, str]:
    cat = catalog or load_catalog()
    entities = cat.get("entities") or {}
    out: dict[str, str] = {}
    for key, meta in entities.items():
        if isinstance(meta, dict):
            out[str(key)] = str(meta.get("description") or key)
    return out


def get_gold_route_keys(catalog: dict[str, Any] | None = None) -> frozenset[str]:
    cat = catalog or load_catalog()
    entities = cat.get("entities") or {}
    keys: set[str] = set()
    for key, meta in entities.items():
        if isinstance(meta, dict) and meta.get("layer") == "gold":
            keys.add(str(meta.get("route_key") or key))
    return frozenset(keys)


def qualified_source(table: str, schema: str, catalog: dict[str, Any] | None = None) -> str:
    cat = catalog or load_catalog()
    gold = get_gold_schema(cat)
    if table in get_gold_route_keys(cat):
        return f"{gold}.{table}"
    entities = cat.get("entities") or {}
    meta = entities.get(table) if isinstance(entities, dict) else None
    if isinstance(meta, dict) and meta.get("qualified_name"):
        return str(meta["qualified_name"])
    pattern = meta.get("qualified_pattern") if isinstance(meta, dict) else None
    if pattern:
        return str(pattern).format(schema=schema)
    return f"{schema}.{table}"


def qualified_sources(tables: list[str], schema: str) -> list[str]:
    return [qualified_source(t, schema) for t in tables]


def build_kpi_question_pattern(catalog: dict[str, Any] | None = None) -> re.Pattern[str]:
    cat = catalog or load_catalog()
    routing = cat.get("routing") or {}
    keywords = routing.get("kpi_keywords") or []
    parts: list[str] = []
    for kw in keywords:
        kw = str(kw).strip()
        if not kw:
            continue
        escaped = re.escape(kw)
        if " " in kw:
            escaped = escaped.replace(r"\ ", r"\s+")
        parts.append(escaped)
    if not parts:
        parts = [r"kpi", r"margen", r"utilidad"]
    return re.compile(rf"\b({'|'.join(parts)})\b", re.IGNORECASE)


def build_router_entities_block(catalog: dict[str, Any] | None = None) -> str:
    cat = catalog or load_catalog()
    routing = cat.get("routing") or {}
    order = routing.get("router_order") or list((cat.get("entities") or {}).keys())
    entities = cat.get("entities") or {}
    lines: list[str] = []
    for idx, key in enumerate(order, start=1):
        meta = entities.get(key) if isinstance(entities, dict) else None
        if not isinstance(meta, dict):
            continue
        desc = meta.get("description") or key
        prefer = meta.get("prefer_for") or []
        suffix = f" — preferir: {', '.join(prefer)}" if prefer else ""
        lines.append(f"{idx}. {key} → {desc}{suffix}")
    return "\n".join(lines)


def build_routing_rules_block(catalog: dict[str, Any] | None = None) -> str:
    cat = catalog or load_catalog()
    routing = cat.get("routing") or {}
    rules = routing.get("rules") or []
    return "\n".join(f"- {r}" for r in rules)


def build_intents_list(catalog: dict[str, Any] | None = None) -> str:
    cat = catalog or load_catalog()
    routing = cat.get("routing") or {}
    intents = routing.get("intents") or ["mixto"]
    return " | ".join(str(i) for i in intents)


def reload_semantic_cache() -> None:
    """Clear caches (tests / hot reload)."""
    get_semantic_dir.cache_clear()
    load_catalog.cache_clear()
    load_schema_context_text.cache_clear()
