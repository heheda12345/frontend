from types import FrameType
from typing import Dict, Any, Callable, List
import logging
import itertools
import torch
import torch.fx
import operator
import dis
from .code import ProcessedCode, load_code
from .c_api import get_value_stack_from_top, get_value_stack_size
from .instruction import Instruction, ci
from .cache import CachedGraph, get_frame_cache, StoreInStack, StoreInLocal
from . import variables as vs
from .utils import is_scalar, new_random_key, has_force_graph_break, NullObject
from .object_table import ObjectTable
from .pycode_generator import GraphFnCodegen, GuardFnCodegen
from .fx_graph import FxGraph, fx_graph_functions, get_frame_root, is_leaf_module
from .bytecode_analysis import livevars_analysis


class State:
    objects: ObjectTable
    start_pc: int
    start_stack_size: int
    is_empty: bool
    fx_graph: FxGraph
    proxy_waiting_ids: list[torch.fx.Proxy]
    stored_locals: set[str]
    submodule_paths: dict[str, torch.nn.Module]

    def __init__(self, root: torch.nn.Module) -> None:
        self.objects = ObjectTable()
        self.start_pc = -1
        self.start_stack_size = -1
        self.is_empty = True
        self.fx_graph = FxGraph(root)
        self.proxy_waiting_ids = []
        self.stored_locals = set()
        self.submodule_paths = {mod: name for name, mod in root.named_modules()}

    def proxy_args_kwargs(
        self, args: list[Any], kwargs: dict[str, Any]
    ) -> tuple[tuple[torch.fx.Proxy, ...], dict[str, torch.fx.Proxy]]:
        proxy_args = tuple(
            self.objects.get(arg, allow_unexist_const=True).as_proxy()
            for arg in args)
        proxy_kwargs = {
            key: self.objects.get(arg, allow_unexist_const=True).as_proxy()
            for key, arg in kwargs.items()
        }
        return proxy_args, proxy_kwargs

    def record_function(self, func: Callable[..., Any], args: List[Any],
                        kwargs: Dict[str, Any]) -> None:
        if isinstance(func, torch.nn.Module):
            if func in self.submodule_paths and is_leaf_module(func):
                proxy = self.fx_graph.create_proxy(
                    "call_module",
                    self.submodule_paths[func],
                    *self.proxy_args_kwargs(args, kwargs),
                )
                self.proxy_waiting_ids.append(proxy)
            else:
                raise NotImplementedError
        else:
            proxy = self.fx_graph.create_proxy(
                "call_function",
                func,
                *self.proxy_args_kwargs(args, kwargs),
            )
            self.proxy_waiting_ids.append(proxy)

    @classmethod
    def from_frame(cls, frame: FrameType, read_stack: bool,
                   frame_root: torch.nn.Module) -> 'State':
        state = cls(frame_root)
        if read_stack:
            state.start_stack_size = get_value_stack_size(frame)
            for i in range(state.start_stack_size):
                value = get_value_stack_from_top(frame, i)
                var = vs.make_var_from_value(value, True, state.fx_graph,
                                             f"locals['__stack__{i}']")
                state.objects.add(var, value)
        return state


