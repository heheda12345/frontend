import inspect
import dis
from typing import Any, TYPE_CHECKING, Callable, TypeVar, Generic, Optional, no_type_check, Iterator, Union, Dict, List, Generator
from types import FrameType
import random
import operator
import os
import contextlib
import itertools
import math
import torch
import torch._C
import collections
from .config import get_config, set_config
from .c_api import parse_type_obj

if TYPE_CHECKING:
    from .instruction import Instruction


class NullObject:
    '''
    The stack should be the following when meth is unbound
    NULL | meth | arg1 | ... | argN
    But as we cannot push NULL into the stack, we push a NullObject instead.
    NullObject | meth | arg1 | ... | argN
    We simulate the behavior of unbound method by calling arg0(arg1, ..., argN)
    '''

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        print("calling unbound method")
        return args[0](*args[1:], **kwargs)


null_object = NullObject()


def print_bytecode() -> None:
    this_frame = inspect.currentframe()  # the print_bytecode function
    assert this_frame is not None
    test_func_frame = this_frame.f_back
    assert test_func_frame is not None
    code = test_func_frame.f_code
    insts = dis.Bytecode(code)
    for inst in insts:
        print(inst)
    from .bytecode_writter import get_code_keys
    keys = get_code_keys()
    code_options = {k: getattr(code, k) for k in keys}
    for k, v in code_options.items():
        print(k, v)


ScalarType = Union[int, float, bool, str]


def is_scalar(value: Any) -> bool:
    return type(value) in {int, float, bool, str}


def is_call_bytecode(inst: 'Instruction') -> bool:
    return inst.opname.startswith("CALL_")


fx_graph_inplace_functions: set[Callable[..., Any]] = {
    operator.ipow,
    operator.imul,
    operator.imatmul,
    operator.ifloordiv,
    operator.itruediv,
    operator.imod,
    operator.iadd,
    operator.isub,
    operator.ilshift,
    operator.irshift,
    operator.iand,
    operator.ixor,
    operator.ior,
    operator.setitem,
}

fx_graph_functions: set[Callable[..., Any]] = {
    operator.pos,
    operator.neg,
    operator.not_,
    operator.invert,
    operator.pow,
    operator.mul,
    operator.matmul,
    operator.floordiv,
    operator.truediv,
    operator.mod,
    operator.add,
    operator.sub,
    operator.getitem,
    operator.lshift,
    operator.rshift,
    operator.and_,
    operator.or_,
    operator.is_,
    operator.xor,
    operator.eq,
    operator.lt,
    operator.ne,
    operator.le,
    operator.gt,
    operator.ge,
    operator.contains,
}
fx_graph_functions = fx_graph_functions.union(fx_graph_inplace_functions)

math2torch = {
    math.log2: torch.log2,
    math.ceil: torch.ceil,
}

torch_inplace_funcs = {
    "abs_", "acos_", "acosh_", "add_", "addcmul_", "addcdiv_", "asin_",
    "asinh_", "atan_", "atanh_", "atan2_", "bitwise_and_",
    "bitwise_left_shift_", "bitwise_not_", "bitwise_or_",
    "bitwise_right_shift_", "bitwise_xor_", "ceil_", "clamp_", "clamp_min_",
    "clamp_max_", "conj_physical_", "copy_", "copysign_", "cos_", "cosh_",
    "cumsum_", "digamma_", "div_", "eq_", "erf_", "erfc_", "erfinv_", "exp_",
    "exp2_", "expm1_", "float_power_", "floor_", "floor_divide_", "fmod_",
    "frac_", "gcd_", "ge_", "gt_", "heaviside_", "hypot_", "igamma_",
    "igammac_", "i0_", "lcm_", "le_", "lerp_", "lgamma_", "log10_", "log1p_",
    "log2_", "log_", "logical_and_", "logical_not_", "logical_or_",
    "logical_xor_", "lt_", "mul_", "mvlgamma_", "nan_to_num_", "ne_", "neg_",
    "nextafter_", "pow_", "reciprocal_", "remainder_", "rsqrt_", "sgn_",
    "sigmoid_", "sign_", "sin_", "sinc_", "sinh_", "sqrt_", "square_", "sub_",
    "tan_", "tanh_", "tril_", "triu_", "true_divide_", "trunc_", "xlogy_",
    "cauchy_", "exponential_", "geometric_", "log_normal_", "zero_"
}


