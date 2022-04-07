#include <ATen/ATen.h>
#include <ATen/Config.h>
#include <ATen/NativeFunctions.h>
#include <ATen/core/op_registration/op_registration.h>

#include <intrinsic/ipex_intrinsic.h>
#include <oneDNN/oneDNN.h>
#include <quantized/QUtil.h>
#include <runtime/Utils.h>
#include "InnerProduct.h"
#include "comm/ParamUtils.h"

using namespace dnnl;
using namespace at::native;
using namespace xpu::dpcpp;
using namespace xpu::oneDNN;

namespace at {
namespace AtenIpexTypeQuantizedXPU {

using namespace impl;

at::Tensor dpcppLinear(
    Tensor input,
    const c10::intrusive_ptr<LinearPackedParamsBase>& packed_weight,
    double output_scale,
    int64_t output_zero_point) {
  auto pack_ptr = dynamic_cast<PackedLinearWeightQDPCPP*>(packed_weight.get());
  at::Tensor weight = pack_ptr->weight;
  at::Tensor bias;
  if (pack_ptr->bias_.has_value()) {
    bias = pack_ptr->bias_.value();
  } else {
    bias = at::empty({0}, input.options());
  }

  if (weight.is_quantized()) {
    return at::addmm(
        bias,
        input,
        weight,
        bias.is_quantized() ? bias.q_scale() : 1.f,
        output_scale);
  } else {
    // omit transpose on weight
    return trans_addmm(
        bias,
        input.is_quantized() ? at::dequantize(input) : input,
        weight,
        0.f,
        1.f);
  }
}

TORCH_LIBRARY_IMPL(quantized, QuantizedXPU, m) {
  m.impl("quantized::linear", dpcppLinear);
}

} // namespace AtenIpexTypeQuantizedXPU
} // namespace at