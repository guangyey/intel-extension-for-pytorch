import os
import subprocess
import unittest
import itertools
import torch
import torch.nn as nn
import torch.nn.functional as F
from test_ao_jit_llga_utils import (
    JitLlgaTestCase,
    LLGA_FUSION_GROUP,
    llga_fp32_bf16_test_env,
    get_eltwise_fn,
)
from torch.testing._internal.common_utils import TEST_SCIPY


import intel_extension_for_pytorch as ipex

try:
    import torchvision

    HAS_TORCHVISION = True
except ImportError:
    HAS_TORCHVISION = False
except RuntimeError:
    HAS_TORCHVISION = False
skipIfNoTorchVision = unittest.skipIf(not HAS_TORCHVISION, "no torchvision")


class TestOp(JitLlgaTestCase):
    @llga_fp32_bf16_test_env
    def test_conv2d(self):
        for [
            spatial,
            in_channels,
            out_channels,
            kernel,
            padding,
            stride,
            dilation,
            g,
            bias,
        ] in itertools.product(
            [7, 8],
            [8, 15],
            [7, 16],
            [3, 4],
            [0, 2],
            [1, 2],
            [1, 2],
            [1, 2],
            [True, False],
        ):
            m = nn.Conv2d(
                in_channels=in_channels * g,
                out_channels=out_channels * g,
                kernel_size=kernel,
                padding=padding,
                stride=stride,
                dilation=dilation,
                groups=g,
                bias=bias,
            )

            x = torch.rand(1, in_channels * g, spatial, spatial)
            graph, _ = self.checkTrace(m, [x])
            self.assertGraphContainsExactly(graph, LLGA_FUSION_GROUP, 1)

    @llga_fp32_bf16_test_env
    def test_conv2d_script(self):
        for bias in [True, False]:
            m = nn.Conv2d(
                in_channels=3,
                out_channels=3,
                kernel_size=3,
                padding=1,
                stride=1,
                dilation=1,
                groups=1,
                bias=bias,
            )

        x = torch.rand(1, 3, 5, 5)
        graph, _ = self.checkScript(m, [x])
        self.assertGraphContainsExactly(graph, LLGA_FUSION_GROUP, 1)

    @llga_fp32_bf16_test_env
    def test_bn2d(self):
        m = nn.BatchNorm2d(32).eval()
        x = torch.rand(1, 32, 28, 28)
        graph, _ = self.checkTrace(m, [x])
        self.assertGraphContainsExactly(graph, LLGA_FUSION_GROUP, 1)

    @llga_fp32_bf16_test_env
    def test_eltwise(self):
        class M(nn.Module):
            def __init__(self, eltwise_fn):
                super(M, self).__init__()
                self.eltwise = eltwise_fn

            def forward(self, x):
                return self.eltwise(x)

        for eltwise in ["relu", "gelu", "tanh", "sqrt", "square"]:
            eltwise_fn = get_eltwise_fn(eltwise)
            m = M(eltwise_fn)
            x = torch.rand(1, 32, 28, 28)
            graph, _ = self.checkTrace(m, [x])
            self.assertGraphContainsExactly(graph, LLGA_FUSION_GROUP, 1)

    @llga_fp32_bf16_test_env
    def test_max_pool2d(self):
        for [
            spatial,
            kernel,
            padding,
            stride,
            dilation,
            ceil_mode,
        ] in itertools.product(
            [15, 16, 17, 18, 19],
            [4, 5],
            [0, 1, 2],
            [1, 2],  # [1, 2, 4], TODO: fix issue in pad calculation
            [1],  # [1, 2], TODO: backend support for dilation
            [True, False],
        ):
            m = nn.MaxPool2d(
                kernel_size=kernel,
                stride=stride,
                padding=padding,
                dilation=dilation,
                ceil_mode=ceil_mode,
            )

            x = torch.rand(1, 4, spatial, spatial)
            graph, _ = self.checkTrace(m, [x])
            self.assertGraphContainsExactly(graph, LLGA_FUSION_GROUP, 1)

    @llga_fp32_bf16_test_env
    def test_avg_pool2d(self):
        for [
            spatial,
            kernel,
            padding,
            stride,
            ceil_mode,
            count_include_pad,
        ] in itertools.product(
            [15, 16, 17, 18, 19],
            [4, 5],
            [0, 1, 2],
            [1, 2, 4],
            [False],  # TODO: DNNL does not fully support ceil_mode=True
            [True, False],
        ):
            m = nn.AvgPool2d(
                kernel_size=kernel,
                stride=stride,
                padding=padding,
                ceil_mode=ceil_mode,
                count_include_pad=count_include_pad,
            )

            x = torch.rand(1, 4, spatial, spatial)
            graph, _ = self.checkTrace(m, [x])
            self.assertGraphContainsExactly(graph, LLGA_FUSION_GROUP, 1)

    @llga_fp32_bf16_test_env
    @unittest.skipIf(True, "Enable once size peephole is supported")
    def test_variable_kernel_avg_pool2d(self):
        class M(nn.Module):
            def __init__(self):
                super(M, self).__init__()

            def forward(self, x):
                x = F.avg_pool2d(
                    x,
                    kernel_size=(x.size(2), x.size(3)),
                    padding=0,
                    count_include_pad=False,
                )
                return x

        x = torch.randn(1, 1000, 1, 1)
        m = M()
        graph, _ = self.checkTrace(m, [x])
        # kernel_size is not Constant, shouldn't have any LLGA_FUSION_GROUP
        # TODO: with shape specialization, should have 1 LLGA_FUSION_GROUP
        self.assertGraphContainsExactly(graph, LLGA_FUSION_GROUP, 0)

    @llga_fp32_bf16_test_env
    def test_softmax(self):
        for dim in [-4, -3, -2, -1, 0, 1, 2, 3]:
            m = nn.Softmax(dim=dim)
            x = torch.rand(8, 12, 12, 12)
            graph, _ = self.checkTrace(m, [x])
            self.assertGraphContainsExactly(graph, LLGA_FUSION_GROUP, 1)

    @llga_fp32_bf16_test_env
    def test_softmax_different_output_dtype(self):
        class M(nn.Module):
            def __init__(self):
                super(M, self).__init__()

            def forward(self, x):
                return torch.nn.functional.softmax(x, dim=3, dtype=torch.bfloat16)

        m = M()
        x = torch.rand(8, 12, 12, 12)
        graph, _ = self.checkTrace(m, [x])
        self.assertGraphContainsExactly(graph, LLGA_FUSION_GROUP, 1)

    def _gen_binary_inputs(self, gen_permute=True):
        for xshape, yshape in [
            [[1, 32, 28, 28], [1, 32, 28, 28]],
            [[1, 32, 28, 28], [1, 1, 28, 28]],
            [[1, 32, 28, 28], [28]],
            [[1, 32, 28, 28], [1]],
        ]:
            yield torch.rand(xshape), torch.rand(yshape)
            if gen_permute and xshape != yshape:
                yield torch.rand(yshape), torch.rand(xshape)

    @llga_fp32_bf16_test_env
    def test_add_with_alpha(self):
        def forward_add(x, y):
            return torch.add(x, y, alpha=2)

        for x, y in self._gen_binary_inputs():
            graph, _ = self.checkTrace(forward_add, [x, y])
            self.assertGraphContainsExactly(graph, LLGA_FUSION_GROUP, 1)

    @llga_fp32_bf16_test_env
    def test_add_scalar(self):
        def add_scalar(x):
            return 42 + x + 3.14

        x = torch.rand(32, 32)
        graph, _ = self.checkTrace(add_scalar, [x])
        self.assertGraphContainsExactly(graph, LLGA_FUSION_GROUP, 1)

    @llga_fp32_bf16_test_env
    def test_add_with_duplicated_input(self):
        class M(nn.Module):
            def __init__(self):
                super(M, self).__init__()
                self.pool1 = nn.AdaptiveAvgPool2d((5, 7))
                self.pool2 = nn.AdaptiveAvgPool2d((5, 7))

            def forward(self, x):
                x1 = self.pool1(x)
                x2 = self.pool2(x)
                return x1 + x2

        m = M()
        x = torch.randn(1, 3, 4, 4)
        graph, _ = self.checkTrace(m, [x])
        self.assertGraphContainsExactly(graph, LLGA_FUSION_GROUP, 1)
        self.assertGraphContainsExactly(graph, "aten::adaptive_avg_pool2d", 1)
        self.assertFused(graph, "aten::add")

    @llga_fp32_bf16_test_env
    @unittest.skipIf(True, "Disable mul due to bad performance")
    def test_mul(self):
        def forward_mul(x, y):
            return torch.mul(x, y) * 3

        for x, y in self._gen_binary_inputs():
            graph, _ = self.checkTrace(forward_mul, [x, y])
            self.assertGraphContainsExactly(graph, LLGA_FUSION_GROUP, 2)

    @llga_fp32_bf16_test_env
    def test_identity_binary(self):
        def forward(x):
            return x * 1 + 0.0

        x = torch.rand(32)
        graph, _ = self.checkTrace(forward, [x])
        self.assertFused(graph, ["aten::add", "aten::mul"])

    @llga_fp32_bf16_test_env
    def test_matmul(self):
        def forward_matmul(x, y):
            return x.matmul(y)

        # TODO: support all shapes combination
        x = torch.randn(8, 128, 368)
        y = torch.randn(368, 3072)
        graph, _ = self.checkTrace(forward_matmul, [x, y])
        self.assertGraphContainsExactly(graph, LLGA_FUSION_GROUP, 1)

    @llga_fp32_bf16_test_env
    def test_mm(self):
        def forward_mm(x, y):
            return torch.mm(x, y)

        x = torch.randn(2, 3)
        y = torch.randn(3, 3)
        graph, _ = self.checkTrace(forward_mm, [x, y])
        self.assertGraphContainsExactly(graph, LLGA_FUSION_GROUP, 1)

    @llga_fp32_bf16_test_env
    def test_layer_norm(self):
        # TODO: support more normalized_shape
        m = torch.nn.LayerNorm(10)
        x = torch.randn(2, 5, 10, 10)
        graph, _ = self.checkTrace(m, [x])
        self.assertGraphContainsExactly(graph, LLGA_FUSION_GROUP, 1)

    @llga_fp32_bf16_test_env
    def test_unsupported_layer_norm(self):
        class M(nn.Module):
            def __init__(self):
                super(M, self).__init__()

            def forward(self, x):
                # The value of normalized_shape is dependent on the input
                return F.layer_norm(x, x.shape)

        x = torch.randn(2, 5, 10, 10)
        m = M()

        graph, _ = self.checkTrace(m, [x])
        self.assertGraphContainsExactly(graph, LLGA_FUSION_GROUP, 0)

    @llga_fp32_bf16_test_env
    def test_cat(self):
        def cat_along_dim(d):
            def forward_cat(*inputs):
                return torch.cat(inputs, d)

            return forward_cat

        for xshape in [
            [8, 8, 8, 8],
            [64, 8, 32],
            [2048, 64],
        ]:
            for d in range(len(xshape)):
                x = torch.rand(xshape)
                graph, _ = self.checkTrace(cat_along_dim(d), [x, x, x])
                self.assertGraphContainsExactly(graph, LLGA_FUSION_GROUP, 1)

    @llga_fp32_bf16_test_env
    def test_linear(self):
        for freeze in [True, False]:
            for bias in [True, False]:
                x = torch.randn(32, 28)
                m = torch.nn.Linear(in_features=28, out_features=64, bias=bias)

                graph, _ = self.checkTrace(m, [x], freeze)
                self.assertGraphContainsExactly(graph, LLGA_FUSION_GROUP, 1)
                self.assertFused(graph, ["aten::linear"])

    @llga_fp32_bf16_test_env
    def test_bmm(self):
        class M(nn.Module):
            def __init__(self):
                super(M, self).__init__()

            def forward(self, x, y):
                return x.matmul(y)

        x = torch.randn(128, 16, 384, 64)
        y = torch.randn(128, 16, 64, 384)
        m = M()

        graph, _ = self.checkTrace(m, [x, y])
        self.assertGraphContainsExactly(graph, LLGA_FUSION_GROUP, 1)
        self.assertFused(graph, ["aten::matmul"])

    @llga_fp32_bf16_test_env
    def test_bmm_mean(self):
        class M(nn.Module):
            def __init__(self):
                super(M, self).__init__()

            def forward(self, x, y):
                z = x.matmul(y)
                z = torch.mean(z, dim=0, keepdim=True)
                return z

        x = torch.randn(128, 16, 384, 64)
        y = torch.randn(128, 16, 64, 384)
        m = M()

        graph, _ = self.checkTrace(m, [x, y])
        # single op partitions
        self.assertGraphContainsExactly(graph, LLGA_FUSION_GROUP, 2)

    @llga_fp32_bf16_test_env
    def test_max(self):
        class M(nn.Module):
            def __init__(self):
                super(M, self).__init__()

            def forward(self, x, y):
                return torch.max(x, y)

        x = torch.randn(1, 3, 32, 32)
        y = torch.randn(1, 3, 32, 32)
        m = M()

        graph, _ = self.checkTrace(m, [x, y])
        self.assertGraphContainsExactly(graph, LLGA_FUSION_GROUP, 1)

    @llga_fp32_bf16_test_env
    def test_bmm_div(self):
        class M(nn.Module):
            def __init__(self):
                super(M, self).__init__()

            def forward(self, x, y):
                return x.matmul(y) / 2

        x = torch.randn(128, 16, 384, 64)
        y = torch.randn(128, 16, 64, 384)
        m = M()

        graph, _ = self.checkTrace(m, [x, y])
        self.assertGraphContainsExactly(graph, LLGA_FUSION_GROUP, 1)
        self.assertFused(graph, ["aten::matmul", "aten::div"])

    @llga_fp32_bf16_test_env
    def test_bmm_div_add(self):
        class M(nn.Module):
            def __init__(self):
                super(M, self).__init__()

            def forward(self, x, y, z):
                return x.matmul(y) / 2 + z

        x = torch.randn(128, 16, 5, 64)
        y = torch.randn(128, 16, 64, 5)
        z = torch.randn(128, 1, 1, 5)
        m = M()

        graph, _ = self.checkTrace(m, [x, y, z])
        self.assertGraphContainsExactly(graph, LLGA_FUSION_GROUP, 1)
        self.assertFused(graph, ["aten::matmul", "aten::div", "aten::add"])

    @llga_fp32_bf16_test_env
    def test_to(self):
        class M(nn.Module):
            def __init__(self, dtype):
                super(M, self).__init__()
                self.dtype = dtype

            def forward(self, x):
                return x.to(dtype=self.dtype)

        for src_dtype, dst_dtype in [
            [torch.bfloat16, torch.float],
            [torch.float, torch.bfloat16],
        ]:
            x = torch.randn((1, 16, 4, 64), dtype=src_dtype)
            m = M(dst_dtype)

            graph, _ = self.checkTrace(m, [x])
            # we do not rewrite single to
            self.assertGraphContainsExactly(graph, LLGA_FUSION_GROUP, 0)

    @llga_fp32_bf16_test_env
    def test_typecheck(self):
        x = torch.rand(32, 28)
        m = torch.nn.Linear(in_features=28, out_features=64, bias=True)
        graph, traced = self.checkTrace(m, [x])
        self.assertGraphContainsExactly(graph, LLGA_FUSION_GROUP, 1)
        self.assertFused(graph, ["aten::linear"])
        # change the shape of the input, we should enter fallback graph
        x = torch.rand(5, 28)
        self.assertEqual(m(x), traced(x))

    @llga_fp32_bf16_test_env
    def test_unsupported_dtype(self):
        class M(nn.Module):
            def __init__(self):
                super(M, self).__init__()

            def forward(self, x):
                x = torch.fft.fftn(x)
                x = torch.abs(x)
                return x

        x = torch.rand(10, 10, dtype=torch.complex64)
        m = M()
        graph, traced = self.checkTrace(m, [x])
        self.assertGraphContainsExactly(graph, LLGA_FUSION_GROUP, 0)
        self.assertGraphContainsExactly(graph, "aten::abs", 1)

    @llga_fp32_bf16_test_env
    # Currently graph with sub-block is unsupported
    # %z : Tensor = prim::If(%8)
    #     block0():
    #     %z.7 : Tensor = aten::mul(%z.1, %y.1)
    #     -> (%z.7)
    #     block1():
    #     %z.13 : Tensor = aten::mul(%z.1, %x.1)
    #     -> (%z.13)
    # return (%z)
    def test_block(self):
        class M(nn.Module):
            def __init__(self):
                super(M, self).__init__()

            def forward(self, x, y, z):
                if z[0][0] > 0:
                    z = z * y
                else:
                    z = z * x
                return z

        x = torch.rand(10, 10)
        y = torch.rand(10, 10)
        z = torch.rand(10, 10)
        m = M()
        graph, scripted = self.checkScript(m, [x, y, z])
        self.assertGraphContainsExactly(graph, LLGA_FUSION_GROUP, 0)


