from fastapi import FastAPI
from sql_agent import run_financial_query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

class QueryRequest(BaseModel):
    agent_id: str
    message: str
    schema: str
    customer_id: str
    customer_type: str

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://app.valia.com.co",
        "http://localhost:3000"
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/")
def health():
    return {"status": "ok"}

@app.post("/query")
def query(payload: QueryRequest):

    return run_financial_query(
        question=payload.message,
        customer_id=payload.customer_id,
        agent_id=payload.agent_id,
        schema=payload.schema,
        customer_type=payload.customer_type
    )
