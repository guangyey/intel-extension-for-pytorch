#pragma once

#include <torch/csrc/jit/ir/ir.h>
#include <torch/csrc/jit/passes/pass_manager.h>

namespace torch_ipex {
namespace jit {
namespace fuser {
namespace onednn {

static std::atomic<bool> onednn_enabled{true};

static std::atomic<bool>& getLlgaEnabled() {
  return onednn_enabled;
}

TORCH_API bool is_llga_fp32_bf16_enabled();

TORCH_API void set_llga_fp32_bf16_enabled(bool new_enabled);

TORCH_API void fuseGraph(std::shared_ptr<torch::jit::Graph>& g);

TORCH_API void setLlgaWeightCacheEnabled(bool enabled);

TORCH_API bool getLlgaWeightCacheEnabled();

} // namespace onednn
} // namespace fuser

struct TORCH_API RegisterLlgaFuseGraph
    : public torch::jit::PassManager<RegisterLlgaFuseGraph> {
  static bool setEnabled(bool enabled) {
    bool oldState = fuser::onednn::getLlgaEnabled();
    fuser::onednn::getLlgaEnabled() = enabled;
    if (enabled) {
      registerPass(fuser::onednn::fuseGraph);
    } else {
      clearPass();
    }
    return oldState;
  }

  static bool isEnabled() {
    return fuser::onednn::getLlgaEnabled();
  }

  // override PassManager::registerPass to register pre-pass
  static bool registerPass(torch::jit::GraphPass p) {
    if (!isRegistered()) {
      passID(registerPrePass(std::move(p)), true);
      isRegistered(true);
      return false;
    }
    return true;
  }

  // override PassManager::clearPass to clear pre-pass
  static void clearPass() {
    if (isRegistered()) {
      torch::jit::clearPrePass(passID());
      isRegistered(true);
    }
  }
};

} // namespace jit
} // namespace torch_ipex
