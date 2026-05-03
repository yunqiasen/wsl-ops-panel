from pathlib import Path

from pydantic import BaseModel, ConfigDict


class SQLiteDatabaseConfig(BaseModel):
    model_config = ConfigDict(extra='forbid')

    path: Path = Path('data/tasks.sqlite3')
