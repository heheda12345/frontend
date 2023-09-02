import inspect
import dis
from .bytecode_writter import get_code_keys
from typing import Any


def print_bytecode() -> None:
    this_frame = inspect.currentframe()  # the print_bytecode function
    assert this_frame is not None
    test_func_frame = this_frame.f_back
    assert test_func_frame is not None
    code = test_func_frame.f_code
    insts = dis.Bytecode(code)
    for inst in insts:
        print(inst)
    keys = get_code_keys()
    code_options = {k: getattr(code, k) for k in keys}
    for k, v in code_options.items():
        print(k, v)


class PyCodeWriter:

    def __init__(self) -> None:
        self.code_str = ''
        self.intend = 0

    def block_start(self) -> None:
        self.intend += 1

    def block_end(self) -> None:
        self.intend -= 1

    def write(self, code_str: str) -> None:
        code = code_str.splitlines()
        for line in code:
            self.code_str += '    ' * self.intend + line + '\n'

    def wl(self, code_str: str) -> None:
        self.write(code_str + '\n')

    def get_code(self) -> str:
        return self.code_str


def is_scalar(value: Any) -> bool:
    return type(value) in {int, float, bool, str}
