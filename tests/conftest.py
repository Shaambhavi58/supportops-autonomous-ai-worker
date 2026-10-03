from pathlib import Path

import pytest

from supportops.agent import SupportOpsAgent
from supportops.config import Settings
from supportops.database import Database


@pytest.fixture
def make_agent(tmp_path: Path):
    def factory(*, threshold: float = 50, retries: int = 2) -> tuple[SupportOpsAgent, Database]:
        settings = Settings(
            demo_mode=True,
            db_path=tmp_path / "supportops.sqlite3",
            approval_threshold=threshold,
            max_retries=retries,
            retry_base_delay_seconds=0,
        )
        database = Database(settings.db_path, approval_threshold=threshold)
        return SupportOpsAgent(settings=settings, database=database), database

    return factory