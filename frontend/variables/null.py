from typing import TYPE_CHECKING, Union

import torch.fx
from .base import Variable
from ..fx_graph import ProxyArgs
from ..cache import StorePos
from ..utils import NullObject, null_object
if TYPE_CHECKING:
    from ..pycode_generator import GraphFnCodegen, GuardFnCodegen


class NullVar(Variable):

    def __init__(self,
                 need_guard_check: bool,
                 extract_code_at_start: str = "") -> None:
        super().__init__(need_guard_check, extract_code_at_start)

    def make_guard_inner(self, codegen: "GuardFnCodegen") -> None:
        pass

    def make_output(self, name_in_graph_fn: str, store_pos: StorePos,
                    codegen: "GraphFnCodegen") -> None:
        name_in_codegen = codegen.add_var(null_object, "NULL_VAR")
        codegen.output(name_in_graph_fn, store_pos, f"{name_in_codegen} # NULL")

    @classmethod
    def from_value(cls,
                   value: NullObject,
                   need_guard_check: bool,
                   _fx_graph: "torch.fx.Graph",
                   extract_code_at_start: str = "") -> "NullVar":
        return cls(need_guard_check, extract_code_at_start)

    def as_proxy(self) -> ProxyArgs:
        raise NotImplementedError()