class TestFusionPattern(JitLlgaTestCase):
    @llga_fp32_bf16_test_env
    def test_conv2d_eltwise(self):
        class M(nn.Module):
            def __init__(self, eltwise_fn):
                super(M, self).__init__()
                self.conv1 = nn.Conv2d(32, 32, 3, padding=1, bias=True)
                self.conv2 = nn.Conv2d(32, 32, 3, padding=1, bias=False)
                self.eltwise = eltwise_fn

            def forward(self, x):
                x = self.conv1(x)
                x = self.eltwise(x)
                x = self.conv2(x)
                x = self.eltwise(x)
                return x

        for eltwise in [
            "relu",
            "leaky_relu",
            "sigmoid",
            "round",
            "abs",
            "square",
            "abs",
            "round",
            "exp",
            "hardswish",
            "tanh",
            "hardtanh",
            "mish",
        ]:
            for inplace in [False, True]:
                eltwise_fn_name = eltwise + "_" if inplace else eltwise
                eltwise_fn = get_eltwise_fn(eltwise_fn_name)

                m = M(eltwise_fn)
                x = torch.rand(1, 32, 28, 28)
                graph, _ = self.checkTrace(m, [x])
                self.assertGraphContainsExactly(graph, LLGA_FUSION_GROUP, 2)
                # test if relu_ is replace with relu by mutation removal pass
                self.assertFused(graph, ["aten::" + eltwise_fn_name])
                # test if relu is fused into the fusion group
                self.assertFused(graph, ["aten::" + eltwise])

    @unittest.skip("Accuracy issue for conv+relu+TypeCast and conv+bn+relu+TypeCast")
    @llga_fp32_bf16_test_env
    def test_type_promotion(self):
        class M(nn.Module):
            def __init__(
                self,
            ):
                super(M, self).__init__()
                self.conv1 = nn.Conv2d(32, 32, 1)
                self.bn1 = nn.BatchNorm2d(32)
                self.conv2 = nn.Conv2d(32, 32, 1, dtype=torch.bfloat16)
                self.bn2 = nn.BatchNorm2d(32, dtype=torch.bfloat16)

            def forward(self, x, y):
                y = self.conv2(y)
                y = self.bn2(y)
                y = torch.nn.functional.relu(y)
                x = self.conv1(x)
                x = self.bn1(x)
                x = torch.nn.functional.relu(x)
                z = y + x
                return z

        m = M()
        x = torch.randn(3, 32, 32, 32)
        y = torch.randn(3, 32, 32, 32, dtype=torch.bfloat16)
        graph, _ = self.checkTrace(m, [x, y])
        self.assertGraphContainsExactly(graph, LLGA_FUSION_GROUP, 2)

    @llga_fp32_bf16_test_env
    def test_conv2d_clamp(self):
        class M(nn.Module):
            def __init__(self):
                super(M, self).__init__()
                self.conv1 = nn.Conv2d(32, 32, 3, padding=1, bias=True)
                self.conv2 = nn.Conv2d(32, 32, 3, padding=1, bias=True)
                self.conv3 = nn.Conv2d(32, 32, 3, padding=1, bias=True)
                self.conv4 = nn.Conv2d(32, 32, 3, padding=1, bias=True)
                self.conv5 = nn.Conv2d(32, 32, 3, padding=1, bias=True)

            def forward(self, x):
                x = self.conv1(x)
                x = torch.clamp(x, min=float("-inf"))
                x = self.conv2(x)
                x = torch.clamp(x, min=-5)
                x = self.conv3(x)
                x = torch.clamp(x, min=0, max=float("inf"))
                x = self.conv4(x)
                x = torch.clamp(x, min=1, max=5)
                x = self.conv5(x)
                x = torch.clamp(x, max=2)
                return x

        for inplace in [False, True]:
            for memory_format in [torch.contiguous_format, torch.channels_last]:
                x = torch.rand(1, 32, 28, 28).to(memory_format=memory_format)
                m = M()
                graph, _ = self.checkTrace(m, [x])
                self.assertGraphContainsExactly(graph, LLGA_FUSION_GROUP, 5)
                self.assertFused(graph, ["aten::_convolution", "aten::clamp"])

    @llga_fp32_bf16_test_env
    def test_ensure_tensor_is_rewrapped(self):
        class M(nn.Module):
            def __init__(self, eltwise_fn, data_type):
                super(M, self).__init__()
                self.conv1 = nn.Conv2d(32, 32, 3, padding=1, bias=True, dtype=data_type)
                self.conv2 = nn.Conv2d(32, 32, 3, padding=1, bias=True, dtype=data_type)
                self.conv3 = nn.Conv2d(32, 32, 3, padding=1, bias=True, dtype=data_type)
                self.conv4 = nn.Conv2d(32, 32, 3, padding=1, bias=True, dtype=data_type)
                self.eltwise = eltwise_fn
                self.adaptive_avg_pool_2d = nn.AdaptiveAvgPool2d((5, 7))

            def forward(self, x, y):
                x = self.conv1(x)
                x = self.eltwise(x)
                x = self.conv2(x)
                x = self.eltwise(x)
                y = self.conv3(y)
                y = self.eltwise(y)
                y = self.conv4(y)
                y = self.eltwise(y)
                x = torch.add(x, y)
                x = self.adaptive_avg_pool_2d(x)
                return x

        eltwise_fn_name = "relu"
        eltwise_fn = get_eltwise_fn(eltwise_fn_name)
        for data_type in [torch.bfloat16, torch.float]:
            m = M(eltwise_fn, data_type)
            m = m.to(memory_format=torch.channels_last)
            x = torch.rand(1, 32, 28, 28, dtype=data_type).to(
                memory_format=torch.channels_last
            )
            y = torch.rand(1, 32, 28, 28, dtype=data_type).to(
                memory_format=torch.channels_last
            )
            # Simply test if the output is accurate
            # The output of the fourth partition is input to adaptive_avg_pool2d, which is
            # unsupported by LLGA. In resnext101 32x16d, we had encountered an accuracy issue.
            # The UT checks that the input to adaptive_avg_pool_2d has not been wrapped by
            # LlgaTensorImpl (assertEqual would fail in that case).
            graph, _ = self.checkTrace(m, [x, y])
            self.assertGraphContainsExactly(graph, LLGA_FUSION_GROUP, 4)

    @llga_fp32_bf16_test_env
    def test_conv2d_bn(self):
        class M(nn.Module):
            def __init__(self):
                super(M, self).__init__()
                self.conv1 = nn.Conv2d(32, 32, 3, padding=1, bias=True)
                self.bn1 = nn.BatchNorm2d(32)

            def forward(self, x):
                x = self.conv1(x)
                x = self.bn1(x)
                return x

        m = M().eval()
        x = torch.rand(1, 32, 28, 28)
        graph, _ = self.checkTrace(m, [x])
        self.assertGraphContainsExactly(graph, LLGA_FUSION_GROUP, 1)
        self.assertFused(graph, ["aten::_convolution", "aten::batch_norm"])

    @llga_fp32_bf16_test_env
    def test_conv2d_bn_relu(self):
        class M(nn.Module):
            def __init__(self):
                super(M, self).__init__()
                self.conv1 = nn.Conv2d(32, 32, 3, padding=1, bias=True)
                self.bn1 = nn.BatchNorm2d(32)

            def forward(self, x):
                x = self.conv1(x)
                x = self.bn1(x)
                x = F.relu(x)
                return x

        m = M().eval()
        x = torch.rand(1, 32, 28, 28)
        graph, _ = self.checkTrace(m, [x])
        self.assertGraphContainsExactly(graph, LLGA_FUSION_GROUP, 1)
        self.assertFused(
            graph, ["aten::_convolution", "aten::batch_norm", "aten::relu"]
        )

    @llga_fp32_bf16_test_env
    def test_bn2d_eltwise(self):
        class M(nn.Module):
            def __init__(self, eltwise_fn):
                super(M, self).__init__()
                self.eltwise = eltwise_fn
                self.bn = nn.BatchNorm2d(32)

            def forward(self, x):
                x = self.bn(x)
                x = self.eltwise(x)
                return x

        for eltwise in ["relu"]:
            eltwise_fn = get_eltwise_fn(eltwise)
            m = M(eltwise_fn).eval()
            x = torch.rand(1, 32, 28, 28)
            graph, _ = self.checkTrace(m, [x])
            self.assertGraphContainsExactly(graph, LLGA_FUSION_GROUP, 1)
            self.assertFused(graph, ["aten::batch_norm", "aten::" + eltwise])

    @llga_fp32_bf16_test_env
    def test_remove_redundant_to(self):
        class M(nn.Module):
            def __init__(
                self,
            ):
                super(M, self).__init__()
                self.conv1 = nn.Conv2d(32, 32, 1)
                self.bn1 = nn.BatchNorm2d(32)

            def forward(self, x):
                x = self.conv1(x)
                x = x.to(torch.float32)
                x = self.bn1(x)
                x = nn.functional.relu(x)
                return x

        m = M()
        x = torch.randn(3, 32, 32, 32)
        graph, _ = self.checkTrace(m, [x])
        self.assertGraphContainsExactly(graph, LLGA_FUSION_GROUP, 1)

    @llga_fp32_bf16_test_env
    def test_avg_pool2d_add(self):
        class M(nn.Module):
            def __init__(self):
                super(M, self).__init__()
                self.pool1 = nn.AvgPool2d(
                    3, stride=1, padding=1, count_include_pad=False
                )
                self.pool2 = nn.AvgPool2d(
                    3, stride=1, padding=1, count_include_pad=False
                )

            def forward(self, x):
                x1 = self.pool1(x)
                x2 = self.pool2(x)
                return x1 + x2

        m = M()
        x = torch.randn(1, 3, 4, 4)
        graph, _ = self.checkTrace(m, [x])
        self.assertGraphContainsExactly(graph, LLGA_FUSION_GROUP, 1)
        self.assertFused(graph, ["aten::avg_pool2d", "aten::add"])

    @unittest.skip("Semi-Compiler unit-test")
    @llga_fp32_bf16_test_env
    def test_mha_pattern(self):
        def forward_test(x, y, z, a):
            tmp = torch.matmul(x, y) / 8.0 + a
            tmp = torch.softmax(tmp, -1)
            tmp = tmp.matmul(z)
            tmp = torch.permute(tmp, (0, 2, 1, 3))
            return tmp.contiguous()

        x = torch.randn(128, 16, 384, 64)
        y = torch.randn(128, 16, 64, 384)
        z = torch.randn(128, 16, 384, 64)
        a = torch.rand(128, 1, 1, 384)

        graph, _ = self.checkTrace(forward_test, [x, y, z, a])
        self.assertGraphContainsExactly(graph, LLGA_FUSION_GROUP, 1)
        self.assertFused(
            graph,
            [
                "aten::matmul",
                "aten::div",
                "aten:add",
                "aten:softmax",
                "aten::permute",
                "aten::contiguous",
            ],
        )

    @llga_fp32_bf16_test_env
    def test_do_not_map_permute(self):
        def forward_test(x, y, z, a):
            tmp = torch.matmul(x, y) / 8.0 + a
            tmp = torch.softmax(tmp, -1)
            tmp = tmp.matmul(z)
            temp = tmp.view(tmp.numel())
            tmp = torch.permute(tmp, (0, 2, 1, 3))
            temp.add_(-1)
            return tmp.contiguous()

        x = torch.randn(128, 16, 384, 64)
        y = torch.randn(128, 16, 64, 384)
        z = torch.randn(128, 16, 384, 64)
        a = torch.rand(128, 1, 1, 384)

        graph, _ = self.checkTrace(forward_test, [x, y, z, a])
        self.assertFused(
            graph,
            [
                "aten::matmul",
                "aten::div",
                "aten::add",
                "aten::softmax",
                "aten::contiguous",
            ],
        )

    @llga_fp32_bf16_test_env
    def test_no_contiguous_no_op(self):
        def forward(x):
            return x.contiguous()

        x = torch.rand(32, 28)
        graph, traced = self.checkTrace(forward, [x])
        self.assertGraphContainsExactly(graph, LLGA_FUSION_GROUP, 0)

    @llga_fp32_bf16_test_env
    def test_contiguous_mapping_padded(self):
        def forward(x):
            tmp = torch.as_strided(x, (15, 15), (16, 1))
            return tmp.contiguous()

        x = torch.rand(16, 16)
        graph, traced = self.checkTrace(forward, [x])
        self.assertGraphContainsExactly(graph, LLGA_FUSION_GROUP, 1)

    @llga_fp32_bf16_test_env
    def test_contiguous_mapping_zero_stride(self):
        def forward(x):
            tmp = torch.as_strided(x, (32, 28), (0, 1))
            return tmp.contiguous()

        x = torch.rand(28, 32)
        graph, traced = self.checkTrace(forward, [x])
        self.assertGraphContainsExactly(graph, LLGA_FUSION_GROUP, 1)

    @llga_fp32_bf16_test_env
    def test_linear_eltwise(self):
        class M(nn.Module):
            def __init__(self, eltwise_fn, bias):
                super(M, self).__init__()
                self.linear = nn.Linear(28, 64, bias)
                self.eltwise = eltwise_fn

            def forward(self, x):
                x = self.linear(x)
                x = self.eltwise(x)
                return x

        # TODO: use itertools.product once all combinations is supported
        for [has_bias, eltwise] in [
            [True, "relu"],
            [False, "relu"],
            [True, "gelu"],
            [False, "gelu"],
            [True, "sigmoid"],
            [False, "sigmoid"],
            [False, "hardtanh"],
            # [False, 'relu6'], # TODO: map relu6 in the bridge
            [False, "elu"],
        ]:
            eltwise_fn = get_eltwise_fn(eltwise)
            m = M(eltwise_fn, has_bias)
            x = torch.rand(32, 28, requires_grad=False)
            graph, _ = self.checkTrace(m, [x])
            self.assertGraphContainsExactly(graph, LLGA_FUSION_GROUP, 1)
            self.assertFused(graph, ["aten::" + eltwise])

    @llga_fp32_bf16_test_env
    def test_conv2d_sum(self):
        class M(nn.Module):
            def __init__(self, bias=False):
                super(M, self).__init__()
                self.conv1 = nn.Conv2d(32, 32, 3, padding=1, bias=bias)
                self.bn1 = nn.BatchNorm2d(32)
                self.conv2 = nn.Conv2d(32, 32, 3, padding=1, bias=bias)
                self.bn2 = nn.BatchNorm2d(32)
                self.relu = nn.ReLU()
                self.conv3 = nn.Conv2d(32, 32, 3, padding=1, bias=bias)
                self.bn3 = nn.BatchNorm2d(32)

            def forward(self, x, y):
                x = self.conv1(x)
                x = self.bn1(x)
                y = self.conv2(y)
                y = self.bn2(y)
                z = self.relu(x + y)
                z = self.conv3(z)
                z = self.bn3(z)
                return z

        for bias in [True, False]:
            m = M(bias).eval()
            x = torch.rand(1, 32, 16, 16, requires_grad=False)
            y = torch.rand(1, 32, 16, 16, requires_grad=False)
            graph, _ = self.checkTrace(m, [x, y])
            self.assertGraphContainsExactly(graph, LLGA_FUSION_GROUP, 3)

    @llga_fp32_bf16_test_env
    def test_wildcard(self):
        class M(nn.Module):
            def __init__(self):
                super(M, self).__init__()
                self.conv1 = nn.Conv2d(32, 32, 3, padding=1, bias=True)
                self.eltwise = nn.ReLU()

            def forward(self, x):
                x = self.conv1(x)
                y = self.eltwise(x)
                return [x, y]

        # The pattern is as the following:
        #      conv
        #     |    \
        # eltwise   \
        #    |       \
        #  ListConstruct
        #
        # The output of conv is used by a wildcard op: ListConstruct.
        # Thus conv-eltwise cannot be selected into the same Partition.
        m = M()
        x = torch.rand(1, 32, 28, 28)
        graph, _ = self.checkTrace(m, [x])
        self.assertGraphContainsExactly(graph, LLGA_FUSION_GROUP, 2)
        self.assertFused(graph, ["aten::_convolution", "aten::relu"])


