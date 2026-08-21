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


def _has_catalog(directory: Path) -> bool:
    return (directory / _CATALOG_FILENAME).is_file()


def _candidate_dirs() -> list[Path]:
    here = Path(__file__).resolve().parent
    env_dir = os.getenv("SEMANTIC_CATALOG_DIR", "").strip()
    candidates: list[Path] = []

    if env_dir:
        env_path = Path(env_dir)
        if _has_catalog(env_path):
            candidates.append(env_path)
        else:
            logger.warning(
                "SEMANTIC_CATALOG_DIR=%s has no %s; using bundled/local fallbacks",
                env_dir,
                _CATALOG_FILENAME,
            )

    candidates.extend(
        [
            # Bundled in the Docker image (COPY semantic/ → /app/semantic).
            here / "semantic",
            # docker-compose mounts siigo-api here so it does not shadow /app/semantic.
            Path("/app/semantic-live"),
            here.parent / "siigo-api" / "core" / "semantic",
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


def get_primary_entity_key(catalog: dict[str, Any] | None = None) -> str:
    cat = catalog or load_catalog()
    return str(cat.get("primary_entity") or cat.get("kpi_entity") or "vw_dim_accounts")


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


def get_silver_fallbacks(table: str, catalog: dict[str, Any] | None = None) -> list[str]:
    """Silver tables to try when a gold view returns no rows."""
    cat = catalog or load_catalog()
    entities = cat.get("entities") or {}
    meta = entities.get(table) if isinstance(entities, dict) else None
    if not isinstance(meta, dict):
        return []
    raw = meta.get("silver_fallback") or []
    if not isinstance(raw, list):
        return []
    return [str(t).strip() for t in raw if str(t).strip()]


def query_candidates(primary_table: str, catalog: dict[str, Any] | None = None) -> list[str]:
    """Gold view first, then configured silver fallbacks (deduped, order preserved)."""
    cat = catalog or load_catalog()
    out: list[str] = []
    seen: set[str] = set()

    def add(name: str) -> None:
        key = name.strip()
        if key and key not in seen:
            seen.add(key)
            out.append(key)

    add(primary_table)
    if primary_table in get_gold_route_keys(cat):
        for fallback in get_silver_fallbacks(primary_table, cat):
            add(fallback)
    return out


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


def build_dim_catalog_question_pattern(catalog: dict[str, Any] | None = None) -> re.Pattern[str]:
    cat = catalog or load_catalog()
    routing = cat.get("routing") or {}
    keywords = routing.get("dim_catalog_keywords") or [
        "plan de cuentas",
        "plan contable",
    ]
    return _keywords_to_pattern(keywords)


def build_tablero_question_pattern(catalog: dict[str, Any] | None = None) -> re.Pattern[str]:
    cat = catalog or load_catalog()
    routing = cat.get("routing") or {}
    keywords = routing.get("tablero_keywords") or [
        "ingresos operacionales",
        "utilidad neta",
        "estado de resultados",
    ]
    return _keywords_to_pattern(keywords)


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


def build_ventas_netas_question_pattern(catalog: dict[str, Any] | None = None) -> re.Pattern[str]:
    cat = catalog or load_catalog()
    routing = cat.get("routing") or {}
    keywords = routing.get("ventas_netas_keywords") or []
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
        parts = [r"ventas\s+netas", r"notas\s+cr[eé]dito"]
    return re.compile(rf"({'|'.join(parts)})", re.IGNORECASE)


def _keywords_to_pattern(keywords: list[str], *, word_boundary: bool = False) -> re.Pattern[str]:
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
        return re.compile(r"a^")
    body = "|".join(parts)
    if word_boundary:
        return re.compile(rf"\b({body})\b", re.IGNORECASE)
    return re.compile(rf"({body})", re.IGNORECASE)


def build_balance_question_pattern(catalog: dict[str, Any] | None = None) -> re.Pattern[str]:
    cat = catalog or load_catalog()
    routing = cat.get("routing") or {}
    keywords = routing.get("balance_keywords") or [
        "balance de prueba",
        "saldo final",
        "movimientos contables",
    ]
    return _keywords_to_pattern(keywords)


def build_presupuesto_question_pattern(catalog: dict[str, Any] | None = None) -> re.Pattern[str]:
    cat = catalog or load_catalog()
    routing = cat.get("routing") or {}
    keywords = routing.get("presupuesto_keywords") or ["presupuesto", "proyeccion", "proyección"]
    return _keywords_to_pattern(keywords, word_boundary=True)


def build_ventas_detalle_question_pattern(catalog: dict[str, Any] | None = None) -> re.Pattern[str]:
    cat = catalog or load_catalog()
    routing = cat.get("routing") or {}
    keywords = routing.get("ventas_detalle_keywords") or [
        "por producto",
        "mix de productos",
        "detalle de ventas",
    ]
    return _keywords_to_pattern(keywords)


def build_presupuesto_vs_real_question_pattern(catalog: dict[str, Any] | None = None) -> re.Pattern[str]:
    cat = catalog or load_catalog()
    routing = cat.get("routing") or {}
    keywords = routing.get("presupuesto_vs_real_keywords") or [
        "presupuesto vs real",
        "cumplimiento presupuesto",
    ]
    return _keywords_to_pattern(keywords)


def build_ultimo_periodo_question_pattern(catalog: dict[str, Any] | None = None) -> re.Pattern[str]:
    cat = catalog or load_catalog()
    routing = cat.get("routing") or {}
    keywords = routing.get("ultimo_periodo_keywords") or [
        "ultimo periodo",
        "último periodo",
    ]
    return _keywords_to_pattern(keywords)


def build_semaforos_actuales_question_pattern(catalog: dict[str, Any] | None = None) -> re.Pattern[str]:
    cat = catalog or load_catalog()
    routing = cat.get("routing") or {}
    keywords = routing.get("semaforos_actuales_keywords") or [
        "semaforo actual",
        "semáforo actual",
    ]
    return _keywords_to_pattern(keywords)


def try_heuristic_route(question: str, catalog: dict[str, Any] | None = None) -> dict[str, str | list[str]] | None:
    """Keyword routing without LLM for common intents."""
    q = (question or "").strip()
    if not q:
        return None

    kpi_table = get_kpi_entity_key(catalog)

    if build_ultimo_periodo_question_pattern(catalog).search(q):
        return {
            "intent": "mixto",
            "tables": ["vw_ultimo_periodo_cliente"],
            "reason": "heuristic: último periodo / datos disponibles",
        }
    if build_semaforos_actuales_question_pattern(catalog).search(q):
        return {
            "intent": "kpis",
            "tables": ["vw_semaforos_cliente"],
            "reason": "heuristic: semáforos del periodo actual",
        }
    if build_presupuesto_vs_real_question_pattern(catalog).search(q):
        return {
            "intent": "presupuesto",
            "tables": ["vw_presupuesto_vs_real_mes"],
            "reason": "heuristic: presupuesto vs real / cumplimiento",
        }
    if build_ventas_netas_question_pattern(catalog).search(q):
        return {
            "intent": "ventas",
            "tables": ["vw_ventas_netas_mes"],
            "reason": "heuristic: ventas netas / notas crédito",
        }
    if build_dim_catalog_question_pattern(catalog).search(q):
        return {
            "intent": "cuentas",
            "tables": ["vw_dim_accounts"],
            "reason": "heuristic: catálogo plan de cuentas (vw_dim_accounts, sin montos)",
        }
    if build_tablero_question_pattern(catalog).search(q):
        return {
            "intent": "mixto",
            "tables": [get_primary_entity_key(catalog)],
            "reason": "heuristic: tablero / P&L / montos (vw_fact_bdp_enriched)",
        }
    if build_kpi_question_pattern(catalog).search(q):
        return {
            "intent": "kpis",
            "tables": [kpi_table],
            "reason": "heuristic: KPI / resultados agregados",
        }
    if build_balance_question_pattern(catalog).search(q):
        return {
            "intent": "balance",
            "tables": [get_primary_entity_key(catalog)],
            "reason": "heuristic: balance / movimientos / saldos (vw_fact_bdp_enriched)",
        }
    if build_presupuesto_question_pattern(catalog).search(q):
        return {
            "intent": "presupuesto",
            "tables": ["presupuesto_proyeccion"],
            "reason": "heuristic: presupuesto / proyección",
        }
    if build_ventas_detalle_question_pattern(catalog).search(q):
        return {
            "intent": "ventas",
            "tables": ["vw_ventas_por_producto_mes"],
            "reason": "heuristic: ventas por producto / mix",
        }
    return None


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
