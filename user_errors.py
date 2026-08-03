"""Mensajes amigables para el usuario final (errores técnicos solo en logs)."""

FRIENDLY_QUERY_ERROR = (
    "No pudimos procesar tu consulta en este momento. "
    "Intenta reformular la pregunta o vuelve a intentarlo en unos minutos."
)

FRIENDLY_TIMEOUT_ERROR = (
    "La consulta está tardando más de lo esperado. "
    "Por favor, inténtalo de nuevo en unos minutos."
)


def friendly_error_payload() -> dict:
    """Respuesta JSON con forma de éxito, sin detalles técnicos."""
    return {
        "route": None,
        "sources_consulted": [],
        "sql": [],
        "data": [],
        "answer": None,
        "customer_answer": FRIENDLY_QUERY_ERROR,
        "query_status": "error",
    }
