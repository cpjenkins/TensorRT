from typing import Optional, Sequence, Union

import torch_tensorrt.dynamo.conversion.impl as impl
from torch.fx.node import Target
from torch_tensorrt.dynamo.conversion._ConversionContext import ConversionContext
from torch_tensorrt.dynamo.conversion.converter_utils import SourceIR, get_trt_tensor
from torch_tensorrt.fx.converters.converter_utils import set_layer_name
from torch_tensorrt.fx.types import TRTTensor


def reshape(
    ctx: ConversionContext,
    target: Union[Target, str],
    source_ir: Optional[SourceIR],
    name: str,
    input: TRTTensor,
    shape: Sequence[int],
) -> TRTTensor:
    layer = ctx.net.add_shuffle(input)
    if all(isinstance(s, int) for s in shape):
        layer.reshape_dims = tuple(shape)
    else:
        # Convert all the dimensions to trt Tensors.
        trt_shape = []

        for i, s in enumerate(shape):
            if isinstance(s, TRTTensor):
                trt_shape.append(s)
            else:
                a = get_trt_tensor(ctx, s, f"{name}_{i}")
                trt_shape.append(a)
        shape_layer = ctx.net.add_concatenation(inputs=trt_shape)
        shape_layer.axis = 0
        shape_layer.name = f"{name}_output_shape"
        layer.set_input(1, shape_layer.get_output(0))

    set_layer_name(layer, target, name, source_ir)
    return layer.get_output(0)


def pixel_shuffle(
    ctx: ConversionContext,
    target: Union[Target, str],
    source_ir: Optional[SourceIR],
    name: str,
    input: TRTTensor,
    upscale_factor: int,
) -> TRTTensor:
    shape = input.shape
    in_channels, in_height, in_width = shape[-3:]
    out_channels = in_channels // (upscale_factor**2)
    out_height = in_height * upscale_factor
    out_width = in_width * upscale_factor
    new_shape = shape[:-3] + (
        out_channels,
        upscale_factor,
        upscale_factor,
        in_height,
        in_width,
    )
    reshaped_tensor = reshape(
        ctx, target, source_ir, f"{name}_reshape1", input, new_shape
    )
    rank = len(shape)
    permute_shape = list(range(rank))
    permute_shape.insert(-2, rank)
    permute_shape.insert(-1, rank + 1)
    permuted_tensor = impl.permutation.permute(
        ctx, target, source_ir, f"{name}_permute", reshaped_tensor, permute_shape
    )
    return reshape(
        ctx,
        target,
        source_ir,
        f"{name}_reshape2",
        permuted_tensor,
        shape[:-3] + (out_channels, out_height, out_width),
    )