def get_root_module(func: Callable[..., Any]) -> str:
    import numpy as np
    if hasattr(func, '__objclass__'):
        if func.__objclass__ == torch._C._TensorBase:
            return 'torch'
        elif func.__objclass__ in (list, tuple, set, dict, str):
            return 'builtins'
        elif func.__objclass__ == np.ndarray:
            return 'numpy'

    if hasattr(func, '__self__') and isinstance(func.__self__, torch.Tensor):
        return 'torch'

    if hasattr(func, '__class__') and func.__class__ == np.ufunc:
        return 'numpy'

    if hasattr(func, '__self__') and isinstance(func.__self__,
                                                np.random.RandomState):
        return 'numpy'

    module = inspect.getmodule(func)
    module_str = ""
    if module is not None:
        module_str = str(module).split('\'')[1]
    if module is None or 'torch.distributions' in module_str:
        return ""
    root_module = module_str.split('.')[0]
    #NOTE: special cases in torchvision module, need to check whether this module is safe to record in graph
    if hasattr(func, '__name__') and func.__name__ in (
            'pad', 'resize') and root_module == 'torchvision':
        return 'torch'
    return root_module


def is_own_method(func: str, parent: Callable[..., Any]) -> bool:
    for member in parent.__class__.__dict__.keys():
        if member == func:
            return True
    return False


def get_method_defined_class(cls: type[Any],
                             func_name: str) -> Optional[type[Any]]:
    while True:
        if func_name in cls.__dict__:
            return cls
        if cls.__base__ is None:
            break
        cls = cls.__base__
    return None


def is_user_defined_func(func: Callable[..., Any]) -> bool:
    # print([(x, getattr(func, x)) for x in dir(func)])
    import numpy
    if hasattr(func, '__objclass__') and func.__objclass__ in (
            torch._C._TensorBase, dict, list, str, collections.OrderedDict,
            numpy.ndarray):
        return False
    if hasattr(func, '__class__') and func.__class__ in (
            torch._C._TensorBase, dict, list, str, collections.OrderedDict,
            numpy.ndarray):
        return False

    # NOTE: random should be called as a UDF, not handled
    if hasattr(func, '__self__'):
        if isinstance(func.__self__, (torch.Tensor, random.Random)):
            return False
        elif isinstance(func.__self__, numpy.random.RandomState):
            return False
        elif isinstance(func.__self__, (list, tuple, set, dict, str)):
            return False
        elif isinstance(func.__self__, torch.nn.Sequential):
            return True

    if hasattr(func, '__name__') and func.__name__ in ('<genexpr>', 'numel'):
        return False
    if hasattr(func, '__name__') and func.__name__ == '_conv_forward':
        return True

    if hasattr(func, '__name__') and func.__name__ == 'forward':
        return True
    if hasattr(func, '__name__') and func.__name__ == 'apply':
        assert hasattr(func, '__self__')
        return is_user_defined_func(func.__self__)

    if inspect.isclass(func):
        tp_name = parse_type_obj(func)
        module = tp_name.split(".")[0]
        if module in ("itertools",):
            return False

    if func is super:
        return False

    root_module = get_root_module(func)
    # print("root module", func, "===is==", root_module, type(root_module))
    if root_module == 'torch' and hasattr(
            func, '__name__') and func.__name__ == '_call_impl':
        return True
    if root_module in ('math', 'builtins', 'torch', 'numpy', '_operator',
                       'inspect', 'collections', 'itertools', 'functools',
                       'copy'):
        #NOTE:self.function should be recursive-checked to find out where it's defined, but not implemented
        if hasattr(func, '__self__'
                  ) and func.__self__ is not None and is_user_defined_func(
                      func.__self__):
            if is_own_method(func.__name__, func.__self__):
                return True
            else:
                return False
        return False
    return True


def is_graph_func(func: Callable[..., Any]) -> bool:
    if func in fx_graph_functions:
        return True
    if hasattr(func,
               '__objclass__') and func.__objclass__ == torch._C._TensorBase:
        return True
    if isinstance(func, torch.nn.Module):
        return True

    root_module = get_root_module(func)
    if root_module == '':
        return False
    return root_module == 'torch'


def is_math_func(func: Callable[..., Any]) -> bool:
    root_module = get_root_module(func)
    return root_module == 'math'


random_state = None


def new_random_key() -> int:
    global random_state
    cur_state = random.getstate()
    if random_state is None:
        random.seed(66666)
        random_state = random.getstate()
    random.setstate(random_state)
    new_key = random.randint(0, 10000)
    random_state = random.getstate()
    random.setstate(cur_state)
    return new_key


class ForceGraphBreaker:
    breaks: dict[int, set[int]]  # frame_id -> list of pc

    def __init__(self) -> None:
        self.breaks = {}

    def add(self, frame_id: int, pc: int) -> None:
        if frame_id not in self.breaks:
            self.breaks[frame_id] = set()
        self.breaks[frame_id].add(pc)

    def need_break(self, frame_id: int, pc: int) -> bool:
        if frame_id not in self.breaks:
            return False
        return pc in self.breaks[frame_id]


graph_breaker = None


