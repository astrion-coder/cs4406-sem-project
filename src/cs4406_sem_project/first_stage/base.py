from pathlib import Path
from typing import Protocol

import numpy as np


class FirstStage(Protocol):
    """A first-stage retriever over a persisted index.

    ``retrieve`` returns collection doc ids and scores, best first, at most ``k`` of each.
    """

    name: str

    def retrieve(self, query: str, k: int) -> tuple[np.ndarray, np.ndarray]: ...

    def index_files(self) -> list[Path]:
        """Files backing the index (used to drop them from the page cache for cold runs)."""
        ...

    def index_bytes(self) -> int:
        """On-disk size of the index."""
        ...