class TestAPI(JitLlgaTestCase):
    def test_weight_cache_api(self):
        weight_cache_enabled_default_value = ipex._C._jit_llga_weight_cache_enabled()
        self.assertTrue(weight_cache_enabled_default_value)

        ipex._C._jit_set_llga_weight_cache_enabled(False)
        weight_cache_enabled = ipex._C._jit_llga_weight_cache_enabled()
        self.assertFalse(weight_cache_enabled)

        # set the value back to the default one
        ipex._C._jit_set_llga_weight_cache_enabled(weight_cache_enabled_default_value)


class TestDebugLog(JitLlgaTestCase):
    def test_fusion_group_name(self):
        num = 0
        num_debug_str = 0
        loc = os.path.dirname(os.path.abspath(__file__))
        with subprocess.Popen(
            'PYTORCH_JIT_LOG_LEVEL=":>>kernel:>>" python  -u {}/profile_ipex_op.py --llga'.format(
                loc
            ),
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        ) as p:
            for line in p.stdout.readlines():
                line = str(line, "utf-8").strip()
                if line.__contains__("LLGA_bridge::prepareRunArgs"):
                    num += 1
                if line.__contains__("Executing partition"):
                    num_debug_str += 1
        self.assertTrue(num == 2, "IPEX LLGA op profiling info not found.")
        self.assertTrue(num_debug_str > 0, "IPEX LLGA debug info not found")


