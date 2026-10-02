"""验收套件级隔离：测例失败也不得改写种子库里的成功运行行数。

做法：在导入任何 app 模块之前，把 DATABASE_URL 指到一次性 sqlite 文件——
整套验收（含现网入口 POST /api/allocate/run 写的 allocation_runs）只落在
这个临时库上，种子库（Postgres）从头到尾不被连接；另加一道 autouse 守卫，
一旦发现配置指向非隔离库，直接拒绝运行，宁可整组失败也不碰种子库。
"""
import os
import tempfile

_TMP_DIR = tempfile.mkdtemp(prefix="stallspan_acceptance_")
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP_DIR}/acceptance.db"
os.environ["SEED_ON_EMPTY"] = "false"  # 种子由场景编排显式调用，不走 lifespan

import pytest  # noqa: E402

from app.config import settings  # noqa: E402
from app.database import Base, SessionLocal, engine  # noqa: E402


@pytest.fixture(autouse=True)
def _guard_isolated_db():
    url = settings.database_url
    if not url.startswith("sqlite"):
        pytest.fail(f"验收套件拒绝在非隔离库上运行（会污染种子库行数）: {url}")


@pytest.fixture()
def db():
    """每个测例一套空表；用毕整库 drop，行数变化出不了这个临时文件。"""
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(bind=engine)


@pytest.fixture()
def client(db):
    """现网入口的 HTTP 客户端。不进 lifespan，套件外状态一律不触。"""
    from fastapi.testclient import TestClient

    from app.main import app

    return TestClient(app)
