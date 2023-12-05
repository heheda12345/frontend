from typing import Callable, Any, Union

CONFIG = {
    "backend": "inductor",  # Union[str, Callable[..., Any]]
    "debug": True,
    "dynshape": False,
}


def set_config(key: str, value: Any) -> None:
    CONFIG[key] = value


def get_config(key: str) -> Any:
    return CONFIG[key]