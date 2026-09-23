from typing import List, Dict, Tuple, Optional, Any, Union
from dataclasses import dataclass
import os,sys
import fire


def str_msg(*items, **values) -> str:
    text = ",".join(str(item) for item in items)
    if values:
        extra = ",".join(f"{key}:{value}" for key, value in values.items())
        text = f"{text},{extra}" if text else extra
    return text.replace(str(os.getcwd()), ".")


def log_info(*items, **values):
    print(str_msg(*items, **values), file=sys.stderr, flush=True)


def log_warning(*items, **values):
    print(f"warning,{str_msg(*items, **values)}", file=sys.stderr, flush=True)


def log_trace(*items, **values):
    if str(os.environ.get("VMODAL_TRACE", "")).strip():
        print(f"trace,{str_msg(*items, **values)}", file=sys.stderr, flush=True)


def log_error(*items, **values):
    print(f"error,{str_msg(*items, **values)}", file=sys.stderr, flush=True)
