from cs4406_sem_project.config import PipelineConfig, grid


def test_config_id_stable_and_param_order_independent():
    a = PipelineConfig("A4", "splade", 100, "cross-encoder/ms-marco-MiniLM-L-6-v2", (("q", 5), ("h", 0.8)))
    b = PipelineConfig("A4", "splade", 100, "cross-encoder/ms-marco-MiniLM-L-6-v2", (("h", 0.8), ("q", 5)))
    assert a.config_id == b.config_id == "A4|splade|k100|h=0.8|q=5|ms-marco-MiniLM-L-6-v2"


def test_grid_ids_unique():
    ids = [c.config_id for c in grid(("A1", "A2"))]
    assert len(ids) == len(set(ids)) == 8
