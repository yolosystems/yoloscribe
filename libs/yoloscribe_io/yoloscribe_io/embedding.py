"""The semantic indexing engine, fixed.

Semantic search is `amazon.titan-embed-text-v2:0` embeddings stored in S3
Vectors. That is not configurable, and the reason is the index rather than a
preference: an S3 Vectors index fixes its dimension and distance metric at
creation and offers no way to alter them afterwards.

So the three values below are one decision, not three settings. Changing the
model changes the width of every vector, which an existing index cannot accept;
the only way through is to delete the index and re-embed every page. Worse, that
failure is invisible at startup -- a service configured with the wrong model
comes up healthy and fails on its first indexing job.

Keeping this a constant is also what makes the direct Bedrock call correct.
Embeddings deliberately bypass LiteLLM because the model is fixed, so the
provider flexibility a proxy buys has no value here. Declaring it here makes
that true by construction rather than by convention.

`DIMENSION` is 1024 because callers invoke Titan with no `dimensions` field and
take the model's default width. A caller that starts passing `dimensions` would
silently invalidate the index; don't.
"""

from __future__ import annotations

#: Bedrock model id used for every embedding in the system.
MODEL_ID = "amazon.titan-embed-text-v2:0"

#: Vector width produced by MODEL_ID at its default settings.
DIMENSION = 1024

#: Distance metric the S3 Vectors index is created with.
DISTANCE_METRIC = "cosine"

#: Element type of the stored vectors.
DATA_TYPE = "float32"
