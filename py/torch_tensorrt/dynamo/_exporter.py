import operator
from typing import Any, Dict, Sequence, Tuple, cast

import torch
from torch._guards import detect_fake_mode
from torch._subclasses.fake_tensor import FakeTensor
from torch.export import ExportedProgram, ExportGraphSignature
from torch.export.exported_program import (
    InputKind,
    InputSpec,
    OutputKind,
    OutputSpec,
    TensorArgument,
)
from torch_tensorrt.dynamo import partitioning


def export(
    gm: torch.fx.GraphModule,
    inputs: Sequence[torch.Tensor],
    output_format: str,
) -> ExportedProgram:
    """Export the result of TensorRT compilation into the desired output format.

    Arguments:
        gm (torch.fx.GraphModule): Compiled Torch-TensorRT module, generated by ``torch_tensorrt.dynamo.compile``
        inputs (torch.Tensor): Torch input tensors
        output_format (str): Output format of the result of TRT compilation. Options include "exported_program" (or) "ep" | "torchscript" (or) "ts" | "graph_module" (or) "fx". Default is "exported_program"
    """
    if output_format == "torchscript" or output_format == "ts":
        return torch.jit.trace(gm, inputs)
    elif output_format == "exported_program" or output_format == "ep":
        patched_module = transform(gm, inputs)
        exp_program = create_trt_exp_program(patched_module)
        return exp_program
    elif output_format == "graph_module" or output_format == "fx":
        return gm
    else:
        raise ValueError(
            f"Invalid output format {output_format} specified. Supported options include exported_program (or) ep | torchscript (or) ts | graph_module (or) fx"
        )


def transform(
    gm: torch.fx.GraphModule, inputs: Sequence[torch.Tensor]
) -> torch.fx.GraphModule:
    """
    Transforms the graphmodule by inlining Pytorch and TensorRT submodules.
    Inlining collapses submodules into nodes which is necessary for torch.export
    serialization.

    Arguments:
        gm (torch.fx.GraphModule): Compiled Torch-TensorRT module, generated by ``torch_tensorrt.dynamo.compile``
        inputs (torch.Tensor): Torch input tensors

    Returns an inlined torch.fx.GraphModule
    """
    # Run shape analysis
    _, outputs_map = partitioning.run_shape_analysis(gm, inputs)

    # Inline TensorRT submodules
    inline_trt_modules(gm, outputs_map)

    # Inline pytorch submodules
    inline_torch_modules(gm)
    breakpoint()
    # Clean the graph
    gm.delete_all_unused_submodules()
    gm.graph.eliminate_dead_code()
    gm.graph.lint()

    return gm


def lift(gm: torch.fx.GraphModule, graph_signature: Any) -> torch.fx.GraphModule:
    """
    Given an unlifted fx.GraphModule, lift all parameters, buffers into placeholders.
    Arguments:
        gm (torch.fx.GraphModule): Unlifted GraphModule which contains parameters and buffers as get_attr nodes.
        graph_signature (torch.export.ExportGraphSignature): Instance of ExportGraphSignature class created for the output ExportedProgram.
        After lifting, this graph_signature will be modified with the parameters and buffers added appropriately.
    Returns:
        A lifted fx.GraphModule, modified graph_signature and a new state_dict
    """
    # Get the state_dict of graph_module. This is different from exported_program.state_dict
    # exp_program.state_dict contains parameters and buffers whereas a graph_module's state_dict
    # has all parameters registered as torch.tensors.
    state_dict = gm.state_dict()

    fake_mode = detect_fake_mode(
        tuple(node.meta["val"] for node in gm.graph.nodes if node.op == "placeholder")
    )
    assert fake_mode is not None

    # Locate the user input to insert new placeholders before them
    first_user_input = None
    for node in gm.graph.nodes:
        if node.op == "placeholder" and node.name in graph_signature.user_inputs:
            first_user_input = node
            break

    # At first the user_inputs are only present in the graph_signature.input_specs and hence non_user_input_idx=0
    # The input_specs should be of the form [params, buffers, constant_tensors, user_inputs]
    non_user_input_idx = 0
    for node in gm.graph.nodes:
        if node.op == "get_attr":
            if node.target not in state_dict:
                raise ValueError(
                    f"The get_attr node : {node.name} with target: {node.target} value could not be found in state_dict. Please check the input exported_program's graphmodule parameters."
                )

            constant_tensor = state_dict[node.target]
            input_kind = InputKind.CONSTANT_TENSOR

            # state_dict has these parameters/buffers as torch.Tensors. We override them as torch.nn.Parameter/torch.Tensors respectively.
            for name, _ in gm.named_parameters():
                if node.target == name:
                    input_kind = InputKind.PARAMETER
                    state_dict[name] = torch.nn.Parameter(state_dict[name])
                    break
            for name, _ in gm.named_buffers():
                if node.target == name:
                    input_kind = InputKind.BUFFER
                    break

            # Replace get_attr nodes with placeholder nodes and copy metadata.
            with gm.graph.inserting_before(first_user_input):
                const_placeholder_node = gm.graph.placeholder(node.target)
                for k, v in node.meta.items():
                    const_placeholder_node.meta[k] = v
                const_placeholder_node.meta["val"] = fake_mode.from_tensor(
                    constant_tensor
                )
                node.replace_all_uses_with(const_placeholder_node)
                gm.graph.erase_node(node)

                # Add these parameters/buffers/constants to the existing graph signature
                # before user inputs. These specs are looked up in the state_dict during ExportedProgram creation.
                graph_signature.input_specs.insert(
                    non_user_input_idx,
                    InputSpec(
                        kind=input_kind,
                        arg=TensorArgument(name=const_placeholder_node.name),
                        target=node.target,
                    ),
                )
                non_user_input_idx += 1

    gm.graph.eliminate_dead_code()
    gm.graph.lint()

    return gm, graph_signature, state_dict