def add_force_graph_break(frame_id: int, pc: int) -> None:
    global graph_breaker
    if graph_breaker is None:
        graph_breaker = ForceGraphBreaker()
    graph_breaker.add(frame_id, pc)


def has_force_graph_break(frame_id: int, pc: int) -> bool:
    global graph_breaker
    # fast path
    if graph_breaker is None:
        return False
    return graph_breaker.need_break(frame_id, pc)


def clear_force_graph_break() -> None:
    global graph_breaker
    graph_breaker = None


class UnknownTypeError(Exception):

    def __init__(self, ty: type[Any]) -> None:
        super().__init__(f"Unknown type {ty}")


def get_all_objects_in_stack(frame: FrameType) -> list[Any]:
    from .c_api import get_value_stack_from_top, get_value_stack_size
    stack_size = get_value_stack_size(frame)
    return [get_value_stack_from_top(frame, i) for i in range(stack_size)]


def reset() -> None:
    global graph_breaker
    graph_breaker = None
    global random_state
    random_state = None


T = TypeVar('T')


class ReadOnlyObject(Generic[T]):
    obj: T
    const_attrs: tuple[str, ...]

    def __init__(self, obj: T, const_attrs: tuple[str, ...] = ()) -> None:
        self.obj = obj
        self.const_attrs = const_attrs

    def __getattr__(self, attr: str) -> Any:
        if attr in self.const_attrs:
            return getattr(self.obj, attr)
        else:
            raise AttributeError(
                f"Attribute {attr} should not be called in reader of {self.obj}"
            )


class SetConfig:
    config_old: dict[str, Any]
    config_new: dict[str, Any]

    def __init__(self, config: dict[str, Any]) -> None:
        self.config_new = config
        self.config_old = {}

    def __enter__(self) -> None:
        for k, v in self.config_new.items():
            self.config_old[k] = get_config(k)
            set_config(k, v)

    def __exit__(self, *args: Any) -> None:
        for k, v in self.config_old.items():
            set_config(k, v)


@no_type_check
def is_namedtuple(obj: Any) -> bool:
    cls: type[Any] = obj if inspect.isclass(cls) else type(obj)
    return (issubclass(cls, tuple) and
            isinstance(getattr(cls, '_fields', None), tuple) and
            all(isinstance(field, str) for field in cls._fields))


@no_type_check
def is_structseq(obj: Any) -> bool:
    cls: type[Any] = obj if inspect.isclass(obj) else type(obj)
    if (cls.__base__ is tuple and
            isinstance(getattr(cls, 'n_sequence_fields', None), int) and
            isinstance(getattr(cls, 'n_fields', None), int) and
            isinstance(getattr(cls, 'n_unnamed_fields', None), int)):
        try:

            class subcls(cls):  # type: ignore[misc]
                pass

        except (
                TypeError,  # CPython
                AssertionError,  # PyPy
        ):
            return True

    return False


@contextlib.contextmanager
def enable_dyn_shape() -> Iterator[None]:
    with torch._dynamo.eval_frame.enable_dynamic():
        with SetConfig({'dynshape': True}):
            yield


high_order_func_list = (map, filter, zip, list, iter, enumerate, tuple)


def is_high_order_func(func: Callable[..., Any]) -> bool:
    return func in high_order_func_list or isinstance(func, Generator)


def is_high_order_func_with_udf(func: Callable[..., Any], args: List[Any],
                                kwargs: Dict[str, Any]) -> bool:
    if not is_high_order_func(func):  # fast path
        return False

    def is_user_defined_iter(x: Any) -> bool:
        return isinstance(x, torch.Tensor) or (hasattr(x, '__iter__') and
                                               is_user_defined_func(x.__iter__))

    def call_user_defined_iterator(x: Any) -> bool:
        if isinstance(x, map):
            from .c_api import parse_mapobject
            it, map_fn = parse_mapobject(x)
            return is_user_defined_func(map_fn)
        if isinstance(x, Generator):
            return True
        return False

    if func == zip:
        return any(
            is_user_defined_iter(x)
            for x in itertools.chain(args, kwargs.values()))
    elif func in (map, filter):
        return len(args) >= 2 and is_user_defined_iter(
            args[1]
        )  # not check args[0] is udf because the function is not called during map() call
    elif func == list:
        return len(args) >= 1 and call_user_defined_iterator(args[0])
    elif func == tuple:
        return len(args) >= 1 and call_user_defined_iterator(
            args[0]) and not isinstance(
                args[0],
                Generator)  # generator contains yield, which is not support yet
    elif func == iter:
        return len(args) >= 1 and is_user_defined_iter(args[0])
    elif func == enumerate:
        return len(args) >= 1 and is_user_defined_iter(args[0])
    elif isinstance(func, Generator):
        return True
    else:
        raise NotImplementedError
