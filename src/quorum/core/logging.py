import json
import logging
import time


class JsonFormatter(logging.Formatter):
    def format(self, record):
        out = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(record.created)),
            "level": record.levelname.lower(),
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for k in ("request_id", "event", "actor", "path", "status", "ms"):
            if getattr(record, k, ""):
                out[k] = getattr(record, k)
        if record.exc_info:
            out["exc"] = self.formatException(record.exc_info)
        return json.dumps(out)
