.. _runtime:

Saving models compiled with Torch-TensorRT
====================================

Saving models compiled with Torch-TensorRT varies slightly with the `ir` that has been used for compilation.

1) Dynamo IR

Starting with 2.1 release of Torch-TensorRT, we are switching the default compilation to be dynamo based.
The output of `ir=dynamo` compilation is a `torch.fx.GraphModule` object. There are two ways to save these objects

a) Converting to Torchscript
`torch.fx.GraphModule` objects cannot be serialized directly. Hence we use `torch.jit.trace` to convert this into a `ScriptModule` object which can be saved to disk. 
The following code illustrates this approach. 

.. code-block:: python

    import torch
    import torch_tensorrt

    model = MyModel().eval().cuda()
    inputs = torch.randn((1, 3, 224, 224)).cuda()
    trt_gm = torch_tensorrt.compile(model, ir="dynamo", inputs) # Output is a torch.fx.GraphModule
    trt_script_model = torch.jit.trace(trt_gm, inputs)
    torch.jit.save(trt_script_model, "trt_model.ts")

    # Later, you can load it and run inference
    model = torch.jit.load("trt_model.ts").cuda()
    model(inputs)

b) ExportedProgram
`torch.export.ExportedProgram` is a new format introduced in Pytorch 2.1. After we compile a Pytorch module using Torch-TensorRT, the resultant 
`torch.fx.GraphModule` along with additional metadata can be used to create `ExportedProgram` which can be saved and loaded from disk.

.. code-block:: python

    import torch
    import torch_tensorrt
    from torch_tensorrt.dynamo.export import transform, create_exported_program

    model = MyModel().eval().cuda()
    inputs = torch.randn((1, 3, 224, 224)).cuda()
    trt_gm = torch_tensorrt.compile(model, ir="dynamo", inputs) # Output is a torch.fx.GraphModule
    # Transform and create an exported program
    trt_gm = transform(trt_gm, inputs)
    trt_exp_program = create_exported_program(trt_gm, call_spec, trt_gm.state_dict())
    torch._export.save(trt_exp_program, "trt_model.ep")

    # Later, you can load it and run inference 
    model = torch._export.load("trt_model.ep")
    model(inputs)

`torch_tensorrt.dynamo.export.transform` inlines the submodules within a GraphModule to their corresponding nodes and stiches all the nodes together. 
This is needed as `torch._export` serialization cannot handle serializing and deserializing of submodules (`call_module` nodes). 

NOTE: This way of saving the models using `ExportedProgram` is experimental. Here is a known issue : https://github.com/pytorch/TensorRT/issues/2341

2) Torchscript IR

  In Torch-TensorRT 1.X versions, the primary way to compile and run inference with Torch-TensorRT is using Torchscript IR.
  This behavior stays the same in 2.X versions as well. 

  .. code-block:: python

    import torch
    import torch_tensorrt

    model = MyModel().eval().cuda()
    inputs = torch.randn((1, 3, 224, 224)).cuda()
    trt_ts = torch_tensorrt.compile(model, ir="ts", inputs) # Output is a ScriptModule object
    torch.jit.save(trt_ts, "trt_model.ts")

    # Later, you can load it and run inference
    model = torch.jit.load("trt_model.ts").cuda()
    model(inputs)
  