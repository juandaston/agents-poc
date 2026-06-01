import logging

logger = logging.getLogger("agents-poc.utils")


def has_real_data(data):
    if data is None:
        logger.debug("has_real_data: data is None")
        return False

    if isinstance(data, list):
        if len(data) == 0:
            logger.debug("has_real_data: empty list")
            return False

        for row in data:
            if not isinstance(row, dict):
                if row is not None:
                    return True
                continue

            for v in row.values():
                if v is None:
                    continue
                if v == "":
                    continue
                if isinstance(v, list) and len(v) == 0:
                    continue
                return True

        logger.debug("has_real_data: rows present but all empty/null")
        return False

    return True
