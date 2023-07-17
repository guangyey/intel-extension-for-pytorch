# This Python file uses the following encoding: utf-8
# !/usr/bin/env python

import unittest
import itertools
import torch
import torch.nn as nn
import torch.nn.functional as F
from test_ao_jit_llga_utils import (
    JitLlgaTestCase,
    LLGA_FUSION_GROUP,
    get_eltwise_fn,
)
from torch.quantization.quantize_fx import prepare_fx, convert_fx
from torch.ao.quantization.quantize_fx import convert_to_reference_fx, prepare_qat_fx

from torch.ao.quantization import (
    MinMaxObserver,
    PerChannelMinMaxObserver,
    HistogramObserver,
    QConfig,
)

default_weight_observer = PerChannelMinMaxObserver.with_args(
    dtype=torch.qint8, qscheme=torch.per_channel_symmetric
)

static_qconfig = [
    QConfig(
        activation=MinMaxObserver.with_args(
            qscheme=torch.per_tensor_affine, dtype=torch.quint8
        ),
        weight=default_weight_observer,
    ),
    QConfig(
        activation=MinMaxObserver.with_args(
            qscheme=torch.per_tensor_symmetric, dtype=torch.qint8
        ),
        weight=default_weight_observer,
    ),
    QConfig(
        activation=HistogramObserver.with_args(
            qscheme=torch.per_tensor_affine, dtype=torch.quint8, reduce_range=True
        ),
        weight=default_weight_observer,
    ),
    QConfig(
        activation=HistogramObserver.with_args(
            qscheme=torch.per_tensor_symmetric, dtype=torch.qint8, reduce_range=True
        ),
        weight=default_weight_observer,
    ),
]

try:
    import torchvision

    HAS_TORCHVISION = True
except ImportError:
    HAS_TORCHVISION = False
except RuntimeError:
    HAS_TORCHVISION = False
skipIfNoTorchVision = unittest.skipIf(not HAS_TORCHVISION, "no torchvision")


