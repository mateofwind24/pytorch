import copy
import logging
import re
from typing import cast, Dict, List, Optional, Sequence, Set, Tuple

from ... import ir
from ...config import cuda as inductor_cuda_config
from ...ir import Buffer, CUDATemplateBuffer, FixedLayout, IRNode, Layout
from ..common import ChoiceCaller, IndentedBuffer

from . import cutlass_utils
from .cuda_kernel import CUDATemplateKernel
from .cuda_template import CUTLASSTemplate
from .cutlass_epilogue_gen import (
    CutlassEVTEpilogueArgumentFormatter,
    CutlassEVTEpilogueTypeFormatter,
)

log = logging.getLogger(__name__)

GEMM_TEMPLATE = r"""
{{template.header().getvalue()}}
{{template.globals().getvalue()}}
{{instance_definition}}
// When workspace_size is not a nullptr, populates requested workspace_size and returns.
// Otherwise, computes the Gemm kernel using the given workspace ptr.
extern "C" {
{{kernel_call_signature}} {
  try {
  {{kernel.check_not_null(X)}}
  {{kernel.check_not_null(W)}}
  {{kernel.check_not_null(Bias)}}
  {{kernel.check_not_null(Y)}}
  {% for aux_node in aux_input_nodes %}
  {{kernel.check_not_null(aux_node)}}
  {% endfor %}
  int64_t B = {{kernel.size(Y, 0, -3, default_value=1)}};
  int64_t M = {{kernel.size(X, -2)}};
  int64_t K = {{kernel.size(X, -1)}};
  int64_t N = {{kernel.size(W, -1)}};
  using ElementComputeEpilogue = {{instance_type}}::ElementAccumulator;
  using coord_t = cutlass::gemm::GemmCoord::Index;
  static cutlass::KernelHardwareInfo hw_info;
  if (hw_info.sm_count == 0) {
    // @TODO kadeng: Add support for Multi-GPU machines with heterogeneous SM counts
    // for now we just pick the SM count of the first GPU
    hw_info.sm_count = cutlass::KernelHardwareInfo::query_device_multiprocessor_count(0);
    CUTLASS_TRACE_HOST("Query result for SM count per device: " << hw_info.sm_count);
  }
  {{instance_type}}::Arguments arguments;
  {{template.render_gemm_arguments(argument_template, epilogue_template, should_swap_xw,
                                    X, W, Bias, Y, alpha, beta, kernel, epilogue_args)}}
  {{instance_type}} gemm_op;
  if (workspace_size) {
    *workspace_size = gemm_op.get_workspace_size(arguments);
    return 0;
  }
  {
    auto status = gemm_op.can_implement(arguments);
    CUTLASS_CHECK(status);
  }
#ifdef CUTLASS_DEBUG_TRACE_LEVEL
#if CUTLASS_DEBUG_TRACE_LEVEL == 1
  {
    // Print the maximum number of active blocks per SM for the kernel if CUTLASS_DEBUG_TRACE_LEVEL == 1
    // we don't need a print statement, it's happening inside the function.
    gemm_op.maximum_active_blocks();
  }
#endif
#endif
  {
    auto status = gemm_op.initialize(arguments, workspace, stream);
    CUTLASS_CHECK(status);
  }
  {
    auto status = gemm_op(stream);
    CUTLASS_CHECK(status);
  }
  }
  catch (std::exception& e) {
    std::cerr << "Runtime error: " << e.what() << std::endl;
    return -1;
  }
  catch (...) {
    return -1;
  }
  return 0;
}
}
"""


GEMM_ARGS_CUTLASS_2X = r"""
  int64_t batch_stride_x = {{kernel.stride(X, -3)}};
  int64_t row_stride_x = {{kernel.row_or_column_stride(X)}};
  int64_t batch_stride_w = {{kernel.stride(W, -3)}};
  int64_t row_stride_w = {{kernel.row_or_column_stride(W)}};
  int64_t batch_stride_bias = {{kernel.stride(Bias, -3)}};
  int64_t row_stride_bias = {{kernel.row_or_column_stride(Bias)}};
  int64_t batch_stride_y = {{kernel.stride(Y, -3)}};
  int64_t row_stride_y = {{kernel.row_or_column_stride(Y)}};
  // Initialize GemmUniversalInstance arguments.
  arguments = {
    {{template.gemm_mode()}},  // GemmUniversalMode mode
    {
      static_cast<coord_t>(M),
      static_cast<coord_t>(N),
      static_cast<coord_t>(K)
    },  // GemmCoord problem_size
    {{split_k if split_k > 1 else 'B'}},  // int batch_count
    {ElementComputeEpilogue({{alpha}}), ElementComputeEpilogue({{beta}})},  // typename EpilogueOutputOp::Params epilogue
    {{template.cutlass_type_cast(X, kernel.ptr(X))}},  // void const * ptr_A
    {{template.cutlass_type_cast(W, kernel.ptr(W))}},  // void const * ptr_B
    {{template.cutlass_type_cast(Bias, kernel.ptr(Bias))}},  // void const * ptr_C
    {{template.cutlass_type_cast(Y, kernel.ptr(Y))}},  // void * ptr_D
    batch_stride_x,  // int64_t batch_stride_A
    batch_stride_w,  // int64_t batch_stride_B
    batch_stride_bias,  // int64_t batch_stride_C
    batch_stride_y,  // int64_t batch_stride_D
    row_stride_x,  // typename LayoutA::Stride::LongIndex lda
    row_stride_w,  // typename LayoutB::Stride::LongIndex ldb
    row_stride_bias,  // typename LayoutC::Stride::LongIndex ldc
    row_stride_y,  // typename LayoutC::Stride::LongIndex ldd
  };
"""


