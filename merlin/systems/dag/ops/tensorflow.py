#
# Copyright (c) 2022, NVIDIA CORPORATION.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
import os
import pathlib
import tempfile
from shutil import copytree

# this needs to be before any modules that import protobuf
os.environ["PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION"] = "python"

import tensorflow as tf  # noqa
import tritonclient.grpc.model_config_pb2 as model_config  # noqa
from google.protobuf import text_format  # noqa

from merlin.core.protocols import Transformable  # noqa
from merlin.dag import ColumnSelector  # noqa
from merlin.schema import ColumnSchema, Schema  # noqa
from merlin.systems.dag.ops import compute_dims  # noqa
from merlin.systems.dag.ops.compat import pb_utils  # noqa
from merlin.systems.dag.ops.operator import PipelineableInferenceOperator, add_model_param  # noqa


class PredictTensorflow(PipelineableInferenceOperator):
    """
    This operator takes a tensorflow model and packages it correctly for tritonserver
    to run, on the tensorflow backend.
    """

    def __init__(self, model_or_path, custom_objects: dict = None, backend="tensorflow"):
        """
        Instantiate a PredictTensorflow inference operator.

        Parameters
        ----------
        model_or_path : Tensorflow model or string
            This can be a tensorflow model or a path to a tensorflow model.
        custom_objects : dict, optional
            Any custom objects that need to be loaded with the model, by default None.
        """
        super().__init__()
        self._tf_model_name = None

        if model_or_path is not None:
            custom_objects = custom_objects or {}

            if isinstance(model_or_path, (str, os.PathLike)):
                self.path = model_or_path
                self.model = tf.keras.models.load_model(self.path, custom_objects=custom_objects)
            else:
                self.path = None
                self.model = model_or_path

            self.input_schema, self.output_schema = self._construct_schemas_from_model(self.model)

    def __getstate__(self):
        return {k: v for k, v in self.__dict__.items() if k != "model"}

    @property
    def tf_model_name(self):
        return self._tf_model_name

    def set_tf_model_name(self, tf_model_name: str):
        """
        Set the name of the Triton model to use

        Parameters
        ----------
        tf_model_name : str
            Triton model directory name
        """
        self._tf_model_name = tf_model_name

    def transform(self, col_selector: ColumnSelector, transformable: Transformable):
        # TODO: Validate that the inputs match the schema
        # TODO: Should we coerce the dtypes to match the schema here?
        input_tensors = []
        for col_name in self.input_schema.column_schemas.keys():
            input_tensors.append(pb_utils.Tensor(col_name, transformable[col_name]))

        inference_request = pb_utils.InferenceRequest(
            model_name=self.tf_model_name,
            requested_output_names=self.output_schema.column_names,
            inputs=input_tensors,
        )
        inference_response = inference_request.exec()

        # TODO: Validate that the outputs match the schema
        outputs_dict = {}
        for out_col_name in self.output_schema.column_schemas.keys():
            output_val = pb_utils.get_output_tensor_by_name(
                inference_response, out_col_name
            ).as_numpy()
            outputs_dict[out_col_name] = output_val

        return type(transformable)(outputs_dict)

    @property
    def exportable_backends(self):
        return ["ensemble", "executor"]

    def export(
        self,
        path: str,
        input_schema: Schema,
        output_schema: Schema,
        params: dict = None,
        node_id: int = None,
        version: int = 1,
        backend: str = "ensemble",
    ):
        """Create a directory inside supplied path based on our export name"""
        # Export Triton TF back-end directory and config etc
        export_name = self.__class__.__name__.lower()
        node_name = f"{node_id}_{export_name}" if node_id is not None else export_name

        node_export_path = pathlib.Path(path) / node_name
        node_export_path.mkdir(exist_ok=True)

        tf_model_path = pathlib.Path(node_export_path) / str(version) / "model.savedmodel"

        if self.path:
            copytree(
                str(self.path),
                tf_model_path,
                dirs_exist_ok=True,
            )
        else:
            self.model.save(tf_model_path, include_optimizer=False)

        self.set_tf_model_name(node_name)
        backend_model_config = self._export_model_config(node_name, node_export_path)
        return backend_model_config

    @property
    def export_name(self):
        """
        Provides a clear common english identifier for this operator.

        Returns
        -------
        String
            Name of the current class as spelled in module.
        """
        return self.__class__.__name__.lower()

    @classmethod
    def from_path(cls, path, **kwargs):
        return cls.__init__(path, **kwargs)

    def compute_input_schema(
        self,
        root_schema: Schema,
        parents_schema: Schema,
        deps_schema: Schema,
        selector: ColumnSelector,
    ) -> Schema:
        """
        Use the input schema supplied during object creation.
        """
        return self.input_schema

    def compute_output_schema(
        self, input_schema: Schema, col_selector: ColumnSelector, prev_output_schema: Schema = None
    ) -> Schema:
        """
        Use the output schema supplied during object creation.
        """
        return self.output_schema

    def _export_model_config(self, name, output_path):
        """Exports a TensorFlow model for serving with Triton

        Parameters
        ----------
        model:
            The tensorflow model that should be served
        name:
            The name of the triton model to export
        output_path:
            The path to write the exported model to
        """
        config = model_config.ModelConfig(
            name=name, backend="tensorflow", platform="tensorflow_savedmodel"
        )

        config.parameters["TF_GRAPH_TAG"].string_value = "serve"
        config.parameters["TF_SIGNATURE_DEF"].string_value = "serving_default"

        for _, col_schema in self.input_schema.column_schemas.items():
            add_model_param(
                config.input,
                model_config.ModelInput,
                col_schema,
                compute_dims(col_schema, self.scalar_shape),
            )

        for _, col_schema in self.output_schema.column_schemas.items():
            add_model_param(
                config.output,
                model_config.ModelOutput,
                col_schema,
                compute_dims(col_schema, self.scalar_shape),
            )

        with open(os.path.join(output_path, "config.pbtxt"), "w", encoding="utf-8") as o:
            text_format.PrintMessage(config, o)
        return config

    def _construct_schemas_from_model(self, model):
        signatures = getattr(model, "signatures", {}) or {}
        default_signature = signatures.get("serving_default")

        if not default_signature:
            # roundtrip saved model to disk to generate signature if it doesn't exist
            self._ensure_input_spec_includes_names(model)

            with tempfile.TemporaryDirectory() as tmp_dir:
                tf_model_path = pathlib.Path(tmp_dir) / "model.savedmodel"
                model.save(tf_model_path, include_optimizer=False)
                reloaded = tf.keras.models.load_model(tf_model_path)
                default_signature = reloaded.signatures["serving_default"]

        input_schema = Schema()
        for col_name, col in default_signature.structured_input_signature[1].items():
            col_schema = ColumnSchema(col_name, dtype=col.dtype.as_numpy_dtype)
            if col.shape[1] and col.shape[1] > 1:
                col_schema = self._set_list_length(col_schema, col.shape[1])
            input_schema.column_schemas[col_name] = col_schema

        output_schema = Schema()
        for col_name, col in default_signature.structured_outputs.items():
            col_schema = ColumnSchema(col_name, dtype=col.dtype.as_numpy_dtype)
            if col.shape[1] and col.shape[1] > 1:
                col_schema = self._set_list_length(col_schema, col.shape[1])
            output_schema.column_schemas[col_name] = col_schema

        return input_schema, output_schema

    def _ensure_input_spec_includes_names(self, model):
        if isinstance(model._saved_model_inputs_spec, dict):
            for key, spec in model._saved_model_inputs_spec.items():
                if isinstance(spec, tuple):
                    model._saved_model_inputs_spec[key] = (
                        tf.TensorSpec(shape=spec[0].shape, dtype=spec[0].dtype, name=key),
                        tf.TensorSpec(shape=spec[1].shape, dtype=spec[1].dtype, name=key),
                    )
                else:
                    model._saved_model_inputs_spec[key] = tf.TensorSpec(
                        shape=spec.shape, dtype=spec.dtype, name=key
                    )

        return model

    def _set_list_length(self, col_schema, list_length):
        return col_schema.with_dtype(
            col_schema.dtype, is_list=True, is_ragged=False
        ).with_properties({"value_count": {"min": list_length, "max": list_length}})