@unittest.skip("Enable when integration with dynamo aot_autograd is more stable")
class TestDynamoAOT(JitLlgaTestCase):
    def test_dynamo_aot_ts_onednn(self):
        class Seq(nn.Module):
            def __init__(self):
                super().__init__()
                self.layers = nn.Sequential(
                    nn.Linear(10, 10),
                    nn.ReLU(),
                    nn.Linear(10, 10),
                    nn.ReLU(),
                )

            def forward(self, x):
                return self.layers(x)

        mod = Seq()

        import torch._dynamo

        aot_mod = torch._dynamo.optimize("aot_ts", nopython=True)(mod)

        for _ in range(10):
            with torch.jit.fuser("fuser3"):
                loss = aot_mod(torch.rand([10, 10])).sum()
                loss.backward()

        torch._dynamo.reset()


class TestModel(JitLlgaTestCase):
    @skipIfNoTorchVision
    @llga_fp32_bf16_test_env
    def _test_vision(self, model_name):
        m = getattr(torchvision.models, model_name)().eval()
        x = torch.rand(1, 3, 224, 224) / 10
        graph, _ = self.checkTrace(m, [x])
        self.assertFused(
            graph,
            [
                "aten::_convolution",
                "aten::batch_norm",
                "aten::relu",
                "aten::linear",
                "aten::avg_pool2d",
                "aten::max_pool2d",
            ],
        )


for model_name, enabled in [
    ["resnet50", True],
    ["resnext50_32x4d", True],
    ["resnext101_32x8d", True],
    ["densenet121", False],
    ["googlenet", TEST_SCIPY],
    ["mobilenet_v2", True],
    ["mnasnet1_0", True],
    ["squeezenet1_0", True],
    ["vgg16", True],
    ["alexnet", True],
    ["shufflenet_v2_x1_0", True],
    ["wide_resnet50_2", True],
]:

    def wrapper(mname):
        @unittest.skipIf(not enabled, "Disabled")
        def test(self):
            return self._test_vision(mname)

        return test

    setattr(TestModel, "test_vision_%s" % model_name, wrapper(model_name))


if __name__ == "__main__":
    run_tests()
