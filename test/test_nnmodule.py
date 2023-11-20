import pytest
from frontend.compile import compile, reset
from frontend.utils import add_force_graph_break
from frontend.c_api import get_next_frame_id
import logging
import torch
from common.checker import run_and_check, HIT, MISS


class Model(torch.nn.Module):

    def __init__(self):
        super(Model, self).__init__()
        self.linear = torch.nn.Linear(10, 5)

    def forward(self, x):
        y = self.linear(x)
        z = y + 1.0
        return z


class ModelParam(torch.nn.Module):

    def __init__(self):
        super(ModelParam, self).__init__()
        self.param = torch.nn.Parameter(torch.randn(5, 5))

    def forward(self, x):
        y = self.param + x
        return y


class Model2(torch.nn.Module):

    def __init__(self):
        super().__init__()
        self.linear = torch.nn.Linear(1, 1)

    def forward(self, x):
        return self.linear(x) - 4.0


def call_model(model, a):
    b = model.linear(a) + 1
    return b


def nn_module(a):
    b = torch.nn.Softmax(dim=-1)(a)
    return b


def test_call_method(caplog):
    reset()
    with torch.no_grad():
        model = Model().eval()
        x = torch.randn(1, 10)
        expect_result = model(x)
        add_force_graph_break(get_next_frame_id(), 3)
        compiled_model = compile(model)
        run_and_check(compiled_model, [MISS], 2, caplog, expect_result, x)
        run_and_check(compiled_model, [HIT, HIT], 2, caplog, expect_result, x)


def test_module(caplog):
    reset()
    with torch.no_grad():
        model = Model().eval()
        x = torch.randn(1, 10)
        expect_result = model(x)
        compiled_model = compile(model)
        run_and_check(compiled_model, [MISS], 1, caplog, expect_result, x)
        run_and_check(compiled_model, [HIT], 1, caplog, expect_result, x)


def test_module_param(caplog):
    reset()
    with torch.no_grad():
        model = ModelParam().eval()
        x = torch.randn(1, 5)
        expect_result = model(x)
        compiled_model = compile(model)
        run_and_check(compiled_model, [MISS], 1, caplog, expect_result, x)
        run_and_check(compiled_model, [HIT], 1, caplog, expect_result, x)


def test_external_module(caplog):
    reset()
    with torch.no_grad():
        model = Model2().eval()
        x = torch.randn(1, 1)
        expect_result = call_model(model, x)
        compiled_model = compile(call_model)
        run_and_check(compiled_model, [MISS], 1, caplog, expect_result, model,
                      x)
        run_and_check(compiled_model, [HIT], 1, caplog, expect_result, model,
                      x)


def test_nn_module(caplog):
    reset()
    compiled = compile(nn_module)
    x = torch.randn(1, 10)
    expect_result = nn_module(x)
    run_and_check(compiled, [MISS], 1, caplog, expect_result, x)
    run_and_check(compiled, [HIT], 1, caplog, expect_result, x)


if __name__ == "__main__":
    caplog = logging.getLogger(__name__)
    test_call_method(caplog)
    test_module(caplog)
