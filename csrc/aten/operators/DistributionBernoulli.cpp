#include <ATen/ATen.h>
#include <ATen/native/TensorIterator.h>

#include <core/Generator.h>
#include <utils/DPCPP.h>
#include "comm/ATDispatch.h"

#include "Distributions.h"
#include "Random.h"

namespace at {
namespace AtenIpexTypeXPU {

Tensor& uniform_(
    Tensor& self,
    double from,
    double to,
    c10::optional<Generator> generator);

Tensor& bernoulli_(
    Tensor& self,
    const Tensor& p_,
    c10::optional<Generator> gen_) {
  at::AtenIpexTypeXPU::uniform_(self, 0.0, 1.0, gen_);
  auto p = p_.to(kXPU);
  auto iter = TensorIterator::binary_op(self, self, p);
  IPEX_DISPATCH_FLOATING_TYPES_AND2(
      at::ScalarType::Half,
      at::ScalarType::BFloat16,
      iter.dtype(),
      "bernoulli_tensor_dpcpp_",
      [&] {
        dpcpp_kernel_for_tensor_iter(
            iter, [](scalar_t self, scalar_t p) -> scalar_t {
              return static_cast<scalar_t>(self < p);
            });
      });
  return self;
}

void bernoulli_scalar_dpcpp(
    TensorIterator& iter,
    double p_,
    c10::optional<Generator> gen_) {
  auto gen = get_generator_or_default<xpu::dpcpp::DPCPPGeneratorImpl>(
      gen_, xpu::dpcpp::detail::getDefaultDPCPPGenerator());
  IPEX_DISPATCH_FLOATING_TYPES_AND2(
      at::ScalarType::Half,
      at::ScalarType::BFloat16,
      iter.dtype(),
      "bernoulli_scalar_dpcpp",
      [&] {
        using accscalar_t = DiscreteDistributionType<scalar_t>::type;
        auto p = static_cast<accscalar_t>(p_);
        // define lambda for bernoulli transformation
        auto bernoulli_func = [p](accscalar_t rand) {
          return static_cast<scalar_t>(rand < static_cast<accscalar_t>(p));
        };
        AtenIpexTypeXPU::distribution_nullary_kernel<scalar_t, accscalar_t>(
            iter,
            gen,
            [](RandomState<Philox4_32_10>* state) {
              return state->uniform<scalar_t>();
            },
            bernoulli_func);
      });
}

Tensor& bernoulli_(Tensor& self, double p, c10::optional<Generator> gen_) {
  auto iter = TensorIterator::nullary_op(self);
  bernoulli_scalar_dpcpp(iter, p, gen_);
  return self;
}

} // namespace AtenIpexTypeXPU
} // namespace at