class TestOp(JitLlgaTestCase):
    def test_conv_int8_in_f32_out(self):
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
            memory_format,
            module,
        ] in itertools.product(
            [7],
            [8],
            [7],
            [3],
            [0, 2],
            [1, 2],
            [1, 2],
            [1, 2],
            [True, False],
            [torch.contiguous_format, torch.channels_last],
            [torch.nn.Conv2d, torch.nn.Conv3d],
        ):
            m = module(
                in_channels=in_channels * g,
                out_channels=out_channels * g,
                kernel_size=kernel,
                padding=padding,
                stride=stride,
                dilation=dilation,
                groups=g,
                bias=bias,
            )
            input_shape = [1, in_channels * g, spatial, spatial]
            if isinstance(m, torch.nn.Conv3d):
                input_shape.append(spatial)
                if memory_format == torch.channels_last:
                    memory_format = torch.channels_last_3d
            x = torch.rand(input_shape).to(memory_format=memory_format)
            patterns = [["aten::dequantize", "aten::_convolution"]]
            # TODO: enable more config case.
            for qconfig in static_qconfig:
                input_shape[0] = 5
                x_var = [torch.rand(input_shape, requires_grad=False)]
                graph = self.checkQuantizeTrace(
                    m, [x], x_var=x_var, atol=2e-1, qconfig=qconfig
                )
                self.assertGraphContainsExactly(graph, LLGA_FUSION_GROUP, 1)
                self.assertFused(graph, ["aten::_convolution", "aten::dequantize"])
                self.checkPatterns(graph, patterns)

    def test_deconv_int8_in_f32_out(self):
        class M(nn.Module):
            def __init__(
                self,
                in_channels,
                out_channels,
                kernel_size,
                padding,
                stride,
                dilation,
                groups,
                bias,
                module,
            ):
                super(M, self).__init__()
                self.conv = module(
                    in_channels=in_channels * groups,
                    out_channels=out_channels * groups,
                    kernel_size=kernel_size,
                    padding=padding,
                    stride=stride,
                    dilation=dilation,
                    groups=groups,
                    bias=bias,
                )
                inverse_module = (
                    torch.nn.ConvTranspose2d
                    if (module == torch.nn.Conv2d)
                    else torch.nn.ConvTranspose3d
                )
                self.deconv = inverse_module(
                    in_channels=out_channels * groups,
                    out_channels=in_channels * groups,
                    kernel_size=kernel_size,
                    padding=padding,
                    stride=stride,
                    dilation=dilation,
                    groups=groups,
                    bias=bias,
                )

            def forward(self, x):
                y = self.conv(x)
                return self.deconv(y)

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
            memory_format,
            module,
        ] in itertools.product(
            [7],
            [8],
            [7],
            [3],
            [0, 2],
            [1, 2],
            [1, 2],
            [1, 2],
            [True, False],
            [torch.contiguous_format, torch.channels_last],
            [torch.nn.Conv2d, torch.nn.Conv3d],
        ):
            m = M(
                in_channels=in_channels,
                out_channels=out_channels,
                kernel_size=kernel,
                padding=padding,
                stride=stride,
                dilation=dilation,
                groups=g,
                bias=bias,
                module=module,
            )

            input_shape = [1, in_channels * g, spatial, spatial]
            if module == torch.nn.Conv3d:
                input_shape.append(spatial)
                if memory_format == torch.channels_last:
                    memory_format = torch.channels_last_3d
            x = torch.rand(input_shape).to(memory_format=memory_format)

            patterns = [
                ["aten::dequantize", "aten::_convolution"],
                ["aten::dequantize", "aten::_convolution"],
            ]

            # TODO: enable more config case.
            for qconfig in static_qconfig:
                input_shape[0] = 5
                graph = self.checkQuantizeTrace(m, [x], atol=2e-1, qconfig=qconfig)
                self.assertGraphContainsExactly(graph, LLGA_FUSION_GROUP, 2)
                self.assertFused(graph, ["aten::_convolution", "aten::dequantize"])
                self.checkPatterns(graph, patterns)

    def test_conv_no_freeze(self):
        m = nn.Conv2d(
            in_channels=3,
            out_channels=3,
            kernel_size=3,
            padding=1,
            stride=1,
            dilation=1,
            groups=1,
            bias=True,
        )
        x = torch.rand(1, 3, 5, 5)
        graph = self.checkQuantizeTrace(
            m, [x], atol=2e-1, qconfig=static_qconfig[0], freeze=False
        )
        patterns = [
            ["aten::dequantize", "aten::quantize_per_channel", "aten::_convolution"]
        ]
        self.assertGraphContainsExactly(graph, LLGA_FUSION_GROUP, 1)
        self.assertFused(
            graph,
            ["aten::_convolution", "aten::quantize_per_channel", "aten::dequantize"],
        )
        self.checkPatterns(graph, patterns)

    def test_conv_share_dequant_weight(self):
        class M(nn.Module):
            def __init__(self):
                super(M, self).__init__()
                self.conv = nn.Conv2d(32, 32, 3, padding=1, bias=True)

            def forward(self, x):
                # type: (List[Tensor]) -> Tensor
                all_logits = []
                for feature in x:
                    logits = self.conv(feature)
                    all_logits.append(logits)
                return torch.cat(all_logits, dim=1)

        for memory_format in [torch.contiguous_format, torch.channels_last]:
            patterns = [
                ["aten::dequantize", "aten::_convolution"],
                ["aten::dequantize", "aten::_convolution"],
                ["aten::dequantize", "aten::_convolution"],
            ]
            a = torch.randn(1, 32, 28, 28).to(memory_format=memory_format)
            b = torch.randn(1, 32, 28, 28).to(memory_format=memory_format)
            c = torch.randn(1, 32, 28, 28).to(memory_format=memory_format)
            x = [a, b, c]
            for qconfig in static_qconfig:
                m = M()
                graph = self.checkQuantizeTrace(m, [x], atol=2e-1, qconfig=qconfig)
                self.assertGraphContainsExactly(graph, LLGA_FUSION_GROUP, 3)
                self.assertFused(graph, ["aten::_convolution", "aten::dequantize"])
                self.checkPatterns(graph, patterns)

    def test_linear_int8_in_f32_out(self):
        for bias in [True, False]:
            x = torch.rand(32, 28)
            m = torch.nn.Linear(in_features=28, out_features=64, bias=bias)

            patterns = [
                ["aten::dequantize", "aten::linear"],
            ]
            for qconfig in static_qconfig:
                graph = self.checkQuantizeTrace(m, [x], atol=1e-1, qconfig=qconfig)
                self.assertGraphContainsExactly(graph, LLGA_FUSION_GROUP, 1)
                self.assertFused(graph, ["aten::linear", "aten::dequantize"])
                self.checkPatterns(graph, patterns)

    def test_linear_int8_in_int8_out(self):
        class M(nn.Module):
            def __init__(self, bias):
                super(M, self).__init__()
                self.linear1 = nn.Linear(15, 20, bias=bias)
                self.linear2 = nn.Linear(20, 3, bias=bias)

            def forward(self, x, y):
                x = self.linear1(x)
                x = self.linear2(x)
                return x

        for bias in [True, False]:
            x = torch.randn(2, 15)
            y = torch.randn(2, 20)
            m = M(bias)

            patterns = [
                ["aten::dequantize", "aten::linear", "aten::quantize_per_tensor"],
                ["aten::dequantize", "aten::linear"],
            ]

            for qconfig in static_qconfig:
                graph = self.checkQuantizeTrace(m, [x, y], atol=2e-1, qconfig=qconfig)
                self.assertGraphContainsExactly(graph, LLGA_FUSION_GROUP, 2)
                self.assertFused(
                    graph,
                    ["aten::linear", "aten::quantize_per_channel", "aten::dequantize"],
                )
                self.checkPatterns(graph, patterns)

    def test_linear_int8_in_bf16_out(self):
        class M(nn.Module):
            def __init__(self, bias):
                super(M, self).__init__()
                self.linear1 = nn.Linear(15, 20, bias=bias)

            def forward(self, x):
                x = self.linear1(x)
                return x

        for bias in [True]:  # TODO：[True, False] when supported in backend
            x = torch.randn(2, 15)

            patterns = [
                ["aten::dequantize", "aten::to", "aten::linear"],
            ]

            for qconfig in static_qconfig:
                m = M(bias)
                graph = self.checkQuantizeTrace(
                    m, [x], atol=2e-1, qconfig=qconfig, int8_bf16=True
                )
                self.assertGraphContainsExactly(graph, LLGA_FUSION_GROUP, 1)
                # single aten::to won't be rewritten by llga backend
                self.assertFused(graph, ["aten::dequantize", "aten::linear"])
                self.checkPatterns(graph, patterns)

    def test_max_pool2d(self):
        class M(nn.Module):
            def __init__(self, **kargs):
                super(M, self).__init__()
                self.conv = nn.Conv2d(3, 3, 1, 1)
                self.max_pool = nn.MaxPool2d(**kargs)

            def forward(self, x):
                x = self.conv(x)
                x = self.max_pool(x)
                return x

        for [
            spatial,
            kernel,
            padding,
            stride,
            dilation,
            ceil_mode,
            memory_format,
        ] in itertools.product(
            [15],  # [15, 16], TODO: check backend
            [3, 5],  # [3, 4, 5], TODO: check backend
            [0, 1],
            [1, 2],  # [1, 2, 4], TODO: fix issue in pad calculation
            [1, 2],
            [True, False],
            [torch.contiguous_format, torch.channels_last],
        ):
            m = M(
                kernel_size=kernel,
                stride=stride,
                padding=padding,
                dilation=dilation,
                ceil_mode=ceil_mode,
            )
            x = torch.rand(1, 3, spatial, spatial).to(memory_format=memory_format)

            patterns = [
                [
                    "aten::dequantize",
                    "aten::dequantize",
                    "aten::_convolution",
                    "aten::quantize_per_tensor",
                ],
                ["aten::dequantize", "aten::max_pool2d", "aten::quantize_per_tensor"],
            ]
            for qconfig in static_qconfig:
                graph = self.checkQuantizeTrace(m, [x], atol=1e-1, qconfig=qconfig)
                self.assertGraphContainsExactly(graph, LLGA_FUSION_GROUP, 2)
                self.assertFused(graph, ["aten::max_pool2d"])
                self.checkPatterns(graph, patterns)

    def test_add_scalar_input(self):
        class M(torch.nn.Module):
            def __init__(self):
                super(M, self).__init__()

            def forward(self, x):
                x_shape1 = x.size()[0]
                x_shape2 = x.size()[1]
                y1 = x_shape1 + 2
                y2 = x_shape2 + 3
                return y1 + y2

        # input[0] to add being scalar is unsupported
        x = torch.randn(3, 3)
        m = M()
        graph = self.checkQuantizeTrace(m, [x], atol=2e-1)
        self.assertGraphContainsExactly(graph, LLGA_FUSION_GROUP, 0)
        self.assertGraphContainsExactly(graph, "aten::add", 3)

    def test_reshape_6D_linear(self):
        class M(nn.Module):
            def __init__(self):
                super(M, self).__init__()
                self.linear = torch.nn.Linear(
                    in_features=64, out_features=192, bias=True
                )

            def forward(self, x):
                x = x.reshape(4, 8, 7, 8, 8, 64).transpose(2, 3)
                x = self.linear(x)
                return x

        for bias in [True, False]:
            x = torch.randn(4, 56, 64, 64)
            m = M()

            patterns = [["aten::dequantize", "aten::linear"]]

            for qconfig in static_qconfig:
                graph = self.checkQuantizeTrace(m, [x], atol=2e-1, qconfig=qconfig)
                self.assertGraphContainsExactly(graph, LLGA_FUSION_GROUP, 1)
                self.assertFused(graph, ["aten::linear", "aten::dequantize"])
                self.checkPatterns(graph, patterns)

    def test_3d_bmm_int8_in_f32_out(self):
        class M(nn.Module):
            def __init__(self):
                super(M, self).__init__()

            def forward(self, x, y):
                return torch.bmm(x, y)

        x = torch.randn(128, 3, 4) * 0.1
        y = torch.randn(128, 4, 5) * 0.1
        patterns = [
            ["aten::dequantize", "aten::bmm"],
        ]
        m = M()
        graph = self.checkQuantizeTrace(m, [x, y], atol=2e-1)
        self.assertGraphContainsExactly(graph, LLGA_FUSION_GROUP, 1)
        self.assertFused(graph, ["aten::dequantize", "aten::bmm"])
        self.checkPatterns(graph, patterns)

    def test_bmm_int8_in_f32_out(self):
        class M(nn.Module):
            def __init__(self):
                super(M, self).__init__()

            def forward(self, x, y):
                mm_res = torch.matmul(x, y)
                return mm_res

        x = torch.randn(128, 16, 384, 64) * 0.1
        y = torch.randn(128, 1, 64, 384) * 0.1
        patterns = [
            ["aten::dequantize", "aten::matmul"],
        ]
        m = M()
        graph = self.checkQuantizeTrace(m, [x, y], atol=2e-1)
        self.assertGraphContainsExactly(graph, LLGA_FUSION_GROUP, 1)
        self.assertFused(graph, ["aten::matmul"])
        self.checkPatterns(graph, patterns)

    def test_strided_bmm_int8_in_bf16_out(self):
        class M(nn.Module):
            def __init__(self):
                super(M, self).__init__()
                self.num_attention_heads = 16
                self.attention_head_size = 4

            def forward(self, x, y):
                new_x_shape = x.size()[:-1] + (
                    self.num_attention_heads,
                    self.attention_head_size,
                )
                x = x.view(*new_x_shape)
                z1 = x.permute(0, 2, 1, 3)

                new_y_shape2 = y.size()[:-1] + (
                    self.num_attention_heads,
                    self.attention_head_size,
                )
                y = y.view(*new_y_shape2)
                z2 = y.permute(0, 2, 1, 3)

                # inputs to matmul has been permuted or transposed, thus are strided tensor
                return torch.matmul(z1, z2.transpose(-1, -2))

        m = M()
        x = torch.randn(2, 3, 64)
        y = torch.randn(2, 3, 64)

        patterns = [
            ["aten::dequantize", "aten::to", "aten::matmul"],
        ]

        graph = self.checkQuantizeTrace(m, [x, y], atol=2e-1, int8_bf16=True)
        self.assertGraphContainsExactly(graph, LLGA_FUSION_GROUP, 1)
        self.assertFused(graph, ["aten::matmul", "aten::dequantize"])
        self.checkPatterns(graph, patterns)

    def test_mixed_precision_softmax(self):
        class M(torch.nn.Module):
            def __init__(self):
                super(M, self).__init__()

            def forward(self, x, y, z, a):
                o = torch.matmul(x, y) / 8.0
                o = o + a.to(o.dtype)
                o = torch.softmax(o, -1)
                o = o.matmul(z)
                return o

        x = torch.randn(1, 16, 16, 64)
        y = torch.randn(1, 16, 64, 16)
        z = torch.randn(1, 16, 16, 64)
        a = torch.randn(1, 1, 1, 16)
        m = M()

        # fp32 in int8 out softmax
        graph = self.checkQuantizeTrace(m, [x, y, z, a], atol=2e-1, int8_bf16=False)
        self.assertFused(
            graph, ["aten::matmul", "aten::div", "aten::add", "aten::softmax"]
        )

        # bf16 in int8 out softmax
        graph = self.checkQuantizeTrace(m, [x, y, z, a], atol=2e-1, int8_bf16=True)
        self.assertFused(
            graph, ["aten::matmul", "aten::div", "aten::add", "aten::softmax"]
        )