def get_duplicate_nodes(
    gm: torch.fx.GraphModule, submodule: torch.fx.GraphModule
) -> Tuple[Sequence[Any], Sequence[Any]]:
    """
    We check if there are duplicate nodes when we copy submodule graph into gm.
    Handle the case where the subgraph input placeholders are same as
    gm placeholders. This happens when the first submodule in the graph is
    a pytorch submodule
    """
    submodule_placeholder_inputs = [
        node for node in submodule.graph.nodes if node.op == "placeholder"
    ]
    submodule_input_node_names = [node.name for node in submodule_placeholder_inputs]
    gm_node_names = [node.name for node in gm.graph.nodes]
    submodule_duplicate_inputs = [
        node for node in submodule_placeholder_inputs if node.name in gm_node_names
    ]
    gm_duplicate_inputs = [
        node for node in gm.graph.nodes if node.name in submodule_input_node_names
    ]
    return submodule_duplicate_inputs, gm_duplicate_inputs


def inline_torch_modules(gm: torch.fx.GraphModule) -> torch.fx.GraphModule:
    """
    Inline a submodule within the parent graph (gm). All `call_module` nodes
    should be replaced by their nodes in the submodule.
    """
    # Clean the graph
    gm.graph.eliminate_dead_code()
    gm.graph.lint()

    for gm_node in gm.graph.nodes:
        if gm_node.op == "call_module" and "_run_on_gpu" in gm_node.name:
            submodule = getattr(gm, gm_node.name)
            with gm.graph.inserting_before(gm_node):
                # Get inputs of submodule node which are most likely outputs of a previous TRT node
                # or a placeholder of the main graph
                submodule_inputs = gm_node.args

                submodule_duplicate_inputs, gm_duplicate_inputs = get_duplicate_nodes(
                    gm, submodule
                )
                assert len(submodule_duplicate_inputs) == len(gm_duplicate_inputs)
                # Avoid creating new copies of duplicate inputs by creating a mapping
                val_map = {}
                for i in range(len(submodule_duplicate_inputs)):
                    val_map[submodule_duplicate_inputs[i]] = gm_duplicate_inputs[i]

                # Copy all nodes in the submodule into gm and
                # store the output node of this submodule which is now present in gm
                submodule_output = gm.graph.graph_copy(submodule.graph, val_map)

                # Get their references (since we copied) in the parent graph (gm)
                if len(submodule_duplicate_inputs) == 0:
                    submodule_placeholder_input_names = [
                        node.name
                        for node in submodule.graph.nodes
                        if node.op == "placeholder"
                    ]
                    gm_added_placeholder_inputs = [
                        node
                        for node in gm.graph.nodes
                        if node.name in submodule_placeholder_input_names
                    ]

                    assert len(submodule_inputs) == len(gm_added_placeholder_inputs)

                    # Replace the added placeholder inputs with original inputs to this submodule node
                    for idx in range(len(gm_added_placeholder_inputs)):
                        gm_added_placeholder_inputs[idx].replace_all_uses_with(
                            submodule_inputs[idx]
                        )

                    # Erase the placeholder input nodes in the gm
                    for idx in range(len(gm_added_placeholder_inputs)):
                        gm.graph.erase_node(gm_added_placeholder_inputs[idx])

                # Replace the pytorch submodule node (call_module) with the inlined subgraph output
                gm_node.replace_all_uses_with(submodule_output)
                breakpoint()
                # copy the attributes of the submodule into gm (graph_copy doesn't do this)
                copy_submodule_attributes(gm, submodule, gm_node.name)

            # Erase the pytorch submodule (call_module) node
            gm.graph.erase_node(gm_node)

    return gm


