#pragma once

#include <ATen/Tensor.h>
#include <torch/csrc/autograd/custom_function.h>

#include <ideep.hpp>
#include "cpu/kernels/OpContext.h"

namespace torch_ipex {
namespace cpu {

void convolution_kernel_output(
    const at::Tensor& input,
    const ideep::tensor& mkldnn_weight,
    const ideep::tensor& bias_opt,
    at::Tensor& output,
    at::IntArrayRef stride,
    at::IntArrayRef padding,
    at::IntArrayRef dilation,
    int64_t groups,
    const ideep::attr_t& attr);

at::Tensor convolution_kernel(
    const at::Tensor& input,
    const ideep::tensor& mkldnn_weight,
    const ideep::tensor& bias_opt,
    at::IntArrayRef stride,
    at::IntArrayRef padding,
    at::IntArrayRef dilation,
    int64_t groups,
    const ideep::attr_t& attr,
    at::MemoryFormat memory_format);

std::tuple<at::Tensor, at::Tensor, at::Tensor> convolution_backward_kernel(
    const at::Tensor& input,
    const at::Tensor& grad_output,
    const at::Tensor& at_weight,
    const ideep::tensor& mkldnn_weight,
    const ideep::tensor& mkldnn_bias,
    at::IntArrayRef stride,
    at::IntArrayRef padding,
    at::IntArrayRef dilation,
    int64_t groups,
    const bool weight_channels_last,
    std::array<bool, 3> output_mask);

std::vector<int64_t> calc_conv_output_size(
    at::IntArrayRef input_size,
    at::IntArrayRef kernel_size,
    at::IntArrayRef padding,
    at::IntArrayRef stride,
    at::IntArrayRef dilation);

c10::SymDimVector calc_conv_output_size(
    c10::SymIntArrayRef input_size,
    at::IntArrayRef kernel_size,
    at::IntArrayRef padding,
    at::IntArrayRef stride,
    at::IntArrayRef dilation);

// IPEX customized convolution OP with n-D packed weight
class IPEXConvolutionOp : public torch::autograd::Function<IPEXConvolutionOp> {
 public:
  // forward function without autograd overhead, will go this way when only do
  // forward
  static at::Tensor _forward(
      const at::Tensor& input,
      const at::Tensor& weight,
      const c10::optional<at::Tensor>& bias_opt,
      const at::Tensor& op_context,
      c10::optional<at::IntArrayRef> kernel_size,
      c10::optional<at::IntArrayRef> padding,
      c10::optional<at::IntArrayRef> stride,
      c10::optional<at::IntArrayRef> dilation,
      c10::optional<bool> weight_channels_last);

  static at::Tensor forward(
      torch::autograd::AutogradContext* ctx,
      const at::Tensor& input,
      const at::Tensor& weight,
      const c10::optional<at::Tensor>& bias_opt,
      const at::Tensor& op_context,
      c10::optional<at::IntArrayRef> kernel_size,
      c10::optional<at::IntArrayRef> padding,
      c10::optional<at::IntArrayRef> stride,
      c10::optional<at::IntArrayRef> dilation,
      c10::optional<bool> weight_channels_last);

  static torch::autograd::variable_list backward(
      torch::autograd::AutogradContext* ctx,
      torch::autograd::variable_list grad_outputs);
};

at::Tensor convolution_forward(
    const at::Tensor& input,
    const at::Tensor& weight,
    const c10::optional<at::Tensor>& bias_opt,
    const at::Tensor& op_context,
    c10::optional<at::IntArrayRef> kernel_size,
    c10::optional<at::IntArrayRef> padding,
    c10::optional<at::IntArrayRef> stride,
    c10::optional<at::IntArrayRef> dilation,
    c10::optional<bool> weight_channels_last);

} // namespace cpu
} // namespace torch_ipex
