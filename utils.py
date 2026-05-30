def has_real_data(data):

    # None total
    if data is None:
        return False

    # lista vacía
    if isinstance(data, list):

        if len(data) == 0:
            return False

        # revisar todas las filas
        for row in data:

            # si no es dict, validar directo
            if not isinstance(row, dict):
                if row is not None:
                    return True
                continue

            # revisar valores de la fila
            values = list(row.values())

            # si al menos un valor existe → hay data
            for v in values:

                # NULL
                if v is None:
                    continue

                # string vacío
                if v == "":
                    continue

                # listas vacías
                if isinstance(v, list) and len(v) == 0:
                    continue

                return True

        return False

    return True