def copy_submodule_attributes(
    gm: torch.fx.GraphModule, submodule: torch.fx.GraphModule, submodule_name: str
) -> None:
    """
    Rename the submodule parameters in the state_dict because we graph_copied
    submodule into parent module gm. The graph_copy call doesn't do this for us unfortunately.
    """

    gm_state_dict = gm.state_dict()
    sub_state_dict = submodule.state_dict()
    # This state dict should have submodule parameters with the submodule name removed in their keys.
    breakpoint()
    updated_state_dict = {}
    for key, value in gm_state_dict.items():
        parent_key = key.replace(submodule_name + ".", "")
        if parent_key in sub_state_dict:
            updated_state_dict[parent_key] = value
        else:
            updated_state_dict[key] = value
    breakpoint()
    gm.load_state_dict(updated_state_dict)

    # for param in gm.named_parameters():
    #     if param[0].startswith(submod_name + "."):
    #         param_name = param[0].replace(submod_name + ".", "")
    #         gm.register_parameter(param_name, param[1])
    #         # gm.state_dict().pop(param[0])
    #         # gm.state_dict()[param_name] = param[1]

    # for buffer in gm.named_buffers():
    #     if buffer[0].startswith(submod_name + "."):
    #         buffer_name = buffer[0].replace(submod_name + ".", "")
    #         gm.register_buffer(buffer_name, buffer[1])
    #         # gm.state_dict().pop(buffer[0])
    #         # gm.state_dict()[buffer_name] = buffer[1]


def create_trt_exp_program(
    gm: torch.fx.GraphModule,
) -> ExportedProgram:
    """Creates a new Exported Program. This function takes an torch.fx.GraphModule which has TRT engines
    and constructs an Exported Program object with the new IO node names and state_dict
    """

    input_nodes = [node for node in gm.graph.nodes if node.op == "placeholder"]
    output_nodes = [node for node in gm.graph.nodes if node.op == "output"]
    assert output_nodes
    output_nodes = output_nodes[0].args[0]

    input_specs = [
        InputSpec(InputKind.USER_INPUT, TensorArgument(name=node.name), node.target)
        for node in input_nodes
    ]
    output_specs = [
        OutputSpec(OutputKind.USER_OUTPUT, TensorArgument(name=node.name), node.target)
        for node in output_nodes
    ]

    trt_graph_signature = ExportGraphSignature(
        input_specs=input_specs, output_specs=output_specs
    )

    # Lift parameters/buffers/constants in the graph
    # torch.export serialization expects them to be lifted
    gm, trt_graph_signature, state_dict = lift(gm, trt_graph_signature)

    trt_exp_program = ExportedProgram(
        gm,
        gm.graph,
        trt_graph_signature,
        state_dict,
        {},
        [],
        [],
    )

    return trt_exp_program


def inline_trt_modules(
    gm: torch.fx.GraphModule, outputs_map: Dict[Any, Sequence[Any]]
) -> torch.fx.GraphModule:
    """
    Replace TRT submodules with trt engine nodes.
    """
    for name, _ in gm.named_children():
        if "_run_on_acc" not in name:
            continue
        # Get the TRT submodule
        trt_module = getattr(gm, name)

        # Ensure the trt module node in the main graph (gm) has inputs
        trt_module_node = [node for node in gm.graph.nodes if node.name == name]
        assert trt_module_node
        trt_module_node = trt_module_node[0]
        assert trt_module_node.args

        num_outputs = len(outputs_map[trt_module_node.name])
        # Insert a call_function node to perform inference on TRT engine
        with gm.graph.inserting_before(trt_module_node):
            trt_node = gm.graph.call_function(
                torch.ops.tensorrt.execute_engine.default,
                (trt_module_node.args, trt_module.engine),
            )
            trt_node.meta["val"] = []
            assert num_outputs > 0
            # Generate meta data for TRT node (a FakeTensor with corresponding output shape)
            for idx in range(num_outputs):
                trt_node.meta["val"].append(
                    cast(
                        FakeTensor,
                        torch.empty_strided(
                            tuple(outputs_map[trt_module_node.name][idx]),
                            tuple([1] * len(outputs_map[trt_module_node.name][idx])),
                        ),
                    )
                )

        if num_outputs == 1:
            # Insert getitem nodes as outputs (for export serialization to work)
            with gm.graph.inserting_after(trt_node):
                getitem_output = gm.graph.call_function(operator.getitem, (trt_node, 0))
                getitem_output.meta["val"] = trt_node.meta["val"]
            trt_module_node.replace_all_uses_with(getitem_output)
        else:
            # Multiple outputs case:
            # Replace uses of submodule with the trt_node.
            # getitem nodes are already added inherently by the partitioner
            trt_module_node.replace_all_uses_with(trt_node)
            getitem_nodes = trt_node.users
            for idx, getitem_node in enumerate(getitem_nodes):
                getitem_node.meta["val"] = trt_node.meta["val"][idx]

        # Erase the TRT submodule (call_module) node.
        gm.graph.erase_node(trt_module_node)

    return gm