class TestFusionPattern(JitLlgaTestCase):
    def test_conv2d_eltwise(self):
        class M(nn.Module):
            def __init__(self, eltwise_fn):
                super(M, self).__init__()
                self.conv1 = nn.Conv2d(32, 32, 3, padding=1, bias=True)
                self.conv2 = nn.Conv2d(32, 32, 3, padding=1, bias=True)
                self.eltwise = eltwise_fn

            def forward(self, x):
                x = self.conv1(x)
                x = self.eltwise(x)
                x = self.conv2(x)
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
                for memory_format in [torch.contiguous_format, torch.channels_last]:
                    eltwise_fn_name = eltwise + "_" if inplace else eltwise
                    eltwise_fn = get_eltwise_fn(eltwise_fn_name)

                    m = M(eltwise_fn)
                    x = torch.rand(1, 32, 28, 28).to(memory_format=memory_format)

                    patterns = [
                        [
                            "aten::dequantize",
                            "aten::_convolution",
                            "aten::" + eltwise,
                            "aten::quantize_per_tensor",
                        ],  # inplace op will become outplace op on the JIT graph
                        ["aten::dequantize", "aten::_convolution"],
                    ]
                    for qconfig in static_qconfig:
                        graph = self.checkQuantizeTrace(
                            m, [x], atol=2e-1, qconfig=qconfig
                        )
                        self.assertGraphContainsExactly(graph, LLGA_FUSION_GROUP, 2)
                        self.assertFused(
                            graph,
                            [
                                "aten::_convolution",
                                "aten::" + eltwise,
                                "aten::quantize_per_channel",
                                "aten::dequantize",
                            ],
                        )
                        self.checkPatterns(graph, patterns)

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
                for qconfig in static_qconfig:
                    graph = self.checkQuantizeTrace(m, [x], atol=2e-1, qconfig=qconfig)
                    self.assertGraphContainsExactly(graph, LLGA_FUSION_GROUP, 5)
                    self.assertFused(
                        graph,
                        [
                            "aten::_convolution",
                            "aten::" + "clamp",
                            "aten::quantize_per_channel",
                            "aten::dequantize",
                        ],
                    )

    def test_conv2d_silu(self):
        class M(nn.Module):
            def __init__(self, inplace):
                super(M, self).__init__()
                self.conv1 = nn.Conv2d(32, 32, 3, padding=1, bias=True)
                self.conv2 = nn.Conv2d(32, 32, 3, padding=1, bias=True)
                self.eltwise = nn.SiLU(inplace=inplace)

            def forward(self, x):
                x = self.conv1(x)
                x = self.eltwise(x)
                x = self.conv2(x)
                return x

        for inplace in [False, True]:
            for memory_format in [torch.contiguous_format, torch.channels_last]:
                m = M(inplace)
                x = torch.rand(1, 32, 28, 28).to(memory_format=memory_format)

                graph = self.checkQuantizeTrace(m, [x], atol=2e-1)
                self.assertGraphContainsExactly(graph, LLGA_FUSION_GROUP, 2)

                silu_op = "aten::silu_" if inplace else "aten::silu"

                # oneDNN graph does not have silu OP. The bridge will convert silu to sigmoid - mul
                patterns = [
                    [
                        "aten::dequantize",
                        "aten::_convolution",
                        "aten::sigmoid",
                        "aten::mul",
                        "aten::quantize_per_tensor",
                    ],  # inplace op will become outplace op on the JIT graph
                    ["aten::dequantize", "aten::_convolution"],
                ]

                self.assertFused(
                    graph, ["aten::_convolution", silu_op, "aten::dequantize"]
                )
                self.checkPatterns(graph, patterns)

    def test_deconv_silu(self):
        class M(nn.Module):
            def __init__(self, inplace):
                super(M, self).__init__()
                self.deconv = nn.ConvTranspose2d(3, 2, 3, stride=2)
                self.eltwise = nn.SiLU(inplace=inplace)

            def forward(self, x):
                x = self.deconv(x)
                x = self.eltwise(x)
                return x

        for inplace in [False, True]:
            m = M(inplace)
            x = torch.rand(1, 3, 28, 28)
            graph = self.checkQuantizeTrace(m, [x], atol=2e-1)
            patterns = [
                ["aten::dequantize", "aten::_convolution", "aten::sigmoid", "aten::mul"]
            ]
            self.assertGraphContainsExactly(graph, LLGA_FUSION_GROUP, 1)
            self.checkPatterns(graph, patterns)

    def test_ensure_tensor_is_rewrapped(self):
        class M(nn.Module):
            def __init__(self, eltwise_fn):
                super(M, self).__init__()
                self.conv1 = nn.Conv2d(32, 32, 3, padding=1, bias=True)
                self.conv2 = nn.Conv2d(32, 32, 3, padding=1, bias=True)
                self.eltwise = eltwise_fn
                self.adaptive_avg_pool_2d = nn.AdaptiveAvgPool2d((5, 7))

            def forward(self, x, y):
                x = self.conv1(x)
                y = self.conv2(y)
                y = self.eltwise(y)
                x = torch.add(x, y)
                x = self.adaptive_avg_pool_2d(x)
                return x

        eltwise_fn_name = "relu"
        eltwise_fn = get_eltwise_fn(eltwise_fn_name)

        m = M(eltwise_fn)
        x = torch.rand(1, 32, 28, 28).to(memory_format=torch.channels_last)
        y = torch.rand(1, 32, 28, 28).to(memory_format=torch.channels_last)
        for qconfig in static_qconfig:
            # The output of the fourth partition is input to adaptive_avg_pool2d, which is
            # unsupported by LLGA. In resnext101 32x16d, we had encountered an accuracy issue.
            # The UT checks that the input to adaptive_avg_pool_2d has not been wrapped by
            # LlgaTensorImpl (assertEqual would fail in that case).
            graph = self.checkQuantizeTrace(m, [x, y], atol=2e-1, qconfig=qconfig)
            self.assertGraphContainsExactly(graph, LLGA_FUSION_GROUP, 2)

    def test_conv2d_bn(self):
        class M(nn.Module):
            def __init__(self, bias):
                super(M, self).__init__()
                self.conv1 = nn.Conv2d(32, 5, 3, padding=1, bias=False)
                self.bn1 = nn.BatchNorm2d(5)

            def forward(self, x):
                x = self.conv1(x)
                x = self.bn1(x)
                return x

        for bias in [False, True]:
            for memory_format in [torch.contiguous_format, torch.channels_last]:
                m = M(bias).eval()
                x = torch.rand(1, 32, 16, 16).to(memory_format=memory_format)
                # TODO: This shape will fail
                # x = torch.rand(1, 32, 28, 28)

                patterns = [["aten::dequantize", "aten::_convolution"]]
                # TODO: add torch.per_tensor_symmetric case.
                for qconfig in static_qconfig:
                    graph = self.checkQuantizeTrace(m, [x], atol=1e-1, qconfig=qconfig)
                    self.assertGraphContainsExactly(graph, LLGA_FUSION_GROUP, 1)
                    self.assertFused(
                        graph,
                        [
                            "aten::_convolution",
                            "aten::quantize_per_channel",
                            "aten::dequantize",
                        ],
                    )
                    self.checkPatterns(graph, patterns)

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

        for memory_format in [torch.contiguous_format, torch.channels_last]:
            m = M().eval()
            x = torch.rand(1, 32, 28, 28).to(memory_format=memory_format)
            patterns = [
                ["aten::dequantize", "aten::_convolution", "aten::relu"],
            ]
            for qconfig in static_qconfig:
                graph = self.checkQuantizeTrace(m, [x], atol=1e-1, qconfig=qconfig)
                self.assertGraphContainsExactly(graph, LLGA_FUSION_GROUP, 1)
                self.assertFused(
                    graph,
                    ["aten::_convolution", "aten::relu", "aten::quantize_per_channel"],
                )
                self.checkPatterns(graph, patterns)

    def test_linear_bn(self):
        class M(nn.Module):
            def __init__(self, dim):
                super(M, self).__init__()
                self.linear = nn.Linear(32, 32)
                if dim == 1:
                    self.input1 = torch.randn(1, 32)
                    self.bn = nn.BatchNorm1d(32)
                elif dim == 2:
                    self.input1 = torch.randn(1, 32, 32, 32)
                    self.bn = nn.BatchNorm2d(32)
                elif dim == 3:
                    self.input1 = torch.randn(1, 32, 32, 32, 32)
                    self.bn = nn.BatchNorm3d(32)

            def forward(self, x):
                x = self.linear(x)
                x = self.bn(x)
                return x

        for dim in [1, 2, 3]:
            m = M(dim=dim)
            x = m.input1
            patterns = [["aten::dequantize", "aten::linear"]]
            for qconfig in static_qconfig:
                graph = self.checkQuantizeTrace(m, [x], atol=2e-1, qconfig=qconfig)
                self.assertGraphContainsExactly(graph, LLGA_FUSION_GROUP, 1)
                self.assertFused(graph, ["ipex::batch_norm"])
                self.checkPatterns(graph, patterns)

    def test_conv_bn_linear_bn(self):
        class M(nn.Module):
            def __init__(
                self,
            ):
                super(M, self).__init__()
                self.input1 = torch.randn(1, 32, 32, 32)
                self.conv = nn.Conv2d(32, 32, 1)
                self.bn1 = nn.BatchNorm2d(32)
                self.linear = nn.Linear(32, 32)
                self.bn2 = nn.BatchNorm2d(32)

            def forward(self, x):
                x = self.conv(x)
                x = self.bn1(x)
                x = self.linear(x)
                x = self.bn2(x)
                return x

        m = M()
        x = m.input1
        patterns = [
            ["aten::dequantize", "aten::_convolution"],
            ["aten::dequantize", "aten::linear"],
        ]
        for qconfig in static_qconfig:
            graph = self.checkQuantizeTrace(m, [x], atol=2e-1, qconfig=qconfig)
            self.assertGraphContainsExactly(graph, LLGA_FUSION_GROUP, 2)
            self.assertFused(graph, ["ipex::batch_norm"])
            self.checkPatterns(graph, patterns)

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
            # [True, 'gelu'], # TODO: enable it once linear_gelu default recipe is fixed
            # [False, 'gelu'], # TODO: enable it once linear_gelu default recipe is fixed
            [True, "sigmoid"],
            [False, "sigmoid"],
        ]:
            eltwise_fn = get_eltwise_fn(eltwise)
            m = M(eltwise_fn, has_bias)
            x = torch.rand(32, 28, requires_grad=False)
            patterns = [
                ["aten::dequantize", "aten::linear", "aten::" + eltwise],
            ]
            for qconfig in static_qconfig:
                graph = self.checkQuantizeTrace(
                    m,
                    [x],
                    x_var=[torch.rand(2, 28, requires_grad=False)],
                    atol=1e-1,
                    qconfig=qconfig,
                )
                self.assertGraphContainsExactly(graph, LLGA_FUSION_GROUP, 1)
                self.assertFused(graph, ["aten::" + eltwise])
                self.checkPatterns(graph, patterns)

    def test_linear_silu(self):
        class M(nn.Module):
            def __init__(self, inplace):
                super(M, self).__init__()
                self.linear = nn.Linear(28, 64)
                self.eltwise = nn.SiLU(inplace=inplace)

            def forward(self, x):
                x = self.linear(x)
                x = self.eltwise(x)
                return x

        for inplace in [False, True]:
            m = M(inplace)
            x = torch.rand(1, 28, requires_grad=False)

            silu_op = "aten::silu_" if inplace else "aten::silu"

            patterns = [
                ["aten::dequantize", "aten::linear", "aten::sigmoid", "aten::mul"],
            ]
            graph = self.checkQuantizeTrace(m, [x], atol=1e-1)
            self.assertGraphContainsExactly(graph, LLGA_FUSION_GROUP, 1)
            self.assertFused(graph, ["aten::linear", silu_op, "aten::dequantize"])
            self.checkPatterns(graph, patterns)

    def test_conv_relu_sigmoid_mul(self):
        #        dequant
        #           |
        #         conv
        #           |
        #         relu
        #          /  |
        #       quant |
        #        /    |
        #     dequant |
        #       |     |
        #     conv    |
        #       |     |
        #     relu    |
        #       |     |
        #     quant   |
        #       |     |
        #    dequant  |
        #       |     |
        #     conv    |
        #       |     |
        #    sigmoid  |
        #         \   /
        #          mul

        class M(nn.Module):
            def __init__(self):
                super(M, self).__init__()
                self.conv1 = nn.Conv2d(32, 32, 3, padding=1)
                self.conv2 = nn.Conv2d(32, 32, 3, padding=1)
                self.conv3 = nn.Conv2d(32, 32, 3, padding=1)

            def forward(self, x):
                x = self.conv1(x)

                # The output y of relu is used by mul
                y = x.relu()

                z = self.conv2(y)
                z = z.relu()
                z = self.conv3(z)
                z = z.sigmoid()
                z = z.mul(y)
                return z

        x = torch.rand(1, 32, 16, 16, requires_grad=False)
        m = M()
        graph = self.checkQuantizeTrace(m, [x], atol=1e-1)
        patterns = [
            ["aten::dequantize", "aten::_convolution", "aten::relu"],
            [
                "aten::dequantize",
                "aten::_convolution",
                "aten::relu",
                "aten::quantize_per_tensor",
            ],
            ["aten::dequantize", "aten::_convolution", "aten::sigmoid", "aten::mul"],
        ]
        self.assertGraphContainsExactly(graph, LLGA_FUSION_GROUP, 3)
        self.assertFused(
            graph, ["aten::_convolution", "aten::relu", "aten::sigmoid", "aten::mul"]
        )
        self.checkPatterns(graph, patterns)

    def test_conv_eltwise_tensor_method(self):
        class ConvSigmoid(nn.Module):
            def __init__(self):
                super(ConvSigmoid, self).__init__()
                self.conv = nn.Conv2d(32, 32, 3, padding=1)

            def forward(self, x):
                x = self.conv(x)
                x = x.sigmoid()
                return x

        class ConvReLU(nn.Module):
            def __init__(self):
                super(ConvReLU, self).__init__()
                self.conv = nn.Conv2d(32, 32, 3, padding=1)

            def forward(self, x):
                x = self.conv(x)
                x = x.relu()
                return x

        m = ConvSigmoid().eval()
        x = torch.rand(1, 32, 16, 16, requires_grad=False)
        patterns = [["aten::dequantize", "aten::_convolution", "aten::sigmoid"]]
        graph = self.checkQuantizeTrace(m, [x], atol=1e-1)
        self.assertGraphContainsExactly(graph, LLGA_FUSION_GROUP, 1)
        self.assertFused(graph, ["aten::_convolution", "aten::sigmoid"])
        self.checkPatterns(graph, patterns)

        m = ConvReLU().eval()
        x = torch.rand(1, 32, 16, 16, requires_grad=False)
        patterns = [["aten::dequantize", "aten::_convolution", "aten::relu"]]
        graph = self.checkQuantizeTrace(m, [x], atol=1e-1)
        self.assertGraphContainsExactly(graph, LLGA_FUSION_GROUP, 1)
        self.assertFused(graph, ["aten::_convolution", "aten::relu"])
        self.checkPatterns(graph, patterns)

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
            for memory_format in [torch.contiguous_format, torch.channels_last]:
                m = M(bias).eval()
                x = torch.rand(1, 32, 16, 16, requires_grad=False).to(
                    memory_format=memory_format
                )
                y = torch.rand(1, 32, 16, 16, requires_grad=False).to(
                    memory_format=memory_format
                )
                patterns = [
                    [
                        "aten::dequantize",
                        "aten::_convolution",
                        "aten::quantize_per_tensor",
                    ],
                    [
                        "aten::dequantize",
                        "aten::_convolution",
                        "aten::relu",
                        "aten::add",
                        "aten::quantize_per_tensor",
                    ],
                    ["aten::dequantize", "aten::_convolution"],
                ]
                for qconfig in static_qconfig:
                    graph = self.checkQuantizeTrace(
                        m, [x, y], atol=1e-1, qconfig=qconfig
                    )
                    self.assertGraphContainsExactly(graph, LLGA_FUSION_GROUP, 3)
                    self.assertFused(
                        graph,
                        [
                            "aten::_convolution",
                            "aten::relu",
                            "aten::add",
                            "aten::quantize_per_channel",
                            "aten::dequantize",
                        ],
                    )
                    self.checkPatterns(graph, patterns)

    def test_add_quantization(self):
        class M(nn.Module):
            def __init__(self, bias=False):
                super(M, self).__init__()
                self.conv1 = nn.Conv2d(16, 16, 1)
                self.conv2 = nn.Conv2d(16, 16, 1)

            def forward(self, x):
                x = self.conv1(x)
                y = self.conv2(x)
                y = y.mul(10)
                z = torch.add(x, y)
                return z

        m = M().eval()
        x = torch.rand(1, 16, 16, 16, requires_grad=False)
        x2 = torch.rand(1, 16, 16, 16, requires_grad=False)

        patterns = [
            ["aten::dequantize", "aten::_convolution"],
            ["aten::dequantize", "aten::_convolution"],
        ]
        graph = self.checkQuantizeTrace(m, [x], atol=1e-1)
        self.assertGraphContainsExactly(graph, LLGA_FUSION_GROUP, 2)
        self.assertFused(graph, ["aten::_convolution", "aten::quantize_per_channel"])
        self.checkPatterns(graph, patterns)

    def test_conv2d_sigmoid_mul_(self):
        class M(nn.Module):
            def __init__(self, in_channels, out_channels, kernel_size, image_size):
                super(M, self).__init__()
                self.conv = torch.nn.Conv2d(
                    in_channels, out_channels, kernel_size, image_size
                )

            def forward(self, x):
                a = self.conv(x)
                b = torch.sigmoid(a)
                res = a.mul_(b)
                return res

        for memory_format in [torch.contiguous_format, torch.channels_last]:
            m = M(3, 16, 3, 224).eval()
            x = torch.rand(1, 3, 224, 224, requires_grad=False).to(
                memory_format=memory_format
            )
            patterns = [
                [
                    "aten::dequantize",
                    "aten::_convolution",
                    "aten::sigmoid",
                    "aten::mul",
                ],
            ]
            for qscheme in [torch.per_tensor_affine, torch.per_tensor_symmetric]:
                graph = self.checkQuantizeTrace(m, [x])
                self.assertGraphContainsExactly(graph, LLGA_FUSION_GROUP, 1)
                self.assertFused(
                    graph,
                    [
                        "aten::_convolution",
                        "aten::sigmoid",
                        "aten::mul",
                        "aten::quantize_per_channel",
                        "aten::dequantize",
                    ],
                )
                self.checkPatterns(graph, patterns)

        # inplace mul_ cannot be replaced with mul
        class M2(nn.Module):
            def __init__(self, in_channels, out_channels, kernel_size, image_size):
                super(M2, self).__init__()
                self.conv = torch.nn.Conv2d(
                    in_channels, out_channels, kernel_size, image_size
                )

            def forward(self, x):
                a = self.conv(x)
                b = torch.sigmoid(a)
                c = a[0]
                res = a.mul_(b)
                c += 2
                return c

        for memory_format in [torch.contiguous_format, torch.channels_last]:
            m = M2(3, 16, 3, 224).eval()
            x = torch.rand(1, 3, 224, 224, requires_grad=False).to(
                memory_format=memory_format
            )
            patterns = [
                ["aten::dequantize", "aten::_convolution"],
            ]
            for qscheme in [torch.per_tensor_affine, torch.per_tensor_symmetric]:
                graph = self.checkQuantizeTrace(m, [x])
                self.assertGraphContainsExactly(graph, LLGA_FUSION_GROUP, 1)
                self.assertFused(
                    graph,
                    [
                        "aten::_convolution",
                        "aten::quantize_per_channel",
                        "aten::dequantize",
                    ],
                )
                self.checkPatterns(graph, patterns)

    def test_conv2d_hardsigmoid_mul_(self):
        class M(nn.Module):
            def __init__(self, in_channels, out_channels, kernel_size, image_size):
                super(M, self).__init__()
                self.conv = torch.nn.Conv2d(
                    in_channels, out_channels, kernel_size, image_size
                )
                self.activation = torch.nn.Hardsigmoid()

            def forward(self, x):
                a = self.conv(x)
                b = self.activation(a)
                res = a.mul_(b)
                return res

        for memory_format in [torch.contiguous_format, torch.channels_last]:
            m = M(3, 16, 3, 224).eval()
            x = torch.rand(1, 3, 224, 224, requires_grad=False).to(
                memory_format=memory_format
            )
            patterns = [
                [
                    "aten::dequantize",
                    "aten::_convolution",
                    "aten::hardsigmoid",
                    "aten::mul",
                ],
            ]
            for qscheme in [torch.per_tensor_affine, torch.per_tensor_symmetric]:
                graph = self.checkQuantizeTrace(m, [x])
                self.assertGraphContainsExactly(graph, LLGA_FUSION_GROUP, 1)
                self.assertFused(
                    graph,
                    [
                        "aten::_convolution",
                        "aten::hardsigmoid",
                        "aten::mul",
                        "aten::quantize_per_channel",
                        "aten::dequantize",
                    ],
                )
                self.checkPatterns(graph, patterns)

    def test_linear_dropout_sum(self):
        class M(nn.Module):
            def __init__(self):
                super(M, self).__init__()
                self.linear1 = nn.Linear(15, 20)
                self.dropout = nn.Dropout()
                self.linear2 = nn.Linear(20, 3)

            def forward(self, x, y):
                x = self.linear1(x)
                x = self.dropout(x)
                z = self.linear2(x + y)
                return z

        x = torch.randn(2, 15)
        y = torch.randn(2, 20)
        m = M()
        patterns = [
            [
                "aten::dequantize",
                "aten::linear",
                "aten::add",
                "aten::quantize_per_tensor",
            ],
            ["aten::dequantize", "aten::linear"],
        ]
        for qconfig in static_qconfig:
            graph = self.checkQuantizeTrace(m, [x, y], atol=2e-1, qconfig=qconfig)
            self.assertGraphContainsExactly(graph, LLGA_FUSION_GROUP, 2)
            self.assertFused(
                graph,
                [
                    "aten::linear",
                    "aten::add",
                    "aten::quantize_per_channel",
                    "aten::dequantize",
                ],
            )
        self.checkPatterns(graph, patterns)

    def test_linear_sum_inplace(self):
        class M(nn.Module):
            def __init__(self):
                super(M, self).__init__()
                self.linear1 = nn.Linear(15, 20)

            def forward(self, x, y):
                x = self.linear1(x)
                x += y.clone()
                return x

        x = torch.randn(2, 15)
        y = torch.randn(2, 20)
        m = M()
        patterns = [
            ["aten::dequantize", "aten::linear", "aten::dequantize"],
        ]
        # HistogramObserver failed, need to do some checks?
        for qconfig in static_qconfig[:2]:
            graph = self.checkQuantizeTrace(m, [x, y], atol=2e-1, qconfig=qconfig)
            self.assertGraphContainsExactly(graph, LLGA_FUSION_GROUP, 1)
            self.assertFused(
                graph,
                ["aten::linear", "aten::quantize_per_channel", "aten::dequantize"],
            )
            self.checkPatterns(graph, patterns)

    def test_linear_dropout_sum_bf16(self):
        class M(nn.Module):
            def __init__(self):
                super(M, self).__init__()
                self.linear1 = nn.Linear(15, 20, bias=True)
                self.dropout = nn.Dropout()
                self.linear2 = nn.Linear(15, 20, bias=True)

            def forward(self, x, y):
                x = self.linear1(x)
                x = self.dropout(x)
                z = self.linear2(y) + x
                return z

        x = torch.randn(2, 15)
        y = torch.randn(2, 15)
        m = M()
        patterns = [
            [
                "aten::dequantize",
                "aten::to",
                "aten::linear",
                "aten::to",
                "aten::quantize_per_tensor",
            ],
            ["aten::dequantize", "aten::to", "aten::linear", "aten::add"],
        ]
        graph = self.checkQuantizeTrace(m, [x, y], atol=2e-1, int8_bf16=True)
        self.assertGraphContainsExactly(graph, LLGA_FUSION_GROUP, 2)
        # TODO: oneDNN primitive raised more limitations to sum post-ops, it forced fusion changes on oneDNN graph side.
        # The dequant node connected to aten::add can't be fused into the INT8 linear-add partition any more.
        # oneDNN graph expects no end to end model performance impact.
        # Revisit this change if validation has found model level regression.
        self.assertFused(graph, ["aten::linear", "aten::add"])
        self.checkPatterns(graph, patterns)

    def test_linear_gelu_bf16(self):
        class M(nn.Module):
            def __init__(self):
                super(M, self).__init__()
                self.linear = nn.Linear(28, 64, bias=True)
                self.eltwise = nn.GELU()
                self.linear2 = nn.Linear(64, 1, bias=True)

            def forward(self, x):
                x = self.linear(x)
                x = self.eltwise(x)
                x = self.linear2(x)
                return x

        patterns = [
            [
                "aten::dequantize",
                "aten::to",
                "aten::linear",
                "aten::gelu",
                "aten::to",
                "aten::quantize_per_tensor",
            ],
            ["aten::dequantize", "aten::to", "aten::linear"],
        ]
        m = M()
        x = torch.rand(32, 28, requires_grad=False)
        for qscheme in [torch.per_tensor_affine]:
            graph = self.checkQuantizeTrace(
                m,
                [x],
                x_var=[torch.rand(2, 28, requires_grad=False)],
                atol=1e-1,
                int8_bf16=True,
            )
            self.assertGraphContainsExactly(graph, LLGA_FUSION_GROUP, 2)
            self.assertFused(graph, ["aten::dequantize", "aten::linear", "aten::gelu"])
            self.checkPatterns(graph, patterns)

    def test_defer_size(self):
        class M(nn.Module):
            def __init__(self):
                super(M, self).__init__()
                self.conv1 = nn.Conv2d(32, 32, 3, padding=1, bias=True)
                self.conv2 = nn.Conv2d(32, 32, 3, padding=1, bias=True)
                self.eltwise = nn.ReLU()

            def forward(self, x):
                x = self.conv1(x)
                x = self.eltwise(x)
                y = self.conv2(x)
                y = y.reshape(x.size(0), -1)
                return y

        for memory_format in [torch.contiguous_format, torch.channels_last]:
            m = M()
            x = torch.rand(1, 32, 28, 28).to(memory_format=memory_format)
            patterns = [
                [
                    "aten::dequantize",
                    "aten::_convolution",
                    "aten::relu",
                    "aten::quantize_per_tensor",
                ],
                ["aten::dequantize", "aten::_convolution"],
            ]
            for qconfig in static_qconfig:
                graph = self.checkQuantizeTrace(m, [x], atol=2e-1, qconfig=qconfig)
                self.assertGraphContainsExactly(graph, LLGA_FUSION_GROUP, 2)
                self.assertFused(
                    graph,
                    [
                        "aten::_convolution",
                        "aten::relu",
                        "aten::quantize_per_channel",
                        "aten::dequantize",
                    ],
                )
                self.checkPatterns(graph, patterns)

    def test_lift_up_quant(self):
        class M(nn.Module):
            def __init__(self, bias):
                super(M, self).__init__()
                self.linear = nn.Linear(28, 64, bias)
                self.linear2 = nn.Linear(28, 64, bias=True)
                self.num_attention_heads = 16
                self.attention_head_size = 4

            def forward(self, x, y):
                x = self.linear(x)
                new_x_shape = x.size()[:-1] + (
                    self.num_attention_heads,
                    self.attention_head_size,
                )
                x = x.view(*new_x_shape)
                z1 = x.permute(0, 2, 1, 3)

                y = self.linear2(y)
                new_y_shape2 = y.size()[:-1] + (
                    self.num_attention_heads,
                    self.attention_head_size,
                )
                y = y.view(*new_y_shape2)
                z2 = y.permute(0, 2, 1, 3)

                return torch.matmul(z1, z2.transpose(-1, -2))

        m = M(bias=True)
        x = torch.randn(2, 3, 28)
        y = torch.randn(2, 3, 28)

        patterns = [
            ["aten::dequantize", "aten::linear", "aten::quantize_per_tensor"],
            ["aten::dequantize", "aten::linear", "aten::quantize_per_tensor"],
            ["aten::dequantize", "aten::matmul"],
        ]

        # TODO: test shape fallback
        graph = self.checkQuantizeTrace(m, [x, y], atol=1e-1)
        self.assertGraphContainsExactly(graph, LLGA_FUSION_GROUP, 3)
        self.assertFused(graph, ["aten::dequantize", "aten::linear", "aten::matmul"])
        self.checkPatterns(graph, patterns)

    def test_lift_up_to_quant_bf16(self):
        class M(nn.Module):
            def __init__(self, bias):
                super(M, self).__init__()
                self.linear = nn.Linear(28, 64, bias)
                self.linear2 = nn.Linear(28, 64, bias=True)
                self.num_attention_heads = 16
                self.attention_head_size = 4

            def forward(self, x, y):
                x = self.linear(x)
                new_x_shape = x.size()[:-1] + (
                    self.num_attention_heads,
                    self.attention_head_size,
                )
                x = x.view(*new_x_shape)
                z1 = x.permute(0, 2, 1, 3)

                y = self.linear2(y)
                new_y_shape2 = y.size()[:-1] + (
                    self.num_attention_heads,
                    self.attention_head_size,
                )
                y = y.view(*new_y_shape2)
                z2 = y.permute(0, 2, 1, 3)

                return torch.matmul(z1, z2.transpose(-1, -2))

        m = M(bias=True)
        x = torch.randn(2, 3, 28)
        y = torch.randn(2, 3, 28)

        patterns = [
            [
                "aten::dequantize",
                "aten::to",
                "aten::linear",
                "aten::to",
                "aten::quantize_per_tensor",
            ],
            [
                "aten::dequantize",
                "aten::to",
                "aten::linear",
                "aten::to",
                "aten::quantize_per_tensor",
            ],
            ["aten::dequantize", "aten::to", "aten::matmul"],
        ]

        # TODO: test shape fallback
        graph = self.checkQuantizeTrace(m, [x, y], atol=1e-1, int8_bf16=True)
        self.assertGraphContainsExactly(graph, LLGA_FUSION_GROUP, 3)
        self.assertFused(graph, ["aten::dequantize", "aten::linear", "aten::matmul"])
        self.checkPatterns(graph, patterns)

    def test_lift_up_quant_unsupported(self):
        # Original graph:
        #          |
        #        view
        #      /  (f32)\   /(f32)
        #   quant       add
        #     |

        # Lifting up in this case will raise:
        # promoteTypes with quantized numbers is not handled in aten::add;
        #          |
        #        quant
        #          |
        #         view
        #         (int8)\  /(f32)
        #                add
        class M(nn.Module):
            def __init__(self):
                super(M, self).__init__()
                self.conv1 = nn.Conv2d(3, 8, 1)
                self.conv2 = nn.Conv2d(8, 8, 1)

            def forward(self, x, y):
                x = self.conv1(x)
                z1 = x.permute(0, 3, 1, 2)
                z2 = self.conv2(z1)
                z = z1 + y
                output = z2 + z
                return output

        x = torch.randn(1, 3, 8, 8)
        y = torch.randn(1, 8, 8, 8)
        m = M()

        patterns = [
            ["aten::dequantize", "aten::_convolution"],
            ["aten::dequantize", "aten::_convolution", "aten::add"],
        ]

        graph = self.checkQuantizeTrace(m, [x, y], atol=2e-1)
        self.assertGraphContainsExactly(graph, LLGA_FUSION_GROUP, 2)
        # TODO: oneDNN primitive raised more limitations to sum post-ops, it forced fusion changes on oneDNN graph side.
        # The dequant node connected to aten::add can't be fused into the INT8 conv-add partition any more.
        # oneDNN graph expects no end to end model performance impact.
        # Revisit this change if validation has found model level regression.
        self.assertFused(graph, ["aten::_convolution"])
        self.checkPatterns(graph, patterns)

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
        patterns = [
            ["aten::dequantize", "aten::_convolution"],
        ]
        graph = self.checkQuantizeTrace(m, [x], atol=2e-1)
        self.assertGraphContainsExactly(graph, LLGA_FUSION_GROUP, 1)
        self.assertGraphContainsExactly(graph, "aten::relu", 1)
        self.assertFused(graph, ["aten::_convolution", "aten::quantize_per_channel"])
        self.checkPatterns(graph, patterns)

    def test_bmm_div_scalar(self):
        class M(nn.Module):
            def __init__(self, div_value):
                super(M, self).__init__()
                self.div_value = div_value

            def forward(self, x, y):
                mm_res = torch.matmul(x, y)
                return mm_res.div(self.div_value)

        x = torch.randn(1, 16, 384, 64)
        y = torch.randn(1, 1, 64, 384)
        patterns = [
            ["aten::dequantize", "aten::matmul", "aten::div"],
        ]
        m = M(8.0)
        graph = self.checkQuantizeTrace(m, [x, y], atol=2e-1)
        self.assertGraphContainsExactly(graph, LLGA_FUSION_GROUP, 1)
        self.assertFused(graph, ["aten::matmul", "aten::div"])
        self.checkPatterns(graph, patterns)

    def test_bmm_div_identity(self):
        class M(nn.Module):
            def __init__(self, div_value):
                super(M, self).__init__()
                self.div_value = div_value

            def forward(self, x, y):
                mm_res = torch.matmul(x, y)
                return mm_res.div(self.div_value)

        x = torch.randn(1, 16, 384, 64) * 0.1
        y = torch.randn(1, 1, 64, 384) * 0.1
        patterns = [
            ["aten::dequantize", "aten::matmul"],
        ]
        m = M(1.0)
        graph = self.checkQuantizeTrace(m, [x, y], atol=2e-1)
        # divide by 1 should be removed by Constant Propagation
        self.assertGraphContainsExactly(graph, "aten::div", 0, consider_subgraphs=True)
        self.assertGraphContainsExactly(graph, LLGA_FUSION_GROUP, 1)
        self.assertFused(graph, ["aten::matmul"])
        self.checkPatterns(graph, patterns)

    def test_bmm_div_tensor(self):
        class M(nn.Module):
            def __init__(self):
                super(M, self).__init__()

            def forward(self, x, y, z):
                mm_res = torch.matmul(x, y)
                return mm_res.div(z)

        x = torch.randn(1, 16, 384, 64) * 0.1
        y = torch.randn(1, 1, 64, 384) * 0.1
        z = torch.randn(
            1
        )  # TODO: enable torch.randn(20) and torch.randn(1, 1, 20, 20) once backend supported them
        patterns = [
            ["aten::dequantize", "aten::matmul", "aten::div"],
        ]
        m = M()
        graph = self.checkQuantizeTrace(m, [x, y, z], atol=2e-1)
        self.assertGraphContainsExactly(graph, LLGA_FUSION_GROUP, 1)
        self.assertFused(graph, ["aten::matmul", "aten::div"])
        self.checkPatterns(graph, patterns)

    def test_bmm_div_int8_in_bf16_out(self):
        class M(nn.Module):
            def __init__(self):
                super(M, self).__init__()

            def forward(self, x, y):
                mm_res = torch.matmul(x, y) / 2
                return mm_res

        x = torch.randn(1, 16, 384, 64) * 0.1
        y = torch.randn(1, 1, 64, 384) * 0.1
        patterns = [
            ["aten::dequantize", "aten::to", "aten::matmul", "aten::div"],
        ]
        m = M()
        graph = self.checkQuantizeTrace(m, [x, y], atol=2e-1, int8_bf16=True)
        self.assertGraphContainsExactly(graph, LLGA_FUSION_GROUP, 1)
        # single aten::to won't be rewritten by llga backend
        self.assertFused(graph, ["aten::dequantize", "aten::matmul", "aten::div"])
        self.checkPatterns(graph, patterns)

    def test_bmm_method_bf16(self):
        class M(nn.Module):
            def __init__(self):
                super(M, self).__init__()

            def forward(self, x, y):
                mm_res = x.matmul(y)
                return mm_res

        x = torch.randn(1, 16, 384, 64) * 0.1
        y = torch.randn(1, 1, 64, 384) * 0.1
        patterns = [
            ["aten::dequantize", "aten::to", "aten::matmul"],
        ]
        m = M()
        graph = self.checkQuantizeTrace(m, [x, y], atol=2e-1, int8_bf16=True)
        self.assertGraphContainsExactly(graph, LLGA_FUSION_GROUP, 1)
        # single aten::to won't be rewritten by llga backend
        self.assertFused(graph, ["aten::dequantize", "aten::matmul"])
        self.checkPatterns(graph, patterns)

    def test_bmm_method_fp32(self):
        class M(nn.Module):
            def __init__(self):
                super(M, self).__init__()

            def forward(self, x, y):
                mm_res = x.matmul(y)
                return mm_res

        x = torch.randn(1, 16, 384, 64) * 0.1
        y = torch.randn(1, 1, 64, 384) * 0.1
        patterns = [
            ["aten::dequantize", "aten::matmul"],
        ]
        m = M()
        graph = self.checkQuantizeTrace(m, [x, y], atol=2e-1)
        self.assertGraphContainsExactly(graph, LLGA_FUSION_GROUP, 1)
        self.assertFused(graph, ["aten::dequantize", "aten::matmul"])
        self.checkPatterns(graph, patterns)

    def test_strided_bmm_div_int8_in_bf16_out(self):
        class M(nn.Module):
            def __init__(self):
                super(M, self).__init__()
                self.num_attention_heads = 16
                self.attention_head_size = 4

            def forward(self, x, y):
                new_x_shape = x.size()[:-1] + (
                    self.num_attention_heads,
                    self.attention_head_size,
                )
                x = x.view(*new_x_shape)
                z1 = x.permute(0, 2, 1, 3)

                new_y_shape2 = y.size()[:-1] + (
                    self.num_attention_heads,
                    self.attention_head_size,
                )
                y = y.view(*new_y_shape2)
                z2 = y.permute(0, 2, 1, 3)

                # inputs to matmul has been permuted or transposed, thus are strided tensor
                return torch.matmul(z1, z2.transpose(-1, -2)) / 0.4

        m = M()
        x = torch.randn(2, 3, 64)
        y = torch.randn(2, 3, 64)

        patterns = [
            ["aten::dequantize", "aten::to", "aten::matmul", "aten::div"],
        ]

        graph = self.checkQuantizeTrace(m, [x, y], atol=2e-1, int8_bf16=True)
        self.assertGraphContainsExactly(graph, LLGA_FUSION_GROUP, 1)
        self.assertFused(graph, ["aten::matmul", "aten::dequantize"])
        self.checkPatterns(graph, patterns)

    def test_bmm_div_add_int8_fp32(self):
        class M(nn.Module):
            def __init__(self):
                super(M, self).__init__()
                self.num_attention_heads = 16
                self.attention_head_size = 4

            def forward(self, x, y, z):
                new_x_shape = x.size()[:-1] + (
                    self.num_attention_heads,
                    self.attention_head_size,
                )
                x = x.view(*new_x_shape)
                z1 = x.permute(0, 2, 1, 3)

                new_y_shape2 = y.size()[:-1] + (
                    self.num_attention_heads,
                    self.attention_head_size,
                )
                y = y.view(*new_y_shape2)
                z2 = y.permute(0, 2, 1, 3)

                # inputs to matmul has been permuted or transposed, thus are strided tensor
                s = torch.matmul(z1, z2.transpose(-1, -2)) / 0.4
                s = s + z
                return s

        m = M()
        x = torch.randn(2, 3, 64)
        y = torch.randn(2, 3, 64)
        z = torch.randn(2, 1, 1, 3)

        patterns = [
            ["aten::dequantize", "aten::matmul", "aten::div", "aten::add"],
        ]

        graph = self.checkQuantizeTrace(m, [x, y, z], atol=2e-1)
        self.assertGraphContainsExactly(graph, LLGA_FUSION_GROUP, 1)
        self.assertFused(
            graph, ["aten::matmul", "aten::dequantize", "aten::div", "aten::add"]
        )
        self.checkPatterns(graph, patterns)

    @unittest.skip("Graph Compiler unit-test")
    def test_mha_pattern_int8_fp32(self):
        class M(torch.nn.Module):
            def __init__(self):
                super(M, self).__init__()
                self.linear = nn.Linear(1024, 1024, False)

            def forward(self, x, y, z, a):
                x = x.permute(0, 2, 1, 3)

                y = y.permute(0, 2, 1, 3)
                y = y.transpose(-1, -2)

                z = z.permute(0, 2, 1, 3)
                tmp = torch.matmul(x, y) / 8.0 + a
                tmp = torch.softmax(tmp, -1)
                tmp = tmp.matmul(z)
                tmp = tmp.permute(0, 2, 1, 3)
                tmp = tmp.contiguous()
                tmp = tmp.view(1, 16, 1024)
                tmp = self.linear(tmp)
                return tmp

        x = torch.randn(1, 16, 16, 64)
        y = torch.randn(1, 16, 16, 64)
        z = torch.randn(1, 16, 16, 64)
        m = M()
        a = torch.randn(1, 1, 1, 16)
        graph = self.checkQuantizeTrace(m, [x, y, z, a], atol=2e-1)
        self.assertGraphContainsExactly(graph, LLGA_FUSION_GROUP, 2)
        self.assertFused(
            graph,
            [
                "aten::matmul",
                "aten::div",
                "aten:add",
                "aten:softmax",
                "aten::contiguous",
                "aten::dequantize",
            ],
        )

    @unittest.skip("Graph Compiler unit-test")
    def test_mha_pattern_int8_bf16(self):
        class M(torch.nn.Module):
            def __init__(self):
                super(M, self).__init__()
                self.linear = nn.Linear(1024, 1024, False)

            def forward(self, x, y, z, a):
                x = x.permute(0, 2, 1, 3)

                y = y.permute(0, 2, 1, 3)
                y = y.transpose(-1, -2)

                z = z.permute(0, 2, 1, 3)
                tmp = torch.matmul(x, y) / 8.0 + a
                tmp = torch.softmax(tmp, -1)
                tmp = tmp.matmul(z)
                tmp = tmp.permute(0, 2, 1, 3)
                tmp = tmp.contiguous()
                tmp = tmp.view(1, 16, 1024)
                tmp = self.linear(tmp)
                return tmp

        x = torch.randn(1, 16, 16, 64)
        y = torch.randn(1, 16, 16, 64)
        z = torch.randn(1, 16, 16, 64)
        m = M()
        a = torch.randn(1, 1, 1, 16, dtype=torch.bfloat16)
        graph = self.checkQuantizeTrace(
            m,
            [x, y, z, a],
            atol=2e-1,
            config_name="mha_pattern",
            qscheme=torch.per_tensor_affine,
            int8_bf16=True,
        )
        self.assertGraphContainsExactly(graph, LLGA_FUSION_GROUP, 5)
        self.assertFused(
            graph,
            [
                "aten::matmul",
                "aten::div",
                "aten:add",
                "aten:softmax",
                "aten::contiguous",
                "aten::dequantize",
                "aten::quantize_per_tensor",
            ],
        )

    def test_bmm_div_add_int8_bf16(self):
        class M(nn.Module):
            def __init__(self):
                super(M, self).__init__()
                self.num_attention_heads = 16
                self.attention_head_size = 4

            def forward(self, x, y, z):
                new_x_shape = x.size()[:-1] + (
                    self.num_attention_heads,
                    self.attention_head_size,
                )
                x = x.view(*new_x_shape)
                z1 = x.permute(0, 2, 1, 3)

                new_y_shape2 = y.size()[:-1] + (
                    self.num_attention_heads,
                    self.attention_head_size,
                )
                y = y.view(*new_y_shape2)
                z2 = y.permute(0, 2, 1, 3)

                # inputs to matmul has been permuted or transposed, thus are strided tensor
                s = torch.matmul(z1, z2.transpose(-1, -2)) / 0.4
                s = s + z.to(s.dtype)
                return s

        m = M()
        x = torch.randn(2, 3, 64)
        y = torch.randn(2, 3, 64)
        z = torch.randn(2, 1, 1, 3)

        patterns = [
            ["aten::dequantize", "aten::to", "aten::matmul", "aten::div", "aten::add"],
        ]

        graph = self.checkQuantizeTrace(m, [x, y, z], atol=2e-1, int8_bf16=True)
        self.assertGraphContainsExactly(graph, LLGA_FUSION_GROUP, 1)
        self.assertFused(
            graph, ["aten::matmul", "aten::dequantize", "aten::div", "aten::add"]
        )
        self.checkPatterns(graph, patterns)

    def test_split_dequant_to(self):
        class M(nn.Module):
            def __init__(self):
                super(M, self).__init__()
                self.linear1 = nn.Linear(2, 1, bias=True)
                self.linear2 = nn.Linear(2, 1, bias=True)
                self.linear3 = nn.Linear(2, 1, bias=True)

            def forward(self, x):
                a = self.linear1(x)
                b = self.linear2(x)
                c = self.linear3(x)
                return torch.cat([a, b, c])

        # The below pattern:
        #         quant
        #           |
        #        dequant
        #           |
        #          to
        #     /    |    \
        # linear linear linear
        #    |     |      |
        #
        # should be transformed to:
        #               to
        #               |
        #             quant
        #        /      |     \
        #   dequant dequant  dequant
        #      |       |       |
        #     to       to     to
        #      |       |       |
        #  linear   linear  linear
        #      |       |       |

        patterns = [
            ["aten::dequantize", "aten::to", "aten::linear"],
            ["aten::dequantize", "aten::to", "aten::linear"],
            ["aten::dequantize", "aten::to", "aten::linear"],
        ]
        m = M()
        x = torch.randn(2, 2)
        graph = self.checkQuantizeTrace(m, [x], atol=2e-1, int8_bf16=True)
        self.assertGraphContainsExactly(graph, LLGA_FUSION_GROUP, 3)
        # single aten::to won't be rewritten by llga backend
        self.assertFused(graph, ["aten::dequantize", "aten::linear"])
        self.checkPatterns(graph, patterns)

    def test_dequant_remove_attr(self):
        class M(nn.Module):
            def __init__(self):
                super(M, self).__init__()

            def forward(self, x):
                x = torch.quantize_per_channel(
                    x, torch.tensor([0.1, 0.01]), torch.tensor([10, 0]), 0, torch.quint8
                )
                x = torch.dequantize(x)
                return x

        x = x = torch.tensor([[-1.0, 0.0], [1.0, 2.0]])
        m = M()
        traced = torch.jit.trace(m, x)
        traced(x)
        graph = traced.graph_for(x)
        self.checkAttr(graph, "aten::dequantize", "qtype")

    def test_fx_converted_model(self):
        class M(nn.Module):
            def __init__(self):
                super(M, self).__init__()
                self.linear = nn.Linear(15, 20)

            def forward(self, x):
                x = self.linear(x)
                return x

        x = x = torch.randn(2, 15)
        m = M()
        m.eval()

        qconfig_dict = {"": static_qconfig[0]}

        m = prepare_fx(m, qconfig_dict, x)
        m = convert_fx(m)
        graph = self.checkQuantizeTrace(m, [x], atol=2e-1)
        self.assertGraphContainsExactly(graph, LLGA_FUSION_GROUP, 0)

    def test_fx_ao_qat_converted_model(self):
        class M(nn.Module):
            def __init__(self):
                super(M, self).__init__()
                self.linear = nn.Linear(15, 20)

            def forward(self, x):
                x = self.linear(x)
                return x

        x = x = torch.randn(2, 15)
        m = M()
        m.eval()

        qconfig_dict = {"": static_qconfig[0]}

        m = prepare_qat_fx(m, qconfig_dict, x)
        m = convert_to_reference_fx(m)
        graph = self.checkQuantizeTrace(m, [x], atol=2e-1)
        # dequant -> linear should be mapped to LLGA
        self.assertGraphContainsExactly(graph, LLGA_FUSION_GROUP, 1)

    @unittest.skipIf(True, "Poor accuracy")
    @skipIfNoTorchVision
    def test_fx_ao_qat_model(self):
        class M(nn.Module):
            def __init__(self):
                super(M, self).__init__()
                self.conv1 = nn.Conv2d(32, 32, 3, padding=1, bias=True)
                self.conv2 = nn.Conv2d(32, 32, 3, padding=1, bias=True)
                self.eltwise = torch.nn.ReLU()

            def forward(self, x):
                x = self.conv1(x)
                x = self.eltwise(x)
                x = self.conv2(x)
                return x

        data = torch.randn(1, 32, 224, 224).to(memory_format=torch.channels_last)
        m = M()
        m.eval()
        #
        # quantization aware training for static quantization
        #
        qconfig_dict = {"": torch.quantization.get_default_qat_qconfig("fbgemm")}
        m.train()
        model_prepared = prepare_qat_fx(m, qconfig_dict, example_inputs=data)
        model_quantized = convert_to_reference_fx(model_prepared)
        model_quantized = model_quantized.eval()
        model = model_quantized.to(memory_format=torch.channels_last)
        graph = self.checkQuantizeTrace(model, [data], atol=2e-1)
        self.checkPatterns(
            graph,
            [
                [
                    "aten::dequantize",
                    "aten::quantize_per_channel",
                    "aten::_convolution",
                    "aten::relu",
                    "aten::quantize_per_tensor",
                ],
                [
                    "aten::dequantize",
                    "aten::quantize_per_channel",
                    "aten::_convolution",
                    "aten::quantize_per_tensor",
                ],
            ],
        )

    def test_ffn_residual(self):
        class FFN_Residual(nn.Module):
            def __init__(self, hidden_size, intermediate_size):
                super(FFN_Residual, self).__init__()
                self.linear1 = nn.Linear(hidden_size, intermediate_size)
                self.linear2 = nn.Linear(intermediate_size, hidden_size)
                self.LayerNorm1 = nn.LayerNorm(hidden_size)
                self.LayerNorm2 = nn.LayerNorm(hidden_size)
                self.intermediate_act_fn = nn.functional.gelu

            def forward(self, x):
                x1 = self.LayerNorm1(x)
                x2 = self.linear1(x1)
                x3 = self.intermediate_act_fn(x2)
                x4 = self.linear2(x3)
                x5 = self.LayerNorm2(x4 + x)
                return x5

        patterns = [
            [
                "aten::dequantize",
                "aten::linear",
                "aten::gelu",
                "aten::quantize_per_tensor",
            ],
            ["aten::dequantize", "aten::linear", "aten::add"],
        ]
        m = FFN_Residual(1024, 4096).eval()
        x = torch.rand(128, 1024)
        graph = self.checkQuantizeTrace(m, [x], atol=2e-1)
        self.assertGraphContainsExactly(graph, LLGA_FUSION_GROUP, 2)
        self.assertFused(graph, ["aten::linear", "aten::gelu"])
        self.assertFused(graph, ["aten::linear", "aten::add"])
        self.checkPatterns(graph, patterns)


