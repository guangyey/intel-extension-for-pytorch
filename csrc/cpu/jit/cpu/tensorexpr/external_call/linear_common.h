#pragma once

#include "csrc/cpu/jit/cpu/kernels/OpContext.h"

#include <torch/csrc/jit/tensorexpr/expr.h>
#include <torch/csrc/jit/tensorexpr/lowerings.h>

#include <vector>

namespace pytnnc = torch::jit::tensorexpr;

namespace torch_ipex {
namespace jit {
namespace cpu {
namespace tensorexpr {

#define STRINGIZE_NX(a) #a
#define STRINGIZE(a) STRINGIZE_NX(a)

#define _CONCAT(a, b) a##b
#define CONCAT(a, b) _CONCAT(a, b)

#define NNC_LINEAR_NS linear
#define _EXT_FUNC(ns, func) \
  CONCAT(CONCAT(nnc_ipex, _), CONCAT(CONCAT(ns, _), func))
#define EXT_FUNC(ns, func) STRINGIZE(_EXT_FUNC(ns, func))
#define RES_VAR(ns, func) STRINGIZE(CONCAT(_EXT_FUNC(ns, func), _res))

#define DECLARE_LINEAR_FUNC_AND_RES(func)            \
  static constexpr const char* get_external_func() { \
    return EXT_FUNC(NNC_LINEAR_NS, func);            \
  }                                                  \
  static constexpr const char* get_res_var() {       \
    return RES_VAR(NNC_LINEAR_NS, func);             \
  }

typedef enum _LinearFusedOp {
  kLinearNone,
  kLinearAbs,
  kLinearExp,
  kLinearHardswish,
  kLinearMish,
  kLinearSigmoid,
  kLinearRelu,
  kLinearSqrt,
  kLinearSquare,
  kLinearTanh,
  kLinearSilu,
  kLinearLog,
  kLinearRound,
  kLinearClamp,
  kLinearElu,
  kLinearGelu,
  kLinearPow,
  kLinearLeakyRelu,
  kLinearAdd,
  kLinearHardsigmoid,
  kLinearAddRelu,
} LinearFusedOp;

static ideep::attr_t empty_attr;

struct LinearCommonOperations {
  /**
   * @brief Return the external function name literal to search its
   * correspond external function. All the linear traits should implement this
   * function.
   *
   * @return const char*
   */
  static const char* get_external_func() {
    throw std::logic_error("__invalid_nnc_func__");
    return "__invalid_nnc_func__";
  }

  /**
   * @brief Get the result var object. All the linear traits should implement
   * this function.
   *
   * @return const char*
   */
  static const char* get_res_var() {
    throw std::logic_error("__invalid_nc_res_var__");
    return "__invalid_nc_res_var__";
  }

  /**
   * @brief Get the buf handles object
   *
   * @details Most fused linear operators only requires one activation and
   * a serialized linear context. Both the activation and the context
   * are representd as BufHandle. The first item of the inputs is
   * activatation while the second one is the context.
   *
   * @param inputs
   * @return std::vector<BufHandle>
   */
  static std::vector<pytnnc::BufHandle> get_input_buf(
      const std::vector<pytnnc::ArgValue>& inputs) {
    std::vector<pytnnc::BufHandle> res = {};
    TORCH_INTERNAL_ASSERT_DEBUG_ONLY(inputs.size() == 2);
    constexpr int act_idx = 0; // Activation tenstor index
    constexpr int ctx_idx = 1; // Context index
    res.push_back(c10::get<pytnnc::BufHandle>(inputs[act_idx]));
    res.push_back(c10::get<pytnnc::BufHandle>(inputs[ctx_idx]));
    return res;
  }

  /**
   * @brief Get the extra args object
   *
   * @details Most fused linear operators do not require extra parameters.
   *
   * @param inputs
   * @return std::vector<PytTeExprHandle>
   */
  static std::vector<pytnnc::ExprHandle> get_extra_args(
      const std::vector<pytnnc::ArgValue>& inputs) {
    return {};
  }

  /**
   * @brief Insert the scalar arg object
   *
   * @details Check dtype of scalar arg and insert it to extra_args
   */
  static void insert_scalar_arg(
      const pytnnc::ArgValue& arg_data,
      std::vector<pytnnc::ExprHandle>& extra_args) {
    if (auto i = c10::get_if<int64_t>(&arg_data)) {
      extra_args.push_back(static_cast<double>(*i));
    } else if (auto i = c10::get_if<double>(&arg_data)) {
      extra_args.push_back(static_cast<double>(*i));
    } else {
      throw pytnnc::unsupported_dtype(
          "Trying to convert unsupported dtype to constant");
    }
  }

  static pytnnc::BufHandle get_result_buf(
      const char* res_var_name,
      const std::vector<pytnnc::ArgValue>& inputs,
      const std::vector<pytnnc::ExprHandle>& output_shape,
      const std::vector<pytnnc::ExprHandle>& output_strides,
      const c10::optional<pytnnc::ScalarType>& output_type) {
    auto te_dtype = pytnnc::Dtype(*output_type);
    return pytnnc::BufHandle(
        res_var_name, output_shape, output_strides, te_dtype);
  }

  /**
   * @brief Get the lina=ear op context object
   *
   * @return torch_ipex::cpu::LinearOpContext*
   */
  static torch_ipex::cpu::LinearOpContext* get_linear_op_context(
      void** buf_data) {
    // The default order is:
    //     0: output tensor
    //     1: activation tensor
    //     2: linear op context
    constexpr int ctx_idx = 2;
    return reinterpret_cast<torch_ipex::cpu::LinearOpContext*>(
        buf_data[ctx_idx]);
  }

  /**
   * @brief Get the attr object to be used for fusion
   *
   * @param buf_data The buf_data may contains the valus to create the fusion
   * attribute.
   * @return ideep::attr_t
   */
  static ideep::attr_t get_attr(int64_t* buf_data) {
    return empty_attr;
  }
};

template <LinearFusedOp T>
struct LoweringFuncTrait;

} // namespace tensorexpr
} // namespace cpu
} // namespace jit
} // namespace torch_ipex
