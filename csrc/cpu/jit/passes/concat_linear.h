//[this file is from https://github.com/pytorch/pytorch/pull/63198/files]
#pragma once

#include <torch/csrc/jit/ir/ir.h>

namespace torch_ipex {
namespace jit {

// Concats multiple linear ops with the same Tensor input
// into a single linear op.
TORCH_API bool FrozenConcatLinear(
    std::shared_ptr<torch::jit::Graph>& graph,
    std::unordered_set<torch::jit::Node*>& aten_linear);

} // namespace jit
} // namespace torch_ipex
