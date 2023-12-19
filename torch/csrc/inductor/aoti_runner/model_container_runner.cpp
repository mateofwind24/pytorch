#if !defined(C10_MOBILE) && !defined(ANDROID)
#include <ATen/DynamicLibrary.h>

#include <torch/csrc/inductor/aoti_runner/model_container_runner.h>
#include <torch/csrc/inductor/aoti_torch/tensor_converter.h>

namespace torch::inductor {

AOTIModelContainerRunner::AOTIModelContainerRunner(
    const std::string& model_so_path,
    size_t num_models,
    bool is_cpu,
    const std::string& cubin_dir) {
  model_so_ = std::make_unique<at::DynamicLibrary>(model_so_path.c_str());
  TORCH_CHECK(model_so_, "Failed to load model: ", model_so_path);
  create_func_ = reinterpret_cast<decltype(create_func_)>(
      model_so_->sym("AOTInductorModelContainerCreate"));
  delete_func_ = reinterpret_cast<decltype(delete_func_)>(
      model_so_->sym("AOTInductorModelContainerDelete"));
  get_num_outputs_func_ = reinterpret_cast<decltype(get_num_outputs_func_)>(
      model_so_->sym("AOTInductorModelContainerGetNumOutputs"));
  run_func_ = reinterpret_cast<decltype(run_func_)>(
      model_so_->sym("AOTInductorModelContainerRun"));
  update_constant_buffer_func_ =
      reinterpret_cast<decltype(update_constant_buffer_func_)>(
          model_so_->sym("AOTInductorModelContainerUpdateConstantBuffer"));
  update_inactive_constant_buffer_func_ =
      reinterpret_cast<decltype(update_inactive_constant_buffer_func_)>(
          model_so_->sym(
              "AOTInductorModelContainerUpdateInactiveConstantBuffer"));
  swap_constant_buffer_func_ =
      reinterpret_cast<decltype(swap_constant_buffer_func_)>(
          model_so_->sym("AOTInductorModelContainerSwapConstantBuffer"));
  get_call_spec_func_ = reinterpret_cast<decltype(get_call_spec_func_)>(
      model_so_->sym("AOTInductorModelContainerGetCallSpec"));

  AOTI_RUNTIME_ERROR_CODE_CHECK(create_func_(
      &container_handle_,
      num_models,
      is_cpu,
      cubin_dir.empty() ? nullptr : cubin_dir.c_str()));

  /*
  #ifdef FBCODE_CAFFE2
    std::string extern_kernel_nodes_path =
        model_so_path.substr(0, model_so_path.size() - 3) + ".json";
    if (fileExists(extern_kernel_nodes_path)) {
      proxy_executor_handle_ =
          std::make_unique<torch::aot_inductor::FbProxyExecutor>(
              extern_kernel_nodes_path, is_cpu);
  #endif
  */
}

AOTIModelContainerRunner::~AOTIModelContainerRunner() {
  AOTIRuntimeError result = delete_func_(container_handle_);
  TORCH_CHECK(
      result == AOTI_RUNTIME_SUCCESS, "AOTInductorModelContainerDelete failed");
}

std::vector<at::Tensor> AOTIModelContainerRunner::run(
    std::vector<at::Tensor>& inputs,
    AOTInductorStreamHandle cuda_stream_handle) {
  auto input_handles =
      torch::aot_inductor::unsafe_alloc_new_handles_from_tensors(inputs);

  // For outputs, we only allocate a vector to hold returned tensor handles,
  // not allocating the actual output tensor storage here
  size_t num_outputs = 0;
  AOTI_RUNTIME_ERROR_CODE_CHECK(
      get_num_outputs_func_(container_handle_, &num_outputs));
  std::vector<AtenTensorHandle> output_handles(num_outputs);

  AOTI_RUNTIME_ERROR_CODE_CHECK(run_func_(
      container_handle_,
      input_handles.data(),
      input_handles.size(),
      output_handles.data(),
      output_handles.size(),
      cuda_stream_handle,
      proxy_executor_handle_));

  return torch::aot_inductor::alloc_tensors_by_stealing_from_handles(
      output_handles.data(), output_handles.size());
}

void AOTIModelContainerRunner::update_constant_buffer(
    const TensorConstantMap& const_map,
    bool use_inactive,
    bool check_full_update) {
  AOTI_RUNTIME_ERROR_CODE_CHECK(update_constant_buffer_func_(
      container_handle_,
      (AOTInductorConstantMapHandle)&const_map,
      use_inactive,
      check_full_update));
}

void AOTIModelContainerRunner::update_inactive_constant_buffer(
    const TensorConstantMap& const_map) {
  AOTI_RUNTIME_ERROR_CODE_CHECK(update_inactive_constant_buffer_func_(
      container_handle_, (AOTInductorConstantMapHandle)&const_map));
}

void AOTIModelContainerRunner::swap_constant_buffer() {
  AOTI_RUNTIME_ERROR_CODE_CHECK(swap_constant_buffer_func_(container_handle_));
}

std::vector<std::string> AOTIModelContainerRunner::get_call_spec() {
  const char* in_spec;
  const char* out_spec;
  AOTI_RUNTIME_ERROR_CODE_CHECK(
      get_call_spec_func_(container_handle_, &in_spec, &out_spec));
  return {in_spec, out_spec};
}

} // namespace torch::inductor
#endif
