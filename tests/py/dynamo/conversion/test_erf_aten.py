import torch
import torch.nn as nn
from parameterized import parameterized
from torch.testing._internal.common_utils import run_tests
from torch_tensorrt import Input

from .harness import DispatchTestCase


class TestErfConverter(DispatchTestCase):
    @parameterized.expand(
        [
            ("2d_dim_dtype_float", (2, 2), torch.float),
            ("3d_dim_dtype_float", (2, 2, 2), torch.float),
            ("2d_dim_dtype_half", (2, 2), torch.half),
            ("3d_dim_dtype_half", (2, 2, 2), torch.half),
        ]
    )
    def test_erf_float(self, _, x, type):
        class erf(nn.Module):
            def forward(self, input):
                return torch.ops.aten.erf.default(input)

        inputs = [torch.randn(x, dtype=type)]
        self.run_test(erf(), inputs, precision=type, output_dtypes=[type])

    @parameterized.expand(
        [
            ("2d_dim_dtype_int32", (2, 2), torch.int32, 0, 5),
            ("3d_dim_dtype_int32", (2, 2, 2), torch.int32, 0, 5),
        ]
    )
    def test_erf_int(self, _, x, type, min, max):
        class erf(nn.Module):
            def forward(self, input):
                return torch.ops.aten.erf.default(input)

        inputs = [torch.randint(min, max, x, dtype=type)]
        self.run_test(
            erf(),
            inputs,
        )


if __name__ == "__main__":
    run_tests()
