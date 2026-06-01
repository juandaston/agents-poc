import logging
import re

logger = logging.getLogger("agents-poc.security")

FORBIDDEN_KEYWORDS = (
    "delete",
    "update",
    "insert",
    "drop",
    "alter",
    "truncate",
    "create",
)

# Palabra completa, no substring (evita falsos positivos: deleted_at, created_at, etc.)
FORBIDDEN_PATTERN = re.compile(
    r"\b(" + "|".join(FORBIDDEN_KEYWORDS) + r")\b",
    re.IGNORECASE,
)


def validate_sql(sql: str):
    original = (sql or "").strip()
    normalized = original.lower()

    logger.info("validating sql len=%s\n%s", len(original), original)

    if "select" not in normalized:
        logger.warning("sql rejected: missing SELECT\n%s", original)
        raise Exception("Solo SELECT permitido")

    match = FORBIDDEN_PATTERN.search(original)
    if match:
        keyword = match.group(1).lower()
        logger.warning(
            "sql rejected: forbidden keyword=%r at pos=%s\n%s",
            keyword,
            match.start(),
            original,
        )
        raise Exception(f"SQL bloqueado por seguridad (keyword: {keyword})")

    logger.info("sql validated ok len=%s", len(original))
    return original
