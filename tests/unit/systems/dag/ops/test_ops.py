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
import random
import shutil

import numpy as np
import pytest

from merlin.core.dispatch import make_df
from merlin.schema import ColumnSchema, Schema
from merlin.systems.dag.ensemble import Ensemble
from merlin.systems.dag.ops import compute_dims
from merlin.systems.dag.ops.session_filter import FilterCandidates
from merlin.systems.dag.ops.softmax_sampling import SoftmaxSampling
from merlin.systems.triton.utils import run_ensemble_on_tritonserver

TRITON_SERVER_PATH = shutil.which("tritonserver")


@pytest.mark.parametrize(
    ["column_schema", "expected_dims"],
    [
        [ColumnSchema("col"), [-1, 1]],
        [ColumnSchema("col", is_list=True), [-1, -1]],
        [ColumnSchema("col", dims=(None, 2)), [-1, 2]],
        [ColumnSchema("col", dims=(None, None)), [-1, -1]],
        [ColumnSchema("col", dims=(None, (1, 4))), [-1, -1]],
        [ColumnSchema("col", dims=(None, 3, 4)), [-1, 3, 4]],
    ],
)
def test_compute_dims(column_schema, expected_dims):
    assert compute_dims(column_schema) == expected_dims


@pytest.mark.skipif(not TRITON_SERVER_PATH, reason="triton server not found")
def test_softmax_sampling(tmpdir):
    request_schema = Schema(
        [
            ColumnSchema("movie_ids", dtype=np.int32),
            ColumnSchema("output_1", dtype=np.float32),
        ]
    )

    combined_features = {
        "movie_ids": np.array(random.sample(range(10000), 100), dtype=np.int32),
        "output_1": np.random.random(100).astype(np.float32),
    }

    request_df = make_df(combined_features)

    ordering = ["movie_ids"] >> SoftmaxSampling(relevance_col="output_1", topk=10, temperature=20.0)

    ensemble = Ensemble(ordering, request_schema)
    ens_config, node_configs = ensemble.export(tmpdir)

    response = run_ensemble_on_tritonserver(
        tmpdir, request_schema, request_df, ensemble.output_schema.column_names, "executor_model"
    )
    assert response is not None
    assert len(response["ordered_ids"]) == 10


@pytest.mark.skipif(not TRITON_SERVER_PATH, reason="triton server not found")
def test_filter_candidates_with_triton(tmpdir):
    request_schema = Schema(
        [
            ColumnSchema("candidate_ids", dtype=np.int32),
            ColumnSchema("movie_ids", dtype=np.int32),
        ]
    )

    candidate_ids = np.array(random.sample(range(100000), 100), dtype=np.int32)
    movie_ids_1 = np.zeros(100, dtype=np.int32)
    movie_ids_1[:20] = np.unique(candidate_ids)[:20]

    combined_features = {
        "candidate_ids": candidate_ids,
        "movie_ids": movie_ids_1,
    }

    request_df = make_df(combined_features)

    filtering = ["candidate_ids"] >> FilterCandidates(filter_out=["movie_ids"])

    ensemble = Ensemble(filtering, request_schema)
    ens_config, node_configs = ensemble.export(tmpdir)

    response = run_ensemble_on_tritonserver(
        tmpdir, request_schema, request_df, ensemble.output_schema.column_names, "executor_model"
    )

    assert response is not None
    assert len(response["filtered_ids"]) == 80
