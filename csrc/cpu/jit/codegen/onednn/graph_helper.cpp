#include "graph_helper.h"
#include "fusion_group_name.h"
#include "utils.h"

#include "codegen/LlgaTensorImpl.h"
#include "codegen/onednn/interface.h"

#include <ATen/core/functional.h>
#include <torch/csrc/jit/jit_log.h>
#include <torch/csrc/jit/passes/utils/subgraph_utils.h>

namespace torch_ipex {
namespace jit {
namespace fuser {
namespace onednn {

using namespace torch::jit;
using opkind = dnnl::graph::op::kind;

void fixOptionalInput(Node* node, int index) {
  if (!node->input(index)->mustNotBeNone()) {
    // Replace non-existent optional bias with const None
    auto g = node->owningGraph();
    auto n = g->createNone();
    auto v = n->insertBefore(node)->output();
    node->replaceInput(index, v);
  }
}

c10::optional<size_t> getDimensions(Value* v) {
  if (v->type()->isSubtypeOf(TensorType::get()))
    return v->type()->cast<TensorType>()->sizes().size();
  else
    return c10::nullopt;
}

// TODO: tensor to vector? We have assumed that zp and scale tensor is
// contiguous
std::vector<float> FloatTensorToVector(const at::Tensor& tensor) {
  std::vector<float> vectors;
  if (tensor.numel() == 1) {
    vectors.push_back(tensor.item().toFloat());
  } else {
    for (int i = 0; i < tensor.numel(); i++) {
      vectors.push_back(tensor[i].item().toFloat());
    }
  }
  return vectors;
}

std::vector<int64_t> IntTensorToVector(const at::Tensor& tensor) {
  std::vector<int64_t> vectors;
  if (tensor.numel() == 1) {
    vectors.push_back(tensor.item().toInt());
  } else {
    for (int i = 0; i < tensor.numel(); i++) {
      vectors.push_back(tensor[i].item().toInt());
    }
  }
  return vectors;
}

Operator makeWildcardOp(Node* node) {
  auto o = Operator(node, opkind::Wildcard);
  // wildcard op contains only topology info
  for (size_t i = 0; i < node->inputs().size(); i++) {
    o.setInput(i);
  }
  for (size_t i = 0; i < node->outputs().size(); i++) {
    o.setOutput(i);
  }
  return o;
}

#define REQ(cond)                                     \
  if (!(cond)) {                                      \
    GRAPH_DEBUG("Unsupported condition " #cond "\n"); \
    return makeWildcardOp(node);                      \
  }

Operator makeEltwiseOp(Node* node, opkind kind) {
  return Operator(node, kind).setInput(0).setOutput(0);
}

Operator makeBinaryOp(Node* node, opkind kind) {
  REQ(node->input(0)->type()->isSubtypeOf(TensorType::get()) &&
      node->input(1)->type()->isSubtypeOf(TensorType::get()))

  auto dim0 = getDimensions(node->input(0));
  REQ(dim0.has_value() && dim0.value() != 0);

  return Operator(node, kind).setInput(0, 1).setOutput(0);
}

// For dequantize, the zp and scale is found through the input node which is
// a quantize_per_tensor or a quantize_per_channel node.
// Not able to get it directly from the input tensor during compile time
Operator makeDequantOp(Node* node, Node* input_node) {
  if (input_node->kind() == Symbol::aten("quantize_per_tensor")) {
    node->s_(Symbol::attr("qtype"), std::string("per_tensor"));
    auto zps_vector = utils::getZPSVector(input_node);
    node->is_(Symbol::attr("zps"), zps_vector);
    auto scale = utils::getScale(input_node);
    node->fs_(Symbol::attr("scales"), {scale});
    return Operator(node, opkind::Dequantize)
        .setInput(0)
        .setOutput(0)
        .setAttr(dnnl::graph::op::attr::qtype, std::string("per_tensor"))
        .setAttr(dnnl::graph::op::attr::scales, std::vector<float>{scale})
        .setAttr(dnnl::graph::op::attr::zps, zps_vector);
  } else if (input_node->kind() == Symbol::aten("quantize_per_channel")) {
    node->s_(Symbol::attr("qtype"), std::string("per_channel"));
    node->t_(Symbol::attr("zps"), Operator::Tensor(input_node, 2));
    node->t_(Symbol::attr("scales"), Operator::Tensor(input_node, 1));
    node->i_(Symbol::attr("axis"), Operator::Int(input_node, 3));

    return Operator(node, opkind::Dequantize)
        .setInput(0)
        .setOutput(0)
        .setAttr(
            dnnl::graph::op::attr::scales,
            FloatTensorToVector(Operator::Tensor(input_node, 1)))
        .setAttr(
            dnnl::graph::op::attr::zps,
            IntTensorToVector(Operator::Tensor(input_node, 2)))
        .setAttr(dnnl::graph::op::attr::axis, Operator::Int(input_node, 3))
        .setAttr(dnnl::graph::op::attr::qtype, std::string("per_channel"));
  } else {
    TORCH_CHECK(
        input_node->kind() == prim::Constant,
        "Expect input_node kind to be prim::Constant but got ",
        input_node->kind().toQualString());

    Value* v = input_node->output();
    TORCH_CHECK(
        v->type()->cast<TensorType>(),
        "Constant input to dequant must be Tensor type");
    auto qtensor = toIValue(v)->toTensor();

    TORCH_CHECK(
        qtensor.scalar_type() == at::ScalarType::QInt8 ||
            qtensor.scalar_type() == at::ScalarType::QUInt8,
        "Expect input to dequant to be int8 dtype but got ",
        qtensor.scalar_type());
    auto scalar_type = qtensor.scalar_type();

    switch (qtensor.qscheme()) {
      case at::kPerTensorAffine:
        return Operator(node, opkind::Dequantize)
            .setInput(0)
            .setOutput(0)
            .setAttr(
                dnnl::graph::op::attr::scales,
                Operator::FloatValueToVector(
                    static_cast<float>(qtensor.q_scale())))
            .setAttr(
                dnnl::graph::op::attr::zps,
                Operator::IntValueToVector(qtensor.q_zero_point()))
            .setAttr(dnnl::graph::op::attr::qtype, std::string("per_tensor"));
      case at::kPerChannelAffine:
        return Operator(node, opkind::Dequantize)
            .setInput(0)
            .setOutput(0)
            .setAttr(
                dnnl::graph::op::attr::scales,
                FloatTensorToVector(qtensor.q_per_channel_scales()))
            .setAttr(
                dnnl::graph::op::attr::zps,
                IntTensorToVector(qtensor.q_per_channel_zero_points()))
            .setAttr(dnnl::graph::op::attr::axis, qtensor.q_per_channel_axis())
            .setAttr(dnnl::graph::op::attr::qtype, std::string("per_channel"));
      default:
        TORCH_CHECK(
            false,
            "Unsupported tensor quantization type ",
            toString(qtensor.qscheme()));
    }
  }
}

Operator LlgaGraphHelper::createOperator(Node* node) const {
  // switch does not allow non-constexpr function, to make the Symbol constexpr,
  // we must add them to the list in aten/src/ATen/core/interned_strings.h to
  // explicitly use interned strings as symbols. Thus, we use if-else here
  // instead of switch to avoid having to apply patch on PyTorch.
  auto nodeKind = node->kind();
  // Calling node->kind() only once so that the compiler would create a
  // jump-table
  if (nodeKind == Symbol::aten("conv2d")) {
    fixOptionalInput(node, 2);
    return Operator(node, opkind::Convolution)
        .setInput(0, 1, 2)
        .setOutput(0)
        .setAttr(dnnl::graph::op::attr::data_format, std::string("NCX"))
        .setAttr(dnnl::graph::op::attr::strides, Operator::Ints, 3)
        .setAttr(dnnl::graph::op::attr::pads_begin, Operator::Ints, 4)
        .setAttr(dnnl::graph::op::attr::pads_end, Operator::Ints, 4)
        .setAttr(dnnl::graph::op::attr::dilations, Operator::Ints, 5)
        .setAttr(dnnl::graph::op::attr::groups, Operator::Int, 6)
        .setAttr(dnnl::graph::op::attr::weights_format, std::string("OIX"));
  } else if (nodeKind == Symbol::aten("_convolution")) {
    bool transposed = Operator::Bool(node, 6);
    if (transposed) {
      return Operator(node, opkind::ConvTranspose)
          .setInput(0, 1, 2)
          .setOutput(0)
          .setAttr(dnnl::graph::op::attr::data_format, std::string("NCX"))
          .setAttr(dnnl::graph::op::attr::strides, Operator::Ints, 3)
          .setAttr(dnnl::graph::op::attr::pads_begin, Operator::Ints, 4)
          .setAttr(dnnl::graph::op::attr::pads_end, Operator::Ints, 4)
          .setAttr(dnnl::graph::op::attr::output_padding, Operator::Ints, 7)
          .setAttr(dnnl::graph::op::attr::dilations, Operator::Ints, 5)
          .setAttr(dnnl::graph::op::attr::groups, Operator::Int, 8)
          .setAttr(dnnl::graph::op::attr::weights_format, std::string("IOX"));
    } else {
      return Operator(node, opkind::Convolution)
          .setInput(0, 1, 2)
          .setOutput(0)
          .setAttr(dnnl::graph::op::attr::data_format, std::string("NCX"))
          .setAttr(dnnl::graph::op::attr::strides, Operator::Ints, 3)
          .setAttr(dnnl::graph::op::attr::pads_begin, Operator::Ints, 4)
          .setAttr(dnnl::graph::op::attr::pads_end, Operator::Ints, 4)
          .setAttr(dnnl::graph::op::attr::dilations, Operator::Ints, 5)
          .setAttr(dnnl::graph::op::attr::groups, Operator::Int, 8)
          .setAttr(dnnl::graph::op::attr::weights_format, std::string("OIX"));
    }
  } else if (
      (nodeKind == Symbol::aten("conv_transpose2d")) ||
      (nodeKind == Symbol::aten("conv_transpose3d"))) {
    fixOptionalInput(node, 2);
    return Operator(node, opkind::ConvTranspose)
        .setInput(0, 1, 2)
        .setOutput(0)
        .setAttr(dnnl::graph::op::attr::data_format, std::string("NCX"))
        .setAttr(dnnl::graph::op::attr::strides, Operator::Ints, 3)
        .setAttr(dnnl::graph::op::attr::pads_begin, Operator::Ints, 4)
        .setAttr(dnnl::graph::op::attr::pads_end, Operator::Ints, 4)
        .setAttr(dnnl::graph::op::attr::output_padding, Operator::Ints, 5)
        .setAttr(dnnl::graph::op::attr::dilations, Operator::Ints, 7)
        .setAttr(dnnl::graph::op::attr::groups, Operator::Int, 6)
        .setAttr(dnnl::graph::op::attr::weights_format, std::string("IOX"));
  } else if (nodeKind == Symbol::aten("batch_norm")) {
    auto training = toIValue(node->input(5));
    REQ(training.has_value()); // cannot get training status in script mode
    REQ(!training->toBool()); // TODO: support bn training
    return Operator(node, opkind::BatchNormInference)
        .setInput(0, 1, 2, 3, 4)
        .setOutput(0)
        .setAttr(dnnl::graph::op::attr::data_format, std::string("NCX"))
        .setAttr(dnnl::graph::op::attr::epsilon, Operator::Float, 7);
  } else if (nodeKind == Symbol::aten("layer_norm")) {
    REQ(node->input(1)->node()->kind() == prim::Constant);
    auto normalized_shape = Operator::Ints(node, 1);
    REQ(normalized_shape.size() == 1);
    return Operator(node, opkind::LayerNorm)
        .setInput(0, 2, 3)
        .setOutput(0)
        .setAttr(dnnl::graph::op::attr::epsilon, Operator::Float, 4)
        .setAttr(dnnl::graph::op::attr::keep_stats, false);
  } else if (nodeKind == Symbol::aten("mean")) {
    REQ(node->input(1)->mustNotBeNone());
    auto numInputs = node->inputs().size();
    bool keep_dims = false;
    if (numInputs >= 3) {
      keep_dims = toIValue(node->namedInput("keepdim"))->toBool();
      if (numInputs == 4) {
        // we don't support typecast with mean
        REQ(toIValue(node->namedInput("dtype"))->isNone());
      }
    }
    std::vector<int64_t> dims{};
    auto ivalue_dim = toIValue(node->namedInput("dim"));
    if (ivalue_dim.has_value()) {
      dims = ivalue_dim->toIntVector();
    }
    return Operator(node, opkind::ReduceMean)
        .setInput(0)
        .setOutput(0)
        .setAttr(dnnl::graph::op::attr::axes, dims)
        .setAttr(dnnl::graph::op::attr::keep_dims, keep_dims);
  } else if (nodeKind == Symbol::aten("add")) {
    return makeBinaryOp(node, opkind::Add);
  } else if (nodeKind == Symbol::aten("mul")) {
    return makeBinaryOp(node, opkind::Multiply);
  } else if (nodeKind == Symbol::aten("div")) {
    return makeBinaryOp(node, opkind::Divide);
  } else if (nodeKind == Symbol::aten("tanh")) {
    return makeEltwiseOp(node, opkind::Tanh);
  } else if (nodeKind == Symbol::aten("relu")) {
    return makeEltwiseOp(node, opkind::ReLU);
  } else if (nodeKind == Symbol::aten("elu")) {
    return makeEltwiseOp(node, opkind::Elu)
        .setAttr(dnnl::graph::op::attr::alpha, Operator::Float, 1);
  } else if (nodeKind == Symbol::aten("sigmoid")) {
    return makeEltwiseOp(node, opkind::Sigmoid);
  } else if (nodeKind == Symbol::aten("gelu")) {
    return makeEltwiseOp(node, opkind::GELU);
  } else if (nodeKind == Symbol::aten("mish")) {
    return makeEltwiseOp(node, opkind::Mish);
  } else if (nodeKind == Symbol::aten("round")) {
    return makeEltwiseOp(node, opkind::Round);
  } else if (nodeKind == Symbol::aten("exp")) {
    return makeEltwiseOp(node, opkind::Exp);
  } else if (nodeKind == Symbol::aten("sqrt")) {
    return makeEltwiseOp(node, opkind::Sqrt);
  } else if (nodeKind == Symbol::aten("rsqrt")) {
    return Operator(node, opkind::Pow)
        .setInput(0)
        .setOutput(0)
        .setAttr(dnnl::graph::op::attr::beta, static_cast<float>(-0.5));
  } else if (nodeKind == Symbol::aten("pow")) {
    auto beta = toIValue(node->input(1));
    REQ(beta.has_value() && (beta->isDouble() || beta->isInt()));
    float beta_value = 0.0;
    if (beta->isDouble()) {
      beta_value = beta->toDouble();
    } else {
      beta_value = beta->toInt();
    }
    return Operator(node, opkind::Pow)
        .setInput(0)
        .setOutput(0)
        .setAttr(dnnl::graph::op::attr::beta, beta_value);
  } else if (nodeKind == Symbol::aten("max")) {
    REQ(node->inputs().size() == 2);
    return makeBinaryOp(node, opkind::Maximum);
  } else if (nodeKind == Symbol::aten("abs")) {
    return makeEltwiseOp(node, opkind::Abs);
  } else if (nodeKind == Symbol::aten("square")) {
    return makeEltwiseOp(node, opkind::Square);
  } else if (nodeKind == Symbol::aten("clamp")) {
    // PyTorch API already checks that both min & max are not None.
    // But we can check it nevertheless.
    auto clamp_min = toIValue(node->input(1));
    auto clamp_max = toIValue(node->input(2));
    REQ(!(clamp_max->isNone() && clamp_min->isNone()));
    auto clamp_min_value = (clamp_min->isNone())
        ? -std::numeric_limits<float>::infinity()
        : Operator::ScalarToFloat(node, 1);
    auto clamp_max_value = (clamp_max->isNone())
        ? std::numeric_limits<float>::infinity()
        : Operator::ScalarToFloat(node, 2);
    return makeEltwiseOp(node, opkind::Clamp)
        .setAttr(dnnl::graph::op::attr::min, clamp_min_value)
        .setAttr(dnnl::graph::op::attr::max, clamp_max_value);
  } else if (nodeKind == Symbol::aten("hardsigmoid")) {
    return makeEltwiseOp(node, opkind::HardSigmoid)
        .setAttr(dnnl::graph::op::attr::alpha, 1.0f / 6)
        .setAttr(dnnl::graph::op::attr::beta, 0.5f);
  } else if (nodeKind == Symbol::aten("hardtanh")) {
    return makeEltwiseOp(node, opkind::Clamp)
        .setAttr(dnnl::graph::op::attr::min, Operator::ScalarToFloat, 1)
        .setAttr(dnnl::graph::op::attr::max, Operator::ScalarToFloat, 2);
  } else if (nodeKind == Symbol::aten("hardswish")) {
    return makeEltwiseOp(node, opkind::HardSwish);
  } else if (nodeKind == Symbol::aten("log")) {
    return makeEltwiseOp(node, opkind::Log);
  } else if (nodeKind == Symbol::aten("leaky_relu")) {
    return makeEltwiseOp(node, opkind::LeakyReLU)
        .setAttr(dnnl::graph::op::attr::alpha, Operator::Float, 1);
  } else if (nodeKind == Symbol::aten("softmax")) {
    auto dim0 = getDimensions(node->input(0));
    REQ(dim0.has_value());

    auto axis = Operator::Int(node, 1);
    if (axis < 0)
      axis += dim0.value();

    return Operator(node, opkind::SoftMax)
        .setInput(0)
        .setOutput(0)
        .setAttr(dnnl::graph::op::attr::axis, axis);
  } else if (nodeKind == Symbol::aten("cat")) {
    auto o = Operator(node, opkind::Concat);
    REQ(node->input(0)->node()->kind() == prim::ListConstruct);
    REQ(node->input(0)->uses().size() == 1);
    REQ(node->input(1)->node()->kind() == prim::Constant);
    // aten::cat needs a special handling since it takes a Tensor[] as input.
    // We set the inputs of ListConstruct as the inputs of cat.
    //
    // Pytorch IR:                              LLGA sees:
    //     %a    %b     %c          %dim              %a    %b    %c
    //      \     |     /             |                \     |    /
    //   prim::ListConstruct   prim::Constant     llga::Concat[axis=%dim]
    //                    \      /
    //                    aten::cat
    auto listConstruct = node->input(0)->node();
    for (auto input : listConstruct->inputs())
      o.setInputValue(input);
    return o.setOutput(0).setAttr(
        dnnl::graph::op::attr::axis, Operator::Int, 1);
  } else if (nodeKind == Symbol::aten("max_pool2d")) {
    auto rounding_type = Operator::Bool(node, 5) ? "ceil" : "floor";
    return Operator(node, opkind::MaxPool)
        .setInput(0)
        .setOutput(0)
        .setAttr(dnnl::graph::op::attr::data_format, std::string("NCX"))
        .setAttr(dnnl::graph::op::attr::kernel, Operator::Ints, 1)
        .setAttr(dnnl::graph::op::attr::strides, Operator::Ints, 2)
        .setAttr(dnnl::graph::op::attr::pads_begin, Operator::Ints, 3)
        .setAttr(dnnl::graph::op::attr::pads_end, Operator::Ints, 3)
        .setAttr(dnnl::graph::op::attr::dilations, Operator::Ints, 4)
        .setAttr(
            dnnl::graph::op::attr::rounding_type, std::string(rounding_type));
  } else if (nodeKind == Symbol::aten("avg_pool2d")) {
    auto rounding_type = Operator::Bool(node, 4) ? "ceil" : "floor";
    auto divisor_override = toIValue(node->input(6));
    REQ(divisor_override->isNone());
    return Operator(node, opkind::AvgPool)
        .setInput(0)
        .setOutput(0)
        .setAttr(dnnl::graph::op::attr::data_format, std::string("NCX"))
        .setAttr(dnnl::graph::op::attr::kernel, Operator::Ints, 1)
        .setAttr(dnnl::graph::op::attr::strides, Operator::Ints, 2)
        .setAttr(dnnl::graph::op::attr::pads_begin, Operator::Ints, 3)
        .setAttr(dnnl::graph::op::attr::pads_end, Operator::Ints, 3)
        .setAttr(dnnl::graph::op::attr::exclude_pad, !Operator::Bool(node, 5))
        .setAttr(
            dnnl::graph::op::attr::rounding_type, std::string(rounding_type));
  } else if (nodeKind == Symbol::aten("matmul")) {
    auto dim0 = getDimensions(node->input(0)).value_or(-1);
    auto dim1 = getDimensions(node->input(1)).value_or(-1);
    // TODO: support all shape combinations
    REQ((dim0 == 2 && dim1 == 2) || (dim0 == 4 && dim1 == 4) ||
        (dim0 == 3 && dim1 == 2));
    // fall through
    return Operator(node, opkind::MatMul).setInput(0, 1).setOutput(0);
  } else if (nodeKind == Symbol::aten("mm")) {
    return Operator(node, opkind::MatMul).setInput(0, 1).setOutput(0);
  } else if (nodeKind == Symbol::aten("bmm")) {
    return Operator(node, opkind::MatMul).setInput(0, 1).setOutput(0);
  } else if (nodeKind == Symbol::aten("linear")) {
    auto dim0 = getDimensions(node->input(0)).value_or(-1);
    auto dim1 = getDimensions(node->input(1)).value_or(-1);
    // REQ(dim1 == 2);

    return Operator(node, opkind::MatMul)
        .setInput(0, 1, 2)
        .setOutput(0)
        .setAttr(dnnl::graph::op::attr::transpose_b, true);
  } else if (nodeKind == Symbol::aten("to")) {
    return Operator(node, opkind::TypeCast).setInput(0).setOutput(0);
  } else if (nodeKind == Symbol::aten("quantize_per_tensor")) {
    // TODO: how to handle this case
    REQ(node->input(1)->node()->kind() != Symbol::aten("q_scale"));

    // TODO: how to handle this case:
    //      quantize_per_tensor
    //   ---/-----/-----\-----\---
    // dequant q_scale  q_zp  dtype
    // REQ(node->output(0)->uses().size() <= 2);
    auto scale = node->input(1);
    REQ(utils::isScaleSupported(scale));

    auto zero_point = node->input(2);
    REQ(utils::isZeroPointSupported(zero_point));

    return Operator(node, opkind::Quantize)
        .setInput(0)
        .setOutput(0)
        .setAttr(dnnl::graph::op::attr::scales, Operator::FloatToVector, 1)
        .setAttr(dnnl::graph::op::attr::zps, Operator::IntToVector, 2)
        .setAttr(dnnl::graph::op::attr::qtype, std::string("per_tensor"));
  } else if (nodeKind == Symbol::aten("quantize_per_channel")) {
    return Operator(node, opkind::Quantize)
        .setInput(0)
        .setOutput(0)
        .setAttr(
            dnnl::graph::op::attr::scales,
            FloatTensorToVector(Operator::Tensor(node, 1)))
        .setAttr(
            dnnl::graph::op::attr::zps,
            IntTensorToVector(Operator::Tensor(node, 2)))
        .setAttr(dnnl::graph::op::attr::axis, Operator::Int, 3)
        .setAttr(dnnl::graph::op::attr::qtype, std::string("per_channel"));
  } else if (nodeKind == Symbol::aten("dequantize")) {
    if (node->numAttributes() == 0) {
      Node* input_node = node->input(0)->node();
      REQ(utils::isSupportedAsInputToDequant(input_node));

      return makeDequantOp(node, input_node);
    } else {
      if (node->s(Symbol::attr("qtype")) == std::string("per_tensor")) {
        std::vector<double> scales_double = node->fs(Symbol::attr("scales"));
        std::vector<float> scales_float;
        for (int i = 0; i < scales_double.size(); i++) {
          scales_float.push_back(static_cast<float>(scales_double[i]));
        }

        return Operator(node, opkind::Dequantize)
            .setInput(0)
            .setOutput(0)
            .setAttr(dnnl::graph::op::attr::scales, scales_float)
            .setAttr(dnnl::graph::op::attr::zps, node->is(Symbol::attr("zps")))
            .setAttr(
                dnnl::graph::op::attr::qtype, node->s(Symbol::attr("qtype")));
      } else {
        return Operator(node, opkind::Dequantize)
            .setInput(0)
            .setOutput(0)
            .setAttr(
                dnnl::graph::op::attr::scales,
                FloatTensorToVector(node->t(Symbol::attr("scales"))))
            .setAttr(
                dnnl::graph::op::attr::zps,
                IntTensorToVector(node->t(Symbol::attr("zps"))))
            .setAttr(dnnl::graph::op::attr::axis, node->i(Symbol::attr("axis")))
            .setAttr(
                dnnl::graph::op::attr::qtype, node->s(Symbol::attr("qtype")));
      }
    }
  } else if (nodeKind == Symbol::aten("permute")) {
    REQ(aliasDb_->hasInputWriters(node) == false) {
      return Operator(node, opkind::StaticTranspose)
          .setInput(0)
          .setOutput(0)
          .setAttr(
              dnnl::graph::op::attr::order,
              toIValue(node->input(1))->toIntVector());
    }
  } else if (nodeKind == Symbol::aten("contiguous")) {
    // Contiguous should only be mapped to oneDNN Graph if the destination
    // memory-layout is different than the source memory-format
    // Strides would be different, but shape would be same
    auto typeOfInput = node->input(0)->type()->expect<TensorType>();
    auto typeOfOutput = node->output(0)->type()->expect<TensorType>();
    auto inputStrides = typeOfInput->strides().concrete_sizes();
    auto outputStrides = typeOfOutput->strides().concrete_sizes();
    REQ(inputStrides != outputStrides);
    return Operator(node, opkind::Reorder).setInput(0).setOutput(0);
  } else if (nodeKind == Symbol::aten("where")) {
    return Operator(node, opkind::Select).setInput(0, 1, 2).setOutput(0);
  }

  GRAPH_DEBUG("Making ", nodeKind.toQualString(), " a wildcard");
  return makeWildcardOp(node);
}

dnnl::graph::op LlgaGraphHelper::createLlgaOp(Node* node) {
  return createOperator(node).llgaOp();
}

bool LlgaGraphHelper::isSupported(Node* node) const {
  return createOperator(node).kind() != opkind::Wildcard;
}

at::DeviceType inferDeviceFromValue(Value* v) {
  auto tt = v->type()->cast<TensorType>();
  if (!tt)
    return at::kCPU;
  auto device = tt->device();
  if (!device)
    return at::kCPU;
  return device->type();
}

at::DeviceType inferDevice(const std::shared_ptr<Graph>& graph) {
  auto dt = inferDeviceFromValue(graph->inputs()[0]);
  TORCH_CHECK(
      std::all_of(
          graph->inputs().begin(),
          graph->inputs().end(),
          [dt](Value* v) { return inferDeviceFromValue(v) == dt; }),
      "All inputs must have the same deive type");
  return dt;
}

dnnl::graph::engine::kind getLlgaEngineKind(at::DeviceType type) {
  switch (type) {
    case at::DeviceType::CPU:
      return dnnl::graph::engine::kind::cpu;
    default:
      TORCH_CHECK(false, "Not support device type ", type);
  }
}

void mayAddListConstructIntoConcatPartition(
    Node* n,
    OpPartitionMap& opToOwningPartition) {
  // Since prim::ListConstruct is not visible to the LLGA,
  // it will not be in any partition returned from partfuseritioning results.
  // We need rewrite opToOwningPartition to make the prim::ListConstruct to be
  // virtually in the same partition with the aten::cat, so that
  // prim::ListConstruct can be fused into the fusion group by graph fuser
  if (n->kind() == aten::cat && opToOwningPartition.has(n)) {
    auto listConstrcut = n->input(0)->node();
    auto partitionId = opToOwningPartition.get(n);
    opToOwningPartition.add(listConstrcut, partitionId);
  }
}

// Currently, we only rewrite quantization partitions with LLGA.
// TODO: remove this check in the future if we want to use LLGA for fp32 and
// bf16
bool shouldRewrite(dnnl::graph::partition partition) {
  // TODO: debug feature to enable llga for fp32 and bf16
  if (is_llga_fp32_bf16_enabled()) {
    return true;
  }

  // check if the partition is quantization-related
  auto opIds = partition.get_ops();
  for (size_t opId : opIds) {
    auto node_in_partition = Operator::getNode(opId);
    if (node_in_partition->kind() == Symbol::aten("quantize_per_tensor") ||
        node_in_partition->kind() == Symbol::aten("quantize_per_channel") ||
        node_in_partition->kind() == Symbol::aten("dequantize")) {
      return true;
    }
  }
  GRAPH_DEBUG("Excluding non-quantization partition ", partition.get_id());
  return false;
}

LlgaGraphHelper::LlgaGraphHelper(
    const std::shared_ptr<Graph>& graph,
    dnnl::graph::partition::policy policy) {
  auto deviceType = inferDevice(graph);
  auto engineKind = getLlgaEngineKind(deviceType);
  dnnl::graph::graph g{engineKind};
  aliasDb_ = std::make_unique<torch::jit::AliasDb>(graph);
  GRAPH_DEBUG("Constructing LLGA graph");
  // TODO: select nodes in top-level block for now
  for (auto* node : graph->block()->nodes()) {
    auto kindOfNode = node->kind();
    auto op = createLlgaOp(node);

    try {
      g.add_op(op);
      GRAPH_DEBUG("  Added node ", kindOfNode.toQualString());
    } catch (std::exception& e) {
      GRAPH_DEBUG("The backend failed to add node ", kindOfNode.toQualString());
      g.add_op(makeWildcardOp(node).llgaOp());
    }

    for (Value* input : node->inputs()) {
      tensorIdToValue_.emplace(input->unique(), input);
    }
  }

  g.finalize();

  GRAPH_DEBUG("Get Partitions");
  std::vector<dnnl::graph::partition> partitions = g.get_partitions(policy);
  // excluded unsupported Wildcard partitions
  for (size_t partId = 0; partId < partitions.size(); partId++) {
    if (partitions[partId].is_supported() && shouldRewrite(partitions[partId]))
      partitions_.push_back(partitions[partId]);
  }

  GRAPH_DEBUG("  Got #partitions: ", partitions_.size());
  for (size_t partId = 0; partId < partitions_.size(); partId++) {
    for (auto opId : partitions_[partId].get_ops()) {
      opToOwningPartition_.add(opId, partId);
    }
  }

  // Scanning the graph again for post processing
  for (auto* node : graph->block()->nodes()) {
    mayAddListConstructIntoConcatPartition(node, opToOwningPartition_);
  }
}

bool LlgaGraphHelper::isLlgaSubgraph(const Node* node) {
  return node->hasAttribute(attr::Subgraph) &&
      node->kind() == Symbol::fromQualString(LlgaFusionGroupName());
}

bool LlgaGraphHelper::shouldMerge(Node* toMerge, Node* subgraph) {
  TORCH_CHECK(
      isLlgaSubgraph(subgraph),
      "The consumer node does not contain a subgraph");
  if (!shouldConsiderForMerge(toMerge)) {
    return false;
  }
  return opToOwningPartition_.get(toMerge) ==
      opToOwningPartition_.get(subgraph);
}

void checkAndRemoveAttr(Node* n, std::string attr) {
  TORCH_CHECK(
      n->hasAttributeS(attr),
      "dequant node with numAttributes != 0 must have attr: ",
      attr);
  n->removeAttributeS(attr);
}

void removeAttrOfDequant(Node* n) {
  if (n->kind() == Symbol::aten("dequantize")) {
    if (n->numAttributes() == 0)
      return;
    std::vector<std::string> common_attrs{"zps", "scales"};
    for (const auto& attr : common_attrs) {
      checkAndRemoveAttr(n, attr);
    }

    if (n->s(Symbol::attr("qtype")) == std::string("per_channel")) {
      checkAndRemoveAttr(n, std::string("axis"));
    }
    checkAndRemoveAttr(n, std::string("qtype"));
  }
}

bool LlgaGraphHelper::isSingleQuantDequantTo(Node* n) {
  if (n->kind() != Symbol::aten("quantize_per_tensor") &&
      n->kind() != Symbol::aten("quantize_per_channel") &&
      n->kind() != Symbol::aten("dequantize") && n->kind() != aten::to)
    return false;
  if (!opToOwningPartition_.has(n))
    return false;

  auto partitionId = opToOwningPartition_.get(n);
  auto OpNum = partitions_[partitionId].get_ops_num();
  return OpNum == 1;
}

bool LlgaGraphHelper::shouldConsiderForMerge(Node* node) {
  // if we're already in the process of merging
  if (isLlgaSubgraph(node)) {
    return true;
  }
  // For a partition composed of 1 single quant, 1 single dequant or 1 single to
  // do not rewrite it in the bridge, so that the FWK may have chances
  // to optimize single int8/bf16 op that LLGA does not support
  if (isSingleQuantDequantTo(node)) {
    // We have added attr on dequant node to create LLGA dequant op.
    // If we won't rewrite it with LLGA op, remove the attr here.
    removeAttrOfDequant(node);
    return false;
  }
  return opToOwningPartition_.has(node);
}

Node* LlgaGraphHelper::createSingletonSubgraph(Node* n, AliasDb& aliasDb) {
  auto partitionId = opToOwningPartition_.get(n);
  GRAPH_DEBUG(
      "Creating FusionGroup_", partitionId, " for ", n->kind().toQualString());
  auto group = SubgraphUtils::createSingletonSubgraphAndUpdateAliasing(
      n, Symbol::fromQualString(LlgaFusionGroupName()), aliasDb);
  opToOwningPartition_.add(group, partitionId);
  return group;
}

void LlgaGraphHelper::mergeNodeIntoSubgraph(
    Node* toMerge,
    Node* subgraphNode,
    AliasDb& aliasDb) {
  if (isLlgaSubgraph(toMerge)) {
    GRAPH_DEBUG(
        "Merging ",
        toMerge->kind().toQualString(),
        "_",
        opToOwningPartition_.get(toMerge),
        " into ",
        subgraphNode->kind().toQualString(),
        "_",
        opToOwningPartition_.get(subgraphNode));
  } else {
    GRAPH_DEBUG(
        "Merging ",
        toMerge->kind().toQualString(),
        " into ",
        subgraphNode->kind().toQualString(),
        "_",
        opToOwningPartition_.get(subgraphNode));
  }

  SubgraphUtils::mergeNodeIntoSubgraphAndUpdateAliasing(
      toMerge, subgraphNode, aliasDb);
}

void LlgaGraphHelper::unmergeIfAnyNodeIsMissing(Node* subgraphNode) {
  TORCH_CHECK(isLlgaSubgraph(subgraphNode), "Cannot unmerge a non-LLGA node");

  auto partitionId = opToOwningPartition_.get(subgraphNode);
  auto expectOpNum = partitions_[partitionId].get_ops_num();
  auto actualOpNum = countSupportedOps(subgraphNode->g(attr::Subgraph));

  if (expectOpNum != actualOpNum) {
    GRAPH_DEBUG(
        "Unmerging FusionGroup_",
        partitionId,
        ". Expected ",
        expectOpNum,
        " ops, but got ",
        actualOpNum,
        " ops.");
    SubgraphUtils::unmergeSubgraph(subgraphNode);
  }
}

size_t LlgaGraphHelper::countSupportedOps(
    const std::shared_ptr<Graph>& graph) const {
  // TODO: count nodes in top-level block for now
  size_t cnt = 0;
  for (auto* node : graph->block()->nodes()) {
    if (isSupported(node)) {
      cnt++;
    }
  }
  return cnt;
}

std::vector<dnnl::graph::partition> LlgaGraphHelper::getPartitions() const {
  return partitions_;
}

std::map<size_t, Value*> LlgaGraphHelper::getTensorIdToValue() const {
  return tensorIdToValue_;
}

LlgaNodeWrapper::LlgaNodeWrapper(const Node* node)
    : n(const_cast<Node*>(node)) {
  TORCH_CHECK(
      LlgaGraphHelper::isLlgaSubgraph(n), "Cannot wrap a non-LLGA fusion node");
}

void LlgaNodeWrapper::setOpaqueLayout(size_t offset) {
  const auto num_output = n->is(attr::output_layouts).size();
  TORCH_CHECK(
      offset < num_output,
      "Out of range. (Invalid index ",
      offset,
      " for attr::output_layouts with size ",
      num_output,
      ")");
  auto& layouts =
      const_cast<std::vector<int64_t>&>(n->is(Symbol::attr("output_layouts")));
  layouts.at(offset) = OPAQUE_LAYOUT;
}

bool LlgaNodeWrapper::useOpaqueLayout(size_t offset) const {
  const auto num_output = n->is(attr::output_layouts).size();
  TORCH_CHECK(
      offset < num_output,
      "Out of range. (Invalid index ",
      offset,
      " for attr::output_layouts with size ",
      num_output,
      ")");
  return n->is(attr::output_layouts)[offset] == OPAQUE_LAYOUT;
}

} // namespace onednn
} // namespace fuser
} // namespace jit
} // namespace torch_ipex
