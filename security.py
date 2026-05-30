def validate_sql(sql: str):

    sql = sql.strip().lower()

    # 🔒 solo SELECT permitido
    if "select" not in sql:
        raise Exception("Solo SELECT permitido")

    forbidden = [
        "delete",
        "update",
        "insert",
        "drop",
        "alter",
        "truncate",
        "create"
    ]

    if any(word in sql for word in forbidden):
        raise Exception("SQL bloqueado por seguridad")

    return sql