class GuardTracker:
    code: ProcessedCode
    frame_id: int
    frame: FrameType
    state: State
    have_error: bool
    frame_root: torch.nn.Module

    def __init__(self, frame: FrameType, frame_id: int):
        self.code = load_code(frame_id)
        self.frame = frame
        self.frame_id = frame_id
        self.frame_root = get_frame_root(frame_id)
        self.init_state(
            read_stack=False
        )  # stack pointer is not initialized at the creation of a stack frame

    def init_state(self, read_stack: bool = True) -> None:
        self.state = State.from_frame(self.frame, read_stack, self.frame_root)
        self.have_error = False

    def record(
            self, frame: FrameType, frame_id: int
    ) -> None:  # pass frame and frame_id only for assertion
        assert frame_id == self.frame_id
        assert frame == self.frame
        self.process_last_inst()

        pc, inst = self.code.get_orig_inst(self.frame.f_lasti)
        if inst is None:
            self.restart(
                f"running injected code (f_lasti={self.frame.f_lasti})")
            return
        if has_force_graph_break(frame_id, pc):
            assert inst.opcode != dis.opmap["LOAD_METHOD"]
            self.restart(f"force graph break (pc = {pc})")
            return
        # call init_state after is_inject_code check to avoid frequent init_state
        if self.have_error:
            try:
                self.init_state()
            except Exception as e:
                self.restart(f"Exception during init: {e}")
                return
        if self.state.start_pc == -1:
            self.state.start_pc = pc
            assert self.state.start_pc >= 0
        if hasattr(self, inst.opname):
            getattr(self, inst.opname)(inst)
            if not self.have_error:
                self.state.is_empty = False
        else:
            self.restart(f"unknown opcode {inst.opname}")

    def commit(self) -> None:
        if self.state.is_empty:
            return
        assert self.state.start_pc >= 0
        end_pc = self.code.get_orig_pc(self.frame.f_lasti)
        if end_pc == -1:
            end_pc = self.code.get_next_orig_pc(self.frame.f_lasti)
        print("commiting", self.state.start_pc, end_pc)
        key = new_random_key()
        guard_codegen = GuardFnCodegen(key=key)
        for var in self.state.objects.get_all():
            var.make_guard(guard_codegen)
        guard_code = guard_codegen.get_code()
        graph_codegen = GraphFnCodegen(key=key)
        for node in self.state.fx_graph.result_graph.nodes:
            if node.op == "placeholder":
                var = node.meta["var"]
                assert isinstance(var, vs.TensorVar)
                graph_codegen.add_graph_input(var.extract_code_at_start)
        current_inst = self.code.get_inst(self.frame.f_lasti)
        # livevars_analysis should return the same result when passing self.code.guard_insts
        # and self.code.original_insts, but as current_inst may not be in original_insts,
        # we pass guard_insts here
        live_vars = livevars_analysis(self.code.guard_insts, current_inst)
        live_vars = live_vars.intersection(self.state.stored_locals)
        for i, live_var in enumerate(live_vars):
            value = self.frame.f_locals[live_var]
            var = self.state.objects.get(value, allow_unexist_const=True)
            var.make_output(f"__live_{i}", StoreInLocal(live_var),
                            graph_codegen)
        # TODO: can be optimized by only reproduce the modified variables
        stack_size = get_value_stack_size(self.frame)
        for i in range(stack_size):
            value = get_value_stack_from_top(self.frame, i)
            var = self.state.objects.get(value, allow_unexist_const=True)
            var.make_output(f"__stack__{i}", StoreInStack(i), graph_codegen)
        graph_code = graph_codegen.get_code()
        compiled_graph = self.state.fx_graph.compile(
            outputs=graph_codegen.get_graph_outputs())

        py_code = f"""\
{graph_code}
{guard_code}
        """
        out: Dict[str, Any] = dict()
        print("RUNNING PY CODE")
        print(py_code)
        exec(py_code, self.frame.f_globals, out)
        guard_fn = out["___make_guard_fn"](*guard_codegen.vars.values())
        graph_fn = out["___make_graph_fn"](compiled_graph,
                                           *graph_codegen.objs.values())

        print("guard_fn:", guard_fn)
        print("pc:", self.state.start_pc, end_pc)
        print("stack:", self.state.start_stack_size, stack_size)

        get_frame_cache(self.frame_id).add(
            CachedGraph(
                guard_fn,
                graph_fn,
                self.state.start_pc,
                end_pc,
                start_stack_size=self.state.start_stack_size,
                end_stack_size=stack_size,
                return_values=graph_codegen.get_return_values(),
                key=key,
            ))
        self.state.is_empty = True

    def process_last_inst(self) -> None:
        for i, proxy in enumerate(self.state.proxy_waiting_ids):
            value = get_value_stack_from_top(self.frame, i)
            if isinstance(value, torch.Tensor):
                var = vs.TensorVar.from_tensor_and_proxy(value, proxy, False)
            else:
                raise NotImplementedError
            self.state.objects.add(var, value)
        self.state.proxy_waiting_ids.clear()

    def restart(self, restart_reason: str) -> None:
        logging.info(f"restart: {restart_reason}")
        self.have_error = True
        self.commit()

    @classmethod
    def has_tensor_arg(cls, args: List[Any], kwargs: Dict[str, Any]) -> bool:
        return any(
            isinstance(i, torch.Tensor)
            for i in itertools.chain(args, kwargs.values()))

    @classmethod
    def all_scalar_arg(cls, args: List[Any], kwargs: Dict[str, Any]) -> bool:
        return all(is_scalar(i) for i in itertools.chain(args, kwargs.values()))

    def call_function(
        self,
        func: Callable[..., Any],
        args: List[Any],
        kwargs: Dict[str, Any],
    ) -> None:
        print("call_function", func, args, kwargs)
        if self.has_tensor_arg(args, kwargs):
            if func in fx_graph_functions() or isinstance(
                    func, torch.nn.Module):
                self.state.record_function(func, args, kwargs)
            else:
                raise NotImplementedError
        elif self.all_scalar_arg(args, kwargs):
            pass
        else:
            raise NotImplementedError

    def BINARY_ADD(self, _inst: Instruction) -> None:
        obj1 = get_value_stack_from_top(self.frame, 1)
        obj2 = get_value_stack_from_top(self.frame, 0)
        self.call_function(operator.add, [obj1, obj2], {})

    def BINARY_SUBSCR(self, inst: Instruction) -> None:
        obj1 = get_value_stack_from_top(self.frame, 1)
        obj2 = get_value_stack_from_top(self.frame, 0)
        self.call_function(operator.getitem, [obj1, obj2], {})

    def BUILD_SLICE(self, _inst: Instruction) -> None:
        pass

    def LOAD_CONST(self, _inst: Instruction) -> None:
        pass

    def LOAD_FAST(self, inst: Instruction) -> None:
        if inst.argval not in self.state.stored_locals:
            obj = self.frame.f_locals[inst.argval]
            var = vs.make_var_from_value(obj, True, self.state.fx_graph,
                                         f'locals["{inst.argval}"]')
            self.state.objects.add(var, obj)

    # heheda: we need to make sure that no unbound LOAD_METHOD is called by python runtime to avoid NULL in stack
    def LOAD_METHOD(self, inst: Instruction) -> None:
        self_obj = get_value_stack_from_top(self.frame, 0)
        method = getattr(self_obj, inst.argval)
        self_var = self.state.objects.get(self_obj)
        if self_var.need_guard_check:
            try:
                method_var = vs.make_var_from_value(
                    method, True, self.state.fx_graph,
                    f"({self_var.extract_code_at_start}).{inst.argval}")
            except Exception as e:
                self.restart(f"Exception during LOAD_METHOD: {e}")
                return
            self.state.objects.add(method_var, method)

    def CALL_METHOD(self, inst: Instruction) -> None:
        num_args = inst.argval
        args = [
            get_value_stack_from_top(self.frame, i)
            for i in range(num_args - 1, -1, -1)
        ]
        kwargs: dict[str, Any] = {}
        self_val = get_value_stack_from_top(self.frame, num_args)
        meth_val = get_value_stack_from_top(self.frame, num_args + 1)
        if isinstance(meth_val, NullObject):
            # Stack layout: ... | NULL | callable | arg1 | ... | argN
            self.call_function(self_val, args, kwargs)
        else:
            # Stack layout: ... | method | self | arg1 | ... | argN
            self.call_function(meth_val, [self_val] + args, kwargs)

    def RETURN_VALUE(self, _inst: Instruction) -> None:
        self.restart("return value")

    def STORE_FAST(self, inst: Instruction) -> None:
        self.state.stored_locals.add(inst.argval)


trackers: list[GuardTracker] = []


def push_tracker(frame: FrameType, frame_id: int) -> None:
    print("init tracker", frame_id, "frame", hex(id(frame)), "frame_id",
          frame_id)
    trackers.append(GuardTracker(frame, frame_id))


def pop_tracker(frame_id: int) -> None:
    to_pop = trackers.pop()
    assert to_pop.state.is_empty
    assert to_pop.frame_id == frame_id


def record(frame: FrameType, frame_id: int) -> None:
    trackers[-1].record(frame, frame_id)


def reset() -> None:
    trackers.clear()
