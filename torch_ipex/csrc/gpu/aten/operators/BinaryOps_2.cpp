#include <ATen/Context.h>
#include <ATen/Dispatch.h>
#include <ATen/native/BinaryOps.h>
#include <ATen/native/TensorIterator.h>

#include <core/DPCPP.h>
#include <utils/Pointwise.h>

#include "Loops.h"

using namespace at::dpcpp;

namespace at {
namespace AtenIpexTypeDPCPP {
namespace impl {

// Note: dpcpp compiler does not support uname type in template.
class SyclOpMul {};
class SyclOpDiv {};

static void mul_kernel_dpcpp(TensorIterator& iter) {
  AT_DISPATCH_ALL_TYPES_AND3(
      at::ScalarType::BFloat16,
      at::ScalarType::Half,
      at::ScalarType::Bool,
      iter.dtype(),
      "mul",
      [&]() {
        dpcpp_kernel_for_tensor_iter<SyclOpMul>(
            iter, [=](scalar_t a, scalar_t b) -> scalar_t { return a * b; });
      });
}

static void div_kernel_dpcpp(TensorIterator& iter) {
  if (isIntegralType(iter.dtype(), false)) {
    AT_DISPATCH_INTEGRAL_TYPES(iter.dtype(), "div", [&] {
      dpcpp_kernel_for_tensor_iter<SyclOpDiv>(
          iter, [](scalar_t a, scalar_t b) -> scalar_t { return a / b; });
    });
  } else {
    AT_DISPATCH_FLOATING_TYPES_AND2(
        at::ScalarType::BFloat16,
        at::ScalarType::Half,
        iter.dtype(),
        "div",
        [&]() {
          dpcpp_kernel_for_tensor_iter<SyclOpDiv>(
              iter, [](scalar_t a, scalar_t b) -> scalar_t { return a / b; });
        });
  }
}

// scalar to tensor
static Tensor wrapped_scalar_tensor(Scalar scalar) {
  auto tensor = scalar_to_tensor(scalar);
  tensor.unsafeGetTensorImpl()->set_wrapped_number(true);
  return tensor;
}

} // namespace impl

Tensor& mul_out(Tensor& result, const Tensor& self, const Tensor& other) {
  auto iter = TensorIterator::binary_op(
      result,
      self,
      other,
      /*check_mem_overlap=*/true);
  impl::mul_kernel_dpcpp(iter);
  return result;
}

Tensor mul(const Tensor& self, const Tensor& other) {
  Tensor result;
  auto iter = TensorIterator::binary_op(result, self, other);
  impl::mul_kernel_dpcpp(iter);
  return iter.output();
}

Tensor& mul_(Tensor& self, const Tensor& other) {
  return at::AtenIpexTypeDPCPP::mul_out(self, self, other);
}

Tensor mul(const Tensor& self, Scalar other) {
  return at::AtenIpexTypeDPCPP::mul(self, impl::wrapped_scalar_tensor(other));
}

Tensor& mul_(Tensor& self, Scalar other) {
  return at::AtenIpexTypeDPCPP::mul_(self, impl::wrapped_scalar_tensor(other));
}

Tensor& div_out(Tensor& result, const Tensor& self, const Tensor& other) {
  auto iter = TensorIterator::binary_op(
      result,
      self,
      other,
      /*check_mem_overlap=*/true);
  impl::div_kernel_dpcpp(iter);
  return result;
}

Tensor div(const Tensor& self, const Tensor& other) {
  Tensor result;
  auto iter = TensorIterator::binary_op(result, self, other);
  impl::div_kernel_dpcpp(iter);
  return iter.output();
}

Tensor& div_(Tensor& self, const Tensor& other) {
  return at::AtenIpexTypeDPCPP::div_out(self, self, other);
}

} // namespace AtenIpexTypeDPCPP
} // namespace at