class TestShapeFallback(JitLlgaTestCase):
    @unittest.skipIf(True, "Size peephole optimization not enabled yet")
    def test_view_permute(self):
        class M(nn.Module):
            def __init__(self):
                super(M, self).__init__()

            def forward(self, x):
                new_x_shape = x.size()[:-1] + (3, 5)
                x = x.view(*new_x_shape)
                return x.permute(0, 2, 1, 3)

        x = torch.randn(5, 10, 15)
        m = M()

        for qconfig in static_qconfig:
            graph = self.checkQuantizeTrace(m, [x], qconfig=qconfig)
            self.assertGraphContainsExactly(graph, "aten::size", 0)
            self.assertGraphContainsExactly(graph, "prim::ListConstruct", 0)

            # change the size of the input
            x2 = torch.randn(6, 4, 15)
            # Bailout get triggered here
            y2 = m(x2)

    def test_conv_reshape(self):
        class M(nn.Module):
            def __init__(self):
                super(M, self).__init__()
                self.conv1 = nn.Conv2d(4, 4, 3, padding=1, bias=True)
                self.conv2 = nn.Conv2d(4, 32, 3, padding=1, bias=True)

            def forward(self, x):
                x = self.conv1(x)
                x = self.conv2(x).reshape(x.size(0), 4, -1)
                return x

        for memory_format in [torch.contiguous_format, torch.channels_last]:
            x = torch.randn(15, 4, 28, 28).to(memory_format=memory_format)
            # change the size of the input, check the fallback
            x_var = torch.randn(7, 4, 16, 16).to(memory_format=memory_format)
            m = M()
            for qconfig in static_qconfig:
                graph = self.checkQuantizeTrace(
                    m, [x], x_var=[x_var], atol=2e-1, qconfig=qconfig
                )

                # TODO: enable this check when size peephole optimization is enabled
                # self.assertGraphContainsExactly(graph, "aten::size", 0)

    def test_add_recipe(self):
        class ConvAddRelu(nn.Module):
            def __init__(self, in_channels, out_channels, kernel_size, image_size):
                super(ConvAddRelu, self).__init__()
                self.conv = torch.nn.Conv2d(
                    in_channels, out_channels, kernel_size, image_size
                )

            def forward(self, x1, x2):
                return torch.relu(torch.add(self.conv(x1), x2))

        class ConvAdd(nn.Module):
            def __init__(self, in_channels, out_channels, kernel_size, image_size):
                super(ConvAdd, self).__init__()
                self.conv = torch.nn.Conv2d(
                    in_channels, out_channels, kernel_size, image_size
                )

            def forward(self, x1, x2):
                return torch.add(self.conv(x1), x2)

        for memory_format in [torch.contiguous_format, torch.channels_last]:
            conv_add_relu = ConvAddRelu(3, 16, 3, 2)
            conv_add = ConvAdd(3, 16, 3, 2)
            x1 = torch.rand(1, 3, 224, 224, requires_grad=False).to(
                memory_format=memory_format
            )
            x2 = torch.rand(1, 16, 111, 111, requires_grad=False).to(
                memory_format=memory_format
            )
            input = [x1, x2]
            graph1 = self.checkQuantizeTrace(conv_add_relu, input, atol=1e-2)
            self.assertGraphContainsExactly(graph1, "aten::quantize_per_tensor", 2)
            graph2 = self.checkQuantizeTrace(conv_add, input, atol=1e-2)
            self.assertGraphContainsExactly(graph2, "aten::quantize_per_tensor", 1)


class TestModel(JitLlgaTestCase):
    @skipIfNoTorchVision
    def _test_vision(self, model_name):
        for memory_format in [torch.contiguous_format, torch.channels_last]:
            m = getattr(torchvision.models, model_name)().eval()
            x = (torch.rand(1, 3, 224, 224) / 10).to(memory_format=memory_format)

            for qconfig in static_qconfig:
                graph = self.checkQuantizeTrace(m, [x], atol=2e-1, qconfig=qconfig)

                # TODO: aten::adaptive_avg_pool2d also need to be fused once backend supported it
                self.assertFused(
                    graph,
                    [
                        "aten::_convolution",
                        "aten::relu",
                        "aten::max_pool2d",
                        "aten::linear",
                        "aten::quantize_per_channel",
                    ],
                )
                # large partition: 7 fusion group in total
                self.assertGraphContainsExactly(graph, LLGA_FUSION_GROUP, 7)


for model_name, enabled in [
    ["resnet50", True],
]:

    def wrapper(mname):
        @unittest.skipIf(not enabled, "Disabled")
        def test(self):
            return self._test_vision(mname)

        return test

    setattr(TestModel, "test_vision_%s" % model_name, wrapper(model_name))

if __name__ == "__main__":
    run_tests()
