from __future__ import annotations

import base64
import copy
import logging
import pickle
from typing import Any, List, Optional, Tuple

import torch
from torch_tensorrt._Device import Device
from torch_tensorrt.dynamo._settings import CompilationSettings

logger = logging.getLogger(__name__)

SerializedTensorRTEngineFmt = Tuple[
    str, str, str, bytes, str, str, str, bytes
]  # Defined in //core/runtime/register_jit_hooks.cpp
SerializedTorchTensorRTModuleFmt = Tuple[
    str, Optional[SerializedTensorRTEngineFmt], List[str], List[str]
]

ABI_TARGET_IDX = torch.ops.tensorrt.ABI_TARGET_IDX()  # 0
NAME_IDX = torch.ops.tensorrt.NAME_IDX()  # 1
DEVICE_IDX = torch.ops.tensorrt.DEVICE_IDX()  # 2
ENGINE_IDX = torch.ops.tensorrt.ENGINE_IDX()  # 3
INPUT_BINDING_NAMES_IDX = torch.ops.tensorrt.INPUT_BINDING_NAMES_IDX()  # 4
OUTPUT_BINDING_NAMES_IDX = torch.ops.tensorrt.OUTPUT_BINDING_NAMES_IDX()  # 5
HW_COMPATIBLE_IDX = torch.ops.tensorrt.HW_COMPATIBLE_IDX()  # 6
SERIALIZED_METADATA_IDX = torch.ops.tensorrt.SERIALIZED_METADATA_IDX()  # 7
SERIALIZATION_LEN = torch.ops.tensorrt.SERIALIZATION_LEN()  # 8


