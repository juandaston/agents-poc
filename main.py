import logging
import time
import uuid

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from logging_setup import setup_logging
from semantic_loader import get_semantic_dir
from sql_agent import run_financial_query
from user_errors import friendly_error_payload

setup_logging()
logger = logging.getLogger("agents-poc.api")


class QueryRequest(BaseModel):
    agent_id: str
    message: str
    schema: str
    customer_id: str
    customer_type: str


app = FastAPI()


@app.on_event("startup")
def _log_semantic_layer():
    try:
        logger.info("semantic layer loaded from %s", get_semantic_dir())
    except Exception as exc:
        logger.error("semantic layer failed to load: %s", exc)


app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://app.valia.com.co",
        "http://localhost:3000",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def log_requests(request: Request, call_next):
    request_id = request.headers.get("x-request-id") or str(uuid.uuid4())[:8]
    started = time.perf_counter()
    logger.info(
        "request start id=%s method=%s path=%s",
        request_id,
        request.method,
        request.url.path,
    )
    try:
        response = await call_next(request)
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        logger.info(
            "request end id=%s status=%s elapsed_ms=%s",
            request_id,
            response.status_code,
            elapsed_ms,
        )
        response.headers["X-Request-Id"] = request_id
        return response
    except Exception:
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        logger.exception(
            "request failed id=%s path=%s elapsed_ms=%s",
            request_id,
            request.url.path,
            elapsed_ms,
        )
        raise


@app.get("/")
def health():
    logger.debug("health check")
    return {"status": "ok"}


@app.post("/query")
def query(payload: QueryRequest):
    logger.info(
        "query received agent_id=%s customer_id=%s customer_type=%s schema=%s message_len=%s",
        payload.agent_id,
        payload.customer_id,
        payload.customer_type,
        payload.schema,
        len(payload.message or ""),
    )
    logger.debug("query message=%r", payload.message)

    started = time.perf_counter()
    try:
        result = run_financial_query(
            question=payload.message,
            customer_id=payload.customer_id,
            agent_id=payload.agent_id,
            schema=payload.schema,
            customer_type=payload.customer_type,
        )
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        route = result.get("route") if isinstance(result, dict) else None
        sql_count = len(result.get("sql") or []) if isinstance(result, dict) else 0
        sources = result.get("sources_consulted") if isinstance(result, dict) else None
        logger.info(
            "query success elapsed_ms=%s intent=%s sources_consulted=%s sql_count=%s has_answer=%s",
            elapsed_ms,
            route.get("intent") if isinstance(route, dict) else None,
            sources,
            sql_count,
            bool(result.get("answer")) if isinstance(result, dict) else False,
        )
        return result
    except Exception as exc:
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        logger.exception(
            "query failed elapsed_ms=%s agent_id=%s customer_id=%s error=%s",
            elapsed_ms,
            payload.agent_id,
            payload.customer_id,
            exc,
        )
        return friendly_error_payload()