GEMM_ARGS_CUTLASS_3X = r"""
  // Initialize GemmUniversal3xInstance arguments.
  arguments = {
    {{template.gemm_mode()}},  // GemmUniversalMode mode
    {
      static_cast<coord_t>({{M}}),
      static_cast<coord_t>({{N}}),
      static_cast<coord_t>(K),
      static_cast<coord_t>(B)
    }, // ProblemShape problem_shape
    {
      {{template.cutlass_type_cast(X, kernel.ptr(X))}},  // ElementA const* ptr_A
      {
        {{template.cute_int(kernel.stride(X, -2), "stride_x0")}},
        {{template.cute_int(kernel.stride(X, -1), "stride_x1")}},
        {{template.cute_int(kernel.stride(X, -3), "batch_stride_x")}}
      },  // StrideA dA
      {{template.cutlass_type_cast(W, kernel.ptr(W))}},  // ElementB const* ptr_B
      {
        {{template.cute_int(kernel.stride(W, -1), "stride_w1")}},
        {{template.cute_int(kernel.stride(W, -2), "stride_w0")}},
        {{template.cute_int(kernel.stride(W, -3), "batch_stride_w")}}
      },  // StrideB dB
    },  // MainloopArguments mainloop
    {{epilogue_arguments}},
    hw_info
  };
"""

GEMM_ARGS_CUTLASS_3X_EPILOGUE = r"""
    // see https://tinyurl.com/4rk89z48
    {
      {{epilogue_args}},  // thread, typename FusionCallbacks::Arguments ( EVT ) or ThreadEpilogueOp::Params (non-EVT )
      {{template.cutlass_type_cast(Bias, kernel.ptr(Bias))}},  // ElementC const* ptr_C
      {
        {{template.cute_int(kernel.stride(Bias, -2, 1), "stride_bias0")}},
        {{template.cute_int(kernel.stride(Bias, -1, 1), "stride_bias1")}},
        {{template.cute_int(kernel.stride(Bias, -3), "batch_stride_bias")}}
      },  // StrideC dC
      {{template.cutlass_type_cast(Y, kernel.ptr(Y))}},  // ElementD const* ptr_D
      {
        {{template.cute_int(kernel.stride(Y, -2), "stride_y0")}},
        {{template.cute_int(kernel.stride(Y, -1), "stride_y1")}},
        {{template.cute_int(kernel.stride(Y, -3), "batch_stride_y")}}
      },  // StrideD dD
    },  // EpilogueArguments epilogue
"""

GEMM_STANDALONE_RUNNER_ADDITIONAL_INCLUDES = r"""
#ifdef GENERATE_STANDALONE_RUNNER
#include "cutlass/util/distribution.h"
#include "cutlass/util/host_tensor.h"
#include "cutlass/util/packed_stride.hpp"
#include "cutlass/util/tensor_view_io.h"
#include "cutlass/util/reference/device/gemm_complex.h"
#include "cutlass/util/reference/device/tensor_compare.h"
#include "cutlass/util/reference/device/tensor_fill.h"
#include <iostream>
#endif
"""

GEMM_STANDALONE_RUNNER_TEMPLATE = r"""
#ifdef GENERATE_STANDALONE_RUNNER
/// Helper to initialize a block of device data
template <class Element>
bool initialize_block(
  cutlass::DeviceAllocation<Element>& block,
  uint64_t seed, float max=1.0, float min=-1.0) {
  if (block.size()<=0) return false;
  Element scope_max(static_cast<Element>(max)), scope_min(static_cast<Element>(min));
  cutlass::reference::device::BlockFillRandomUniform(
    block.get(), block.size(), seed, scope_max, scope_min, 0);

  return true;
}

extern "C" int run_standalone(uint64_t seed) {
    std::cout << "Starting GEMM Standalone test run with seed " << seed << std::endl;
    size_t workspace_size = 0;
    size_t* workspace_size_ptr = &workspace_size;

    using ElementA = {{kernel.cutlass_dtype(X)}};
    using ElementB = {{kernel.cutlass_dtype(W)}};
    using ElementC = {{kernel.cutlass_dtype(Bias, default_dtype='uint8_t')}}; // may not be void
    using ElementD = {{kernel.cutlass_dtype(Y)}};
    {% for aux_node in aux_input_nodes %}
    using Element_{{aux_node.get_name()}} = {{kernel.cutlass_dtype(aux_node)}};
    {% endfor %}

    cutlass::DeviceAllocation<ElementA> X_data({{kernel.max_valid_index(X)+1}});
    initialize_block(X_data, seed++);
    cutlass::DeviceAllocation<ElementB> W_data({{kernel.max_valid_index(W)+1}});
    initialize_block(W_data, seed++);
    cutlass::DeviceAllocation<ElementC> Bias_data({{kernel.max_valid_index(Bias)+1}});
    initialize_block(Bias_data, seed++);
    cutlass::DeviceAllocation<ElementD> Y_data({{kernel.max_valid_index(Y)+1}});
    {% for aux_node in aux_input_nodes %}
    cutlass::DeviceAllocation<Element_{{aux_node.get_name()}}> aux_{{aux_node.get_name()}}_data({{kernel.max_valid_index(aux_node)+1}});
    initialize_block(aux_{{aux_node.get_name()}}_data, seed++);
    {% endfor %}

    cutlass::DeviceAllocation<uint8_t> workspace_data;
    // Call once with workspace_size_ptr set to get workspace size

    std::cout << "Calling once to get workspace size" << std::endl;
    {{test_call_statement}};
    // Allocate workspace if neccessary
    if (workspace_size > 0) {
        workspace_data.reset(workspace_size);
        std::cout << "Allocated workspace size of " << workspace_size << " bytes" << std::endl;
    }
    std::cout << "Calling Kernel as {{test_call_statement}};" << std::endl;
    workspace_size_ptr = nullptr;
    {{test_call_statement}};
    cudaError_t result = cudaDeviceSynchronize();
    if (result != cudaSuccess) {
      std::cerr << "Device synchronize failed with error "
        << cudaGetErrorString(result) << std::endl;
      return result;
    }
    return 0;
}

int main(int argc, char** argv) {
    return run_standalone(1);
}

#endif
"""


