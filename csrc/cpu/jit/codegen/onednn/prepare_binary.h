#pragma once

#include <torch/csrc/jit/ir/ir.h>

namespace torch_ipex {
namespace jit {
namespace fuser {
namespace onednn {

// Prepare binary ops for LLGA
//
// The pass does the following:
//
// - (1). Convert scalar input of aten::add, aten::mul and aten::div into Float
// tensor with
//   dimension [1]
//
// - (2). Decompose fused add into aten::mul + aten::add when alpha != 1.0
//
// - (3). Eliminate identity add/mul/div, i.e., tensor + 0, tensor * 1,
// tensor / 1
//
// (1) and (2) are in the purpose of aligning with the OP spec of LLGA.
// (3) is an optimization pass to remove the redundant calculation
//
void PrepareBinaryForLLGA(const std::shared_ptr<torch::jit::Graph>& graph);

// For unfused add/div, convert tensor input back to scalar input
void RevertPrepareBinaryForLLGA(
    const std::shared_ptr<torch::jit::Graph>& graph);

} // namespace onednn
} // namespace fuser
} // namespace jit
} // namespace torch_ipex