class TorchTensorRTModule(torch.nn.Module):  # type: ignore[misc]
    """TorchTensorRTModule is a PyTorch module which encompasses an arbitrary TensorRT Engine.

    This module is backed by the Torch-TensorRT runtime and is fully compatible with both
    FX / Python deployments (just ``import torch_tensorrt`` as part of the application) as
    well as TorchScript / C++ deployments since TorchTensorRTModule can be passed to ``torch.jit.trace``
    and then saved.

    The forward function is simpily forward(*args: torch.Tensor) -> Tuple[torch.Tensor] where
    the internal implementation is ``return Tuple(torch.ops.tensorrt.execute_engine(list(inputs), self.engine))``

    > Note: TorchTensorRTModule only supports engines built with explicit batch

    Attributes:
        name (str): Name of module (for easier debugging)
        engine (torch.classes.tensorrt.Engine): Torch-TensorRT TensorRT Engine instance, manages [de]serialization, device configuration, profiling
        input_binding_names (List[str]): List of input TensorRT engine binding names in the order they would be passed to the TRT modules
        output_binding_names (List[str]): List of output TensorRT engine binding names in the order they should be returned
    """

    def __init__(
        self,
        serialized_engine: Optional[bytes] = None,
        name: str = "",
        input_binding_names: Optional[List[str]] = None,
        output_binding_names: Optional[List[str]] = None,
        settings: CompilationSettings = CompilationSettings(),
    ):
        """Takes a name, target device, serialized TensorRT engine, and binding names / order and constructs
        a PyTorch ``torch.nn.Module`` around it.

        If binding names are not provided, it is assumed that the engine binding names follow the following convention:

            - [symbol].[index in input / output array]
                - ex. [x.0, x.1, x.2] -> [y.0]

        Arguments:
            name (str): Name for module
            serialized_engine (bytearray): Serialized TensorRT engine in the form of a bytearray
            input_binding_names (List[str]): List of input TensorRT engine binding names in the order they would be passed to the TRT modules
            output_binding_names (List[str]): List of output TensorRT engine binding names in the order they should be returned
            target_device (torch_tensorrt.Device): Device to instantiate TensorRT engine on. Must be a compatible device i.e. same GPU model / compute capability as was used to build the engine
            hardware_compatible (bool): If the engine has be built with the hardware compatibility feature enabled

        Example:

            .. code-block:: py

                with io.BytesIO() as engine_bytes:
                    engine_bytes.write(trt_engine.serialize())
                    engine_str = engine_bytes.getvalue()

                trt_module = TorchTensorRTModule(
                    engine_str,
                    name="my_module",
                    input_binding_names=["x"],
                    output_binding_names=["output"],
                )

        """
        super(TorchTensorRTModule, self).__init__()

        if not isinstance(serialized_engine, bytearray):
            ValueError("Expected serialized engine as bytearray")

        self.input_binding_names = (
            input_binding_names if input_binding_names is not None else []
        )
        self.output_binding_names = (
            output_binding_names if output_binding_names is not None else []
        )
        self.name = name
        target_device = (
            settings.device if settings.device is not None else Device._current_device()
        )
        self.hardware_compatible = settings.hardware_compatible
        self.settings = copy.deepcopy(settings)
        if serialized_engine is not None:
            self.engine = torch.classes.tensorrt.Engine(
                [
                    torch.ops.tensorrt.ABI_VERSION(),
                    self.name + "_engine" if self.name != "" else "tensorrt_engine",
                    target_device._to_serialized_rt_device(),
                    serialized_engine,
                    TorchTensorRTModule._pack_binding_names(self.input_binding_names),
                    TorchTensorRTModule._pack_binding_names(self.output_binding_names),
                    str(int(self.hardware_compatible)),
                    self.encode_metadata(settings),
                ]
            )
        else:
            self.engine = None

    def encode_metadata(self, settings: Any) -> str:
        settings = copy.deepcopy(settings)
        settings.torch_executed_ops = {
            f"torch.ops.{op.__str__()}" for op in settings.torch_executed_ops
        }
        dumped_settings = pickle.dumps(settings)
        encoded_settings = base64.b64encode(dumped_settings).decode("utf-8")
        return encoded_settings

    @staticmethod
    def decode_metadata(encoded_settings: bytes) -> Any:
        dumped_settings = base64.b64decode(encoded_settings.encode("utf-8"))
        settings = pickle.loads(dumped_settings)
        settings.torch_executed_ops = {eval(op) for op in settings.torch_executed_ops}
        return settings

    def get_extra_state(self) -> SerializedTorchTensorRTModuleFmt:
        return (
            self.name,
            self.engine.__getstate__() if self.engine is not None else None,
            self.input_binding_names,
            self.output_binding_names,
        )

    def set_extra_state(self, state: SerializedTorchTensorRTModuleFmt) -> None:
        self.name = state[0]
        if state[1] is not None:
            serialized_engine_info: SerializedTensorRTEngineFmt = state[1]

            serialized_engine = base64.b64decode(serialized_engine_info[3])
            self.engine = torch.classes.tensorrt.Engine(
                [
                    serialized_engine_info[ABI_TARGET_IDX],
                    serialized_engine_info[NAME_IDX],
                    serialized_engine_info[DEVICE_IDX],
                    serialized_engine,
                    serialized_engine_info[INPUT_BINDING_NAMES_IDX],
                    serialized_engine_info[OUTPUT_BINDING_NAMES_IDX],
                    serialized_engine_info[HW_COMPATIBLE_IDX],
                    serialized_engine_info[SERIALIZED_METADATA_IDX],
                ]
            )
        else:
            self.engine = None

        self.input_binding_names = state[2]
        self.output_binding_names = state[3]
        self.hardware_compatible = (
            bool(int(state[1][6])) if state[1] is not None else False
        )
        self.settings = TorchTensorRTModule.decode_metadata(serialized_engine_info[7])

    def forward(self, *inputs: Any) -> torch.Tensor | Tuple[torch.Tensor, ...]:
        """Implementation of the forward pass for a TensorRT engine

        Args:
            *inputs (Union[torch.Tensor, int]): Inputs to the forward function

        Returns:
            torch.Tensor or Tuple(torch.Tensor): Result of the engine computation
        """
        if self.engine is None:
            raise RuntimeError("Engine has not been initialized yet.")

        assert len(inputs) == len(
            self.input_binding_names
        ), f"Wrong number of inputs, expected {len(self.input_binding_names)} got {len(inputs)}."

        # If the inputs are not Torch Tensors, which can occur in scenarios such as shape tensors
        # which are outputs of a preceding Torch subgraph (where the Dynamic input may be an integer)
        # directly cast the input to a Torch Tensor.
        #
        # This also avoids the need for type-checking inputs, since they are now explicitly casted to Torch tensors
        input_tensors: List[torch.Tensor] = [
            torch.as_tensor(i).cuda() for i in inputs
        ]

        outputs: List[torch.Tensor] = torch.ops.tensorrt.execute_engine(
            list(input_tensors), self.engine
        )

        if len(outputs) == 1:
            return outputs[0]

        return tuple(outputs)

    def enable_profiling(self, profiling_results_dir: Optional[str] = None) -> None:
        """Enable the profiler to collect latency information about the execution of the engine

        Traces can be visualized using https://ui.perfetto.dev/ or compatible alternatives

        Keyword Arguments:
            profiling_results_dir (str): Absolute path to the directory to sort results of profiling.
        """
        if self.engine is None:
            raise RuntimeError("Engine has not been initialized yet.")

        if profiling_results_dir is not None:
            self.engine.profile_path_prefix = profiling_results_dir
        self.engine.enable_profiling()

    def disable_profiling(self) -> None:
        """Disable the profiler"""
        if self.engine is None:
            raise RuntimeError("Engine has not been initialized yet.")

        self.engine.disable_profiling()

    def get_layer_info(self) -> str:
        """Get a JSON string containing the layer information encoded by the TensorRT engine in this module

        Returns:

            str: A JSON string which contains the layer information of the engine incapsulated in this module
        """
        if self.engine is None:
            raise RuntimeError("Engine has not been initialized yet.")

        layer_info: str = self.engine.get_engine_layer_info()
        return layer_info

    def dump_layer_info(self) -> None:
        """Dump layer information encoded by the TensorRT engine in this module to STDOUT"""
        if self.engine is None:
            raise RuntimeError("Engine has not been initialized yet.")

        self.engine.dump_engine_layer_info()

    @staticmethod
    def _pack_binding_names(binding_names: List[str]) -> str:
        delim = torch.ops.tensorrt.SERIALIZED_ENGINE_BINDING_DELIM()[0]
        packed_bindings: str = delim.join(binding_names)
        return packed_bindings