class CUTLASSGemmTemplate(CUTLASSTemplate):
    """
    CUTLASS GEMM template, which is used to generate CUTLASS GEMM kernels
    including those which allow flexible fusions with epilogues.
    """

    def __init__(
        self,
        input_nodes: List[Buffer],
        layout: Layout,
        alpha: float,
        beta: float,
        input_reorder: Optional[List[int]] = None,
        can_fuse_epilogue: Optional[bool] = None,
    ):
        """
        Args:
            input_nodes: input nodes of the kernel
            layout: layout of the output node
            alpha: alpha value of the GEMM operation
            beta: beta value of the GEMM operation
            input_reorder: reorder of the input nodes
            can_fuse_epilogue: If set to True, will only list and use operators capable of flexible epilogue fusions.
                               If False, it will not use those. If None, both may be listed, but it will not allow fusions.
                               Defaults to None
        """
        super().__init__("cutlass_gemm", input_nodes, layout, input_reorder)
        self.alpha = alpha
        self.beta = beta
        self.can_fuse_epilogue = can_fuse_epilogue
        assert len(input_nodes) == 2 or len(input_nodes) == 3
        assert self.are_inputs_layout_compatible(
            [node.get_layout() for node in input_nodes]
        )

    def are_inputs_layout_compatible(self, layouts: List[Layout]) -> bool:
        assert len(layouts) == 2 or len(layouts) == 3
        # Check if A and B are compatible
        A_layout, B_layout = layouts[:2]
        if len(A_layout.size) < 1:
            return False
        if len(B_layout.size) < 1:
            return False
        A_size = list(A_layout.size)
        B_size = list(B_layout.size)
        if len(A_size) < 2:
            A_size.insert(0, 1)
        if len(B_size) < 2:
            A_size.insert(1, 1)
        # Are batch dims broadcastable?
        while len(A_size) < len(B_size):
            A_size.insert(0, 1)
        while len(B_size) < len(A_size):
            B_size.insert(0, 1)
        if A_layout.dtype != B_layout.dtype:
            return False
        K = max(A_size[-1], B_size[-2])
        M = A_size[-2]
        N = B_size[-1]
        if K != A_size[-1] and A_size[-1] != 1:
            return False
        if K != B_size[-2] and B_size[-1] != 1:
            return False
        # check batch dim broadcastable
        for i in range(len(A_size) - 2):
            if A_size[i] != B_size[i] and A_size[i] != 1 and B_size[i] != 1:
                return False
        if len(layouts) == 3:
            C_layout = layouts[2]
            C_size = list(C_layout.size)
            while len(C_size) < len(A_size):
                C_size.insert(0, 1)
            # check batch dims
            for i in range(len(A_size) - 2):
                bd = max(A_size[i], B_size[i])
                if bd != C_size[i] and C_size[i] != 1:
                    return False
            if len(C_size) > len(A_size):
                # This may happen if the last elements of C are contiguous and
                # their multiplied size equals the last dim size of B
                if M != C_size[len(A_size) - 2] and C_size[len(A_size) - 2] != 1:
                    return False
                remaining_size = 1
                for i in range(len(A_size) - 1, len(C_size)):
                    remaining_size *= C_size[i]
                if N != remaining_size and remaining_size != 1:
                    return False
                return True
            assert len(C_size) == len(A_size)
            if M != C_size[-2] and C_size[-2] != 1:
                return False
            if N != C_size[-1] and C_size[-1] != 1:
                return False
        return True

    @staticmethod
    def add_cutlass_gemm_choices(
        choices,
        layout,
        input_nodes,
        alpha=1,
        beta=0,
        input_reorder=None,
        fuseable=True,
        non_fuseable=True,
        **extra_kwargs,
    ):
        non_fuseable = non_fuseable and (
            not inductor_cuda_config.cutlass_prefer_evt_capable_ops
        )
        if fuseable:
            cutlass_template_evt = CUTLASSGemmTemplate(
                input_nodes,
                layout,
                alpha=alpha,
                beta=beta,
                input_reorder=input_reorder,
                can_fuse_epilogue=True,
            )
            # This will list only ops capable of EVT fusion
            ops_evt = cutlass_template_evt.gen_ops()
            for op in ops_evt:
                cutlass_template_evt.maybe_append_choice(choices, op=op, **extra_kwargs)
        else:
            ops_evt = []
        if non_fuseable or len(ops_evt) == 0:
            if fuseable:
                # list both fuseable and non-fuseable ops, and treat them all as non-fuseable
                can_fuse_epilogue = False
            else:
                can_fuse_epilogue = None

            cutlass_template = CUTLASSGemmTemplate(
                input_nodes,
                layout,
                alpha=alpha,
                beta=beta,
                input_reorder=input_reorder,
                can_fuse_epilogue=can_fuse_epilogue,
            )
            ops = cutlass_template.gen_ops()
            for op in ops:
                cutlass_template.maybe_append_choice(
                    choices,
                    op=op,
                )
        else:
            ops = []

        if (len(ops_evt) == 0 and fuseable) or (len(ops) == 0 and non_fuseable):
            input_layouts = [node.get_layout() for node in input_nodes]
            input_strides = [node.get_stride() for node in input_nodes]
            output_layout = layout
            warning_msg = f"No suitable Cutlass GEMM configs found, fallbacks used ( {fuseable=}, {non_fuseable=}, {len(ops_evt)=}, {len(ops)=}, {output_layout=}, {input_layouts=}, {input_strides=}"  # noqa: B950
            log.warning(warning_msg)
        log.debug(
            "Added %d Cutlass gemm configs and %d fuseable gemm configs.",
            len(ops),
            len(ops_evt),
        )

    def generate_retune_choices(
        self, ctb: CUDATemplateBuffer, epilogue_nodes: List[IRNode]
    ) -> Sequence[ChoiceCaller]:
        if not self.supports_evt:
            return []
        choices = []
        CUTLASSGemmTemplate.add_cutlass_gemm_choices(
            choices,
            self.layout,
            self.input_nodes,
            alpha=self.alpha,
            beta=self.beta,
            fuseable=True,
            non_fuseable=False,
            epilogue_nodes=epilogue_nodes,
            template_buffer_node=ctb,
        )
        return choices

    def header(self) -> IndentedBuffer:
        res = super().header()
        res.splice(
            """
                #include "cutlass/gemm/gemm.h"
                #include "cutlass/gemm/device/gemm_universal.h"
                #include "cutlass/gemm/device/gemm_universal_adapter.h"
                #include "cutlass/gemm/kernel/gemm_universal.hpp"
                #include "cutlass/gemm/collective/collective_builder.hpp"
                #include "cutlass/epilogue/collective/collective_builder.hpp"
                #include "cutlass/epilogue/collective/default_epilogue.hpp"
                #include "cutlass/epilogue/thread/linear_combination.h"
                #include "cutlass/epilogue/thread/activation.h"
                #include "cutlass/gemm/dispatch_policy.hpp"
                #include "cutlass/gemm/kernel/tile_scheduler.hpp"
                #include "cutlass/util/distribution.h"
                #include "cutlass/util/packed_stride.hpp"
                #include "cutlass/util/tensor_view_io.h"
            """
        )
        if inductor_cuda_config.generate_test_runner:
            res.splice(GEMM_STANDALONE_RUNNER_ADDITIONAL_INCLUDES)
        return res

    @staticmethod
    def cutlass_layout(torch_layout) -> "Optional[cutlass_lib.LayoutType]":  # type: ignore[name-defined]
        assert cutlass_utils.try_import_cutlass()
        import cutlass_library.library as cutlass_lib

        if torch_layout.stride[-1] == 1:
            return cutlass_lib.LayoutType.RowMajor
        elif torch_layout.stride[-2] == 1:
            return cutlass_lib.LayoutType.ColumnMajor
        else:
            return None

    @staticmethod
    def flip_cutlass_layout(
        cutlass_layout: "cutlass_lib.LayoutType",  # type: ignore[name-defined]
    ) -> "cutlass_lib.LayoutType":  # type: ignore[name-defined]
        assert cutlass_utils.try_import_cutlass()
        import cutlass_library.library as cutlass_lib

        if cutlass_layout == cutlass_lib.LayoutType.RowMajor:
            return cutlass_lib.LayoutType.ColumnMajor
        else:
            return cutlass_lib.LayoutType.RowMajor

    @staticmethod
    def layout_match(torch_layout, cutlass_layout) -> bool:
        return CUTLASSGemmTemplate.cutlass_layout(torch_layout) == cutlass_layout

    @staticmethod
    def set_alignment(torch_layout, op_element) -> bool:
        alignment = cutlass_utils.get_max_alignment(torch_layout)
        if alignment < op_element.alignment:
            return False
        else:
            op_element.alignment = alignment
            return True

    @staticmethod
    def has_tma_epilogue(op) -> bool:
        assert cutlass_utils.try_import_cutlass()
        import cutlass_library.library as cutlass_lib

        result = False
        if op.gemm_kind == cutlass_lib.GemmKind.Universal3x:
            epilogue_schedule_str = str(op.epilogue_schedule).split(".")[-1]
            result = epilogue_schedule_str.lower().startswith("tma")
        return result

    @staticmethod
    def supports_evt(op: "cutlass_library.gemm_op.GemmOperation") -> bool:  # type: ignore[name-defined]
        """
        returns True if the op is capable of flexible epilogue fusions
        using epilogue visitor trees.

        See https://github.com/NVIDIA/cutlass/blob/e01b9b5029b7caca5a43c29f7d2714d7cf1dcae8/examples/49_hopper_gemm_with_collective_builder/49_collective_builder.cu#L283-L285 # noqa: B950
        """
        assert cutlass_utils.try_import_cutlass()
        import cutlass_library.library as cutlass_lib

        if op.gemm_kind != cutlass_lib.GemmKind.Universal3x:
            return False
        if op.epilogue_schedule not in (
            cutlass_lib.EpilogueScheduleType.TmaWarpSpecialized,
            cutlass_lib.EpilogueScheduleType.TmaWarpSpecializedCooperative,
        ):
            return False

        return True

    def render_evt_epilogue_declaration(
        self,
        template_output_node_name: str,
        evt_type_name: str,
        epilogue_nodes: List[IRNode],
        Bias: Optional[Buffer] = None,
        gemm_output_layout: Layout = None,
    ) -> str:
        """Generates the epilogue for the EVT epilogue fusion"""
        if len(self.input_nodes) > 2:  # if no bias arg passed in at construction
            pre_fused_addmm_evt = (
                CutlassEVTEpilogueTypeFormatter.create_pre_fused_addmm_evt_type()
            )
        else:
            pre_fused_addmm_evt = None
        return CutlassEVTEpilogueTypeFormatter.ir_to_evt_string(
            template_output_node_name,
            evt_type_name,
            epilogue_nodes,
            pre_fused_addmm_evt,
            Bias.get_name() if Bias is not None else None,
            gemm_output_layout=gemm_output_layout,
        )

    def define_gemm_instance(
        self,
        op: "cutlass_library.gemm_op.GemmOperation",  # type: ignore[name-defined]
        output_buffer_name: str,
        epilogue_nodes: Optional[List[IRNode]] = None,
        Bias: Optional[Buffer] = None,
        gemm_output_layout: Optional[Layout] = None,
    ) -> Tuple[str, str]:
        assert cutlass_utils.try_import_cutlass()
        import cutlass_library.gemm_operation as cutlass_gemm_op
        import cutlass_library.library as cutlass_lib

        from torch._inductor.codegen.cuda.cutlass_lib_extensions.gemm_operation_extensions import (
            EmitGemmUniversal3xInstanceWithEVT,
        )

        if epilogue_nodes is None:
            epilogue_nodes = []

        if op.gemm_kind == cutlass_lib.GemmKind.Universal3x:
            use_evt = self.supports_evt(op) and (
                (Bias is not None) or (len(epilogue_nodes) > 0)
            )
            if use_evt:
                emitter = EmitGemmUniversal3xInstanceWithEVT()
                assert gemm_output_layout is not None
                op.epilogue_functor = lambda epilogue_functor_type_name: self.render_evt_epilogue_declaration(
                    output_buffer_name,
                    epilogue_functor_type_name,
                    epilogue_nodes,
                    Bias=Bias,
                    gemm_output_layout=gemm_output_layout,
                )
            else:
                emitter = cutlass_gemm_op.EmitGemmUniversal3xInstance()
            op_def = emitter.emit(op)
            pattern = re.compile(r"\s*struct\s(.*?)\s:")
            decl = [line for line in op_def.split("\n") if "struct " in line][-1]
        else:
            if epilogue_nodes is not None and len(epilogue_nodes) > 0:
                raise RuntimeError(
                    "EVT epilogue fusion is not supported for Cutlass 2.x ops."
                )
            emitter = cutlass_gemm_op.EmitGemmInstance()
            op_def = emitter.emit(op)
            op_def = op_def.replace(
                "cutlass::gemm::device::Gemm", "cutlass::gemm::device::GemmUniversal"
            )
            op_def = op_def.replace("false,", "")
            pattern = re.compile(r"\s*using\s(.*?)\s=")
            decl = op_def.split("\n")[2]
        match = pattern.match(decl)
        if match is None:
            raise RuntimeError("Invalid Gemm config: \n" + op_def)
        op_type = match.groups()[0]
        if op.gemm_kind == cutlass_lib.GemmKind.Universal3x:
            op_def += f"\n  using {op_type}_device_type = cutlass::gemm::device::GemmUniversalAdapter<{op_type}>;\n"
            op_type = f"{op_type}_device_type"
        return op_def, op_type

    @staticmethod
    def should_swap_XW(
        bias: IRNode,
        beta: float,
    ) -> bool:
        return False

        # TODO(ipiszy): Check whether it's necessary to swap X/W.
        # strides = bias.get_stride()
        # if strides[-1] != 1:
        #     return True
        # for stride in strides[:-1]:
        #     if stride != 0:
        #         return True
        # return False

    @staticmethod
    def swap_XW(
        op: "cutlass_library.gemm_op.GemmOperation",  # type: ignore[name-defined]
    ) -> "cutlass_library.gemm_op.GemmOperation":  # type: ignore[name-defined]
        # Swap X and W in GemmOperation.
        new_op = copy.deepcopy(op)
        new_op.A.layout = CUTLASSGemmTemplate.flip_cutlass_layout(new_op.A.layout)
        new_op.B.layout = CUTLASSGemmTemplate.flip_cutlass_layout(new_op.B.layout)
        new_op.A, new_op.B = new_op.B, new_op.A
        new_op.C.layout = CUTLASSGemmTemplate.flip_cutlass_layout(new_op.C.layout)
        new_op.D.layout = CUTLASSGemmTemplate.flip_cutlass_layout(new_op.D.layout)
        return new_op

    def fix_op_layout(
        self,
        op: "cutlass_library.gemm_op.GemmOperation",  # type: ignore[name-defined]
        X: Buffer,
        W: Buffer,
        Bias: Optional[Buffer],
        Y: Buffer,
    ) -> "cutlass_library.gemm_op.GemmOperation":  # type: ignore[name-defined]
        # This is a workaround to deal with cases where the input layouts have changed
        # between autotuning and rendering. This happens if the inputs layout
        # are FlexibleLayout instances. In this case, we need to update the
        # op's input layouts. It is a hack, because now the op
        # we benchmarked is not the same as the op we render,
        # but there is no simple way to fix this in the autotuner, since that would
        # potentially disable other optimizations.
        # @TODO kadeng: This is a workaround. Find a better way to solve the issue of dealing with FlexibleLayout during autotuning.
        a_layout = X.get_layout()
        b_layout = W.get_layout()
        c_layout = Bias.get_layout() if Bias is not None else None
        d_layout = Y.get_layout()
        all_match = all(
            CUTLASSGemmTemplate.layout_match(buf.get_layout(), op_layout)
            for buf, op_layout in zip(
                (X, W, Bias, Y),
                (op.A.layout, op.B.layout, op.C.layout, op.D.layout),
            )
            if buf is not None
        )
        if all_match:
            return op
        # log.warning(
        #    f"Cutlass GEMM Layout change: Input and/or output layouts have changed between autotuning and call to render on {self}. Applying workaround. This can lead to suboptimal performance."  # noqa: G004, B950
        # )
        new_op = copy.deepcopy(op)

        if a_layout is not None:
            new_op.A.layout = CUTLASSGemmTemplate.cutlass_layout(a_layout)
        if b_layout is not None:
            new_op.B.layout = CUTLASSGemmTemplate.cutlass_layout(b_layout)
        if c_layout is not None:
            new_op.C.layout = CUTLASSGemmTemplate.cutlass_layout(c_layout)
            new_op.C.element = cutlass_utils.torch_dtype_to_cutlass_type(c_layout.dtype)
        if d_layout is not None:
            new_op.D.layout = CUTLASSGemmTemplate.cutlass_layout(d_layout)
        return new_op

    def filter_op(
        self,
        op: "cutlass_library.gemm_op.GemmOperation",  # type: ignore[name-defined]
    ) -> "cutlass_library.gemm_op.GemmOperation":  # type: ignore[name-defined]
        assert cutlass_utils.try_import_cutlass()
        import cutlass_library.library as cutlass_lib

        # Skip simt kernels
        if (
            op.tile_description.math_instruction.opcode_class
            == cutlass_lib.OpcodeClass.Simt
        ):
            return None
        supports_evt: bool = self.supports_evt(op)
        if (self.can_fuse_epilogue is not None) and (
            self.can_fuse_epilogue != supports_evt
        ):
            return None
        # StreamK seems to lead to extreme spilling for certain shapes
        # and might take forever during autotuning. @TODO kadeng: investigate
        if op.tile_scheduler == cutlass_lib.TileSchedulerType.StreamK:
            return None
        # Only keep GemmUniversal kernels
        if op.gemm_kind not in {
            cutlass_lib.GemmKind.Universal,
            cutlass_lib.GemmKind.Universal3x,
        }:
            return None
        # Filter ops by dtypes.
        X = self.input_nodes[0]
        W = self.input_nodes[1]
        accumulator_torch_dtype = cutlass_utils.get_accumulator_dtype(
            [X.get_dtype(), W.get_dtype()],
        )
        if not (
            cutlass_utils.dtype_match(X.get_dtype(), op.A.element)
            and cutlass_utils.dtype_match(W.get_dtype(), op.B.element)
            and cutlass_utils.dtype_match(
                self.output_node.get_layout().dtype, op.C.element
            )
            and cutlass_utils.dtype_match(
                accumulator_torch_dtype, op.accumulator_type()
            )
        ):
            return None

        # Filter ops by input layouts.
        if not (
            self.layout_match(X.get_layout(), op.A.layout)
            and self.layout_match(W.get_layout(), op.B.layout)
        ):
            return None

        # Update op.
        op = copy.deepcopy(op)

        # Set output layout.
        op.D.layout = CUTLASSGemmTemplate.cutlass_layout(self.output_node.get_layout())

        # Filter ops by alignments and set alignments.
        if not (
            self.set_alignment(X.get_layout(), op.A)
            and self.set_alignment(W.get_layout(), op.B)
            and self.set_alignment(self.output_node.get_layout(), op.D)
        ):
            return None

        # Set epilogue.
        # TODO: update epilogue functor according to epilogues.
        op.element_epilogue = op.accumulator_type()

        # Set bias layout and alignment.
        if len(self.input_nodes) >= 3 and self.input_nodes[2] is not None:
            Bias = self.input_nodes[2]
            bias_layout = CUTLASSGemmTemplate.cutlass_layout(Bias.get_layout())
            if op.gemm_kind != cutlass_lib.GemmKind.Universal3x:
                if bias_layout != op.D.layout:
                    # For cutlass2, bias and output layout must match
                    return None
            else:
                op.C.layout = bias_layout
            if not self.set_alignment(Bias.get_layout(), op.C):
                return None
        else:
            if op.gemm_kind == cutlass_lib.GemmKind.Universal3x:
                op.C.element = cutlass_lib.DataType.void
            else:
                op.C.layout = op.D.layout
        return op

    def gen_ops(self) -> "List[cutlass_gemm_op.GemmOperation]":  # type: ignore[name-defined]
        assert cutlass_utils.try_import_cutlass()
        import cutlass_library.gemm_operation as cutlass_gemm_op
        import cutlass_library.library as cutlass_lib

        ops = cutlass_utils.gen_ops()[cutlass_lib.OperationKind.Gemm]
        res: Dict[str, cutlass_gemm_op.GemmOperation] = dict()
        num_3x_ops = 0
        num_2x_ops = 0
        for op_dict in ops.values():
            for op_list in op_dict.values():
                for op in op_list:
                    assert isinstance(op, cutlass_gemm_op.GemmOperation)
                    filter_res = self.filter_op(op)
                    if (
                        filter_res is not None
                        and res.get(filter_res.configuration_name(), None) is None
                    ):
                        res[filter_res.configuration_name()] = filter_res
        for op in res.values():
            if op.gemm_kind == cutlass_lib.GemmKind.Universal3x:
                num_3x_ops += 1
            else:
                num_2x_ops += 1
        log.debug(
            "Got cutlass configs: total number of ops: %d, "
            "total number of 3x ops: %d, total number of 2x ops: %d",
            len(res),
            num_3x_ops,
            num_2x_ops,
        )
        return list(res.values())[: inductor_cuda_config.cutlass_max_profiling_configs]

    def gemm_mode(self) -> str:
        sizes = self.output_node.get_size()
        if len(sizes) > 2:
            return "cutlass::gemm::GemmUniversalMode::kBatched"
        else:
            return "cutlass::gemm::GemmUniversalMode::kGemm"

    def render_gemm_arguments(
        self,
        argument_template: str,
        epilogue_template: str,
        should_swap_xw: bool,
        X: IRNode,
        W: IRNode,
        Bias: IRNode,
        Y: IRNode,
        alpha: float,
        beta: float,
        kernel: CUDATemplateKernel,
        epilogue_args,
    ) -> str:
        options = dict(
            alpha=alpha,
            beta=beta,
            X=X,
            W=W,
            Y=Y,
            Bias=Bias,
            template=self,
            kernel=kernel,
            M="M",
            N="N",
            epilogue_args=epilogue_args,
        )

        if epilogue_template is not None:
            if should_swap_xw:
                # Swap
                def clone_with_transposed_stride(node: IRNode) -> IRNode:
                    old_layout = node.get_layout()
                    new_stride = list(old_layout.stride)
                    new_stride[-2], new_stride[-1] = new_stride[-1], new_stride[-2]
                    new_layout = FixedLayout(
                        old_layout.device,
                        old_layout.dtype,
                        list(old_layout.size),
                        new_stride,
                        old_layout.offset,
                    )
                    return Buffer(node.get_name(), new_layout)

                new_X = clone_with_transposed_stride(X)
                new_W = clone_with_transposed_stride(W)
                new_Bias = clone_with_transposed_stride(Bias)
                new_Y = clone_with_transposed_stride(Y)
                options["X"], options["W"], options["Bias"], options["Y"] = (
                    new_W,
                    new_X,
                    new_Bias,
                    new_Y,
                )
                options["M"], options["N"] = "N", "M"

            epilogue_arguments = self._template_from_string(epilogue_template).render(
                **options
            )
            arguments = self._template_from_string(argument_template).render(
                epilogue_arguments=epilogue_arguments, **options
            )
        else:
            arguments = self._template_from_string(GEMM_ARGS_CUTLASS_2X).render(
                split_k=1, **options
            )
        return arguments

    def get_additional_input_nodes(
        self,
        cuda_template_buffer: CUDATemplateBuffer,
        epilogue_nodes: List[ir.ComputedBuffer],
    ):
        template_buffer_names: Set[str] = cuda_template_buffer.get_read_names()
        fused_reading_buffer_names: Set[str] = set(template_buffer_names)

        for epilogue_node in epilogue_nodes:
            fused_reading_buffer_names.update(epilogue_node.get_read_names())

        # We need to remove all reads which were written as intermediate results
        fused_written_names = set()
        fused_written_names.add(cuda_template_buffer.get_name())
        for epilogue_node in epilogue_nodes:
            fused_written_names.add(epilogue_node.get_name())
        fused_reading_buffer_names -= fused_written_names

        if len(fused_reading_buffer_names) > len(template_buffer_names):
            # Check that the layout of the additional input is compatible
            added_names = sorted(fused_reading_buffer_names - template_buffer_names)

            from torch._inductor.virtualized import V

            added_nodes = [V.graph.get_buffer(added_name) for added_name in added_names]
            return added_nodes
        return []

    def render(  # type: ignore[override]
        self,
        kernel: CUDATemplateKernel,
        op: "cutlass_gemm_op.GemmOperation" = None,  # type: ignore[name-defined]
        template_buffer_node: Optional[CUDATemplateBuffer] = None,
        epilogue_nodes: Optional[List[IRNode]] = None,
        **kwargs,
    ) -> str:
        if epilogue_nodes is not None and len(epilogue_nodes) > 0:
            assert self.can_fuse_epilogue and CUTLASSGemmTemplate.supports_evt(
                op
            ), "op does not support EVT epilogue fusion"
            assert (
                template_buffer_node is not None
            ), "Template node is required for epilogue fusion"
            assert isinstance(
                template_buffer_node, CUDATemplateBuffer
            ), f"Template node has to be a CUDATemplateBuffer, is type {type(template_buffer_node)}"
            assert (
                template_buffer_node.name is not None
            ), "Output node has to be a Buffer with a name"
            # This is the name of the output of the Matmul, before epilogues are applied.
            # it is not necessarily materialized in global memory if we have an epilogue

        template_output_node_name = (
            template_buffer_node.name if template_buffer_node is not None else None
        )
        if epilogue_nodes is None:
            epilogue_nodes = []

        assert cutlass_utils.try_import_cutlass()
        import cutlass_library.gemm_operation as cutlass_gemm_op
        import cutlass_library.library as cutlass_lib

        assert isinstance(
            op, cutlass_gemm_op.GemmOperation
        ), "op argument is required and has to be an instance of GemmOperation"
        if template_buffer_node is not None:
            self.output_node = template_buffer_node
        if epilogue_nodes is not None and len(epilogue_nodes) > 0:
            self.output_node = cast(Buffer, epilogue_nodes[-1])

        assert len(self.input_nodes) >= 2 and self.output_node is not None
        X, W = self.input_nodes[0], self.input_nodes[1]
        assert isinstance(X.layout, FixedLayout), "X.layout is not fixed"
        assert isinstance(W.layout, FixedLayout), "W.layout is not fixed"
        Y = self.output_node
        Bias, aux_input_nodes = self.determine_additional_inputs(
            epilogue_nodes, template_buffer_node
        )

        # Define Kernel call signature, including potentially auxiliary input nodes
        # required for the fused epilogue nodes
        # Important: This step also populates Kernel name to node mapping data structures,
        # which are required further below ( for example by CutlassEVTEpilogueArgumentFormatter and
        # the template renderer )
        inputs = [X, W, Bias] + aux_input_nodes
        names = (
            ["X", "W", "Bias"]
            + ["aux_" + n.get_name() for n in aux_input_nodes]
            + ["Y"]
        )
        names_str = ",".join(names)
        if self.input_reorder is not None:
            input_reorder = self.input_reorder + list(range(3, len(aux_input_nodes)))
        else:
            input_reorder = None
        kernel_call_signature = kernel.def_kernel(
            inputs=inputs, outputs=[Y], names_str=names_str, input_reorder=input_reorder
        )
        test_call_statement = self.test_call_statement(kernel, inputs, names_str)
        # The layouts might have changed between autotuning and this call if they were FlexibleLayout
        # we need to adapt, which might lead to suboptimal performance.
        # Also there might be a Bias / additional input node which was not present during autotuning
        # @TODO kadeng: Find a way to solve this better
        op = self.fix_op_layout(op, X, W, Bias, Y)
        epilogue_template: Optional[str] = None
        should_swap_xw: bool = False
        epilogue_args = f"{{ElementComputeEpilogue({self.alpha}), ElementComputeEpilogue({self.beta})}}"
        if op.gemm_kind == cutlass_lib.GemmKind.Universal3x:
            if Bias is not None and self.has_tma_epilogue(op):
                if self.should_swap_XW(Bias, self.beta):
                    # TMA epilogue requires bias vector in column major to get best perf.
                    op = self.swap_XW(op)
                    should_swap_xw = True
            if self.supports_evt(op):
                if len(self.input_nodes) > 2:  # if bias arg passed in at construction
                    pre_fused_evt_args = CutlassEVTEpilogueArgumentFormatter.create_pre_fused_addmm_arg_str(
                        self.alpha, self.beta
                    )
                else:
                    pre_fused_evt_args = None
                epilogue_args = (
                    CutlassEVTEpilogueArgumentFormatter.ir_to_evt_argument_string(
                        cast(str, template_output_node_name),
                        epilogue_nodes,
                        pre_fused_evt_args,
                        Bias.get_name() if Bias is not None else None,
                        gemm_output_layout=self.output_node.get_layout(),
                    )
                )
            epilogue_template = GEMM_ARGS_CUTLASS_3X_EPILOGUE
            argument_template = GEMM_ARGS_CUTLASS_3X
        else:
            # TODO: Support split_k.
            argument_template = GEMM_ARGS_CUTLASS_2X

        instance_definition, instance_type = self.define_gemm_instance(
            op,
            cast(str, template_output_node_name),
            epilogue_nodes,
            Bias=Bias,
            gemm_output_layout=self.output_node.get_layout(),
        )

        options = dict(
            alpha=self.alpha,
            beta=self.beta,
            X=X,
            W=W,
            Y=Y,
            kernel_call_signature=kernel_call_signature,
            Bias=Bias,
            epilogue_template=epilogue_template,
            argument_template=argument_template,
            should_swap_xw=should_swap_xw,
            template=self,
            kernel=kernel,
            instance_definition=instance_definition,
            instance_type=instance_type,
            input_reorder=self.input_reorder,
            epilogue_args=epilogue_args,
            aux_input_nodes=aux_input_nodes,
            test_call_statement=test_call_statement,
        )
        res = self._template_from_string(GEMM_TEMPLATE).render(**options)
        if inductor_cuda_config.generate_test_runner:
            test_runner_code = self._template_from_string(
                GEMM_STANDALONE_RUNNER_TEMPLATE
            ).render(**options)
            res += "\n\n" + test_runner_code
        return res

    def determine_additional_inputs(
        self, epilogue_nodes=None, template_buffer_node=None, **kwargs
    ):
        """Determines Bias and auxiliary input nodes for the fused epilogue nodes
        based on existing input nodes (including their Layout), presence of a Bias node
         and additional nodes that are read by the epilogue nodes
        """
        X, W = self.input_nodes[:2]
        Bias = None if len(self.input_nodes) == 2 else self.input_nodes[2]
        aux_input_nodes: List[ir.Buffer] = []
        if (
            template_buffer_node is not None
            and epilogue_nodes is not None
            and len(epilogue_nodes) > 0
        ):
            additional_input_nodes: List[
                ir.ComputedBuffer
            ] = template_buffer_node.get_additional_input_nodes(epilogue_nodes)
            aux_input_nodes = additional_input_nodes
            if Bias is None:
                # If we want to cast one of the additional inputs as Bias
                for i in range(len(additional_input_nodes)):
                    MaybeBias = additional_input_nodes[i]
                    if len(MaybeBias.get_stride()) < 2:
                        continue
                    if not self.are_inputs_layout_compatible(
                        [X.get_layout(), W.get_layout(), MaybeBias.get_layout()]
                    ):
                        continue
                    aux_input_nodes = (
                        additional_input_nodes[:i] + additional_input_nodes[i + 1 :]
                    )
                    Bias = MaybeBias
                    break
            for i, aux_input_node in enumerate(aux_input_nodes):
                assert (
                    aux_input_node.get_name() is not None
                ), f"Auxiliary input node {i} has to have a name"
                assert self.are_inputs_layout_compatible(
                    [X.get_layout(), W.get_layout(), aux_input_node.get_layout()]
                ), f"Input layouts are not compatible with auxiliary input {i}: {aux_input_node}"
        return Bias, aux_input_nodes

    def test_call_statement(
        self,
        kernel,
        input_nodes,
        names_str: str = "",
    ) -> str:
        _, __, arg_types = kernel.args.cpp_argdefs()
        arg_names = [name.strip() for name in names_str.strip().split(",")]
        if input_nodes[2] is None:
            del arg_names[2]
        arguments = [
            f"(({arg_type}){arg_name}_data.get())"
            for arg_type, arg_name in zip(arg_types, arg_names)
        ]
        return f"{kernel.kernel_name}({', '.join(arguments)}, workspace_size_ptr, (uint8_t*)workspace_data.get(), 0);"
