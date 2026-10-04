"""pytest 公共夹具：使用临时 SQLite 数据库与 TestClient。"""

import os
import tempfile

_tmp = tempfile.mkdtemp(prefix="stowage_test_")
os.environ["STOWAGE_DATABASE_URL"] = f"sqlite:///{_tmp}/test.db"

from fastapi.testclient import TestClient  # noqa: E402

from app.database import SessionLocal, init_db  # noqa: E402
from app.main import app  # noqa: E402
import pytest  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def _database():
    init_db()
    yield


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture
def db():
    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


@pytest.fixture(autouse=True)
def _clean_tables():
    """每个测试前清空业务表，保证用例互不污染。"""
    import app.models as models
    db = SessionLocal()
    try:
        db.query(models.SealReleaseItem).delete()
        db.query(models.SealRelease).delete()
        db.query(models.SealScan).delete()
        db.query(models.SealReplacement).delete()
        db.query(models.SealManifestItem).delete()
        db.query(models.SealManifest).delete()
        db.query(models.LoadingHandoverItem).delete()
        db.query(models.LoadingHandover).delete()
        db.query(models.LoadingActualEvent).delete()
        db.query(models.LoadingActualCorrection).delete()
        db.query(models.LoadingActualBaselineItem).delete()
        db.query(models.LoadingActuals).delete()
        db.query(models.LoadingScheduleItem).delete()
        db.query(models.LoadingSchedule).delete()
        db.query(models.UnloadingStepRow).delete()
        db.query(models.UnloadingPortRow).delete()
        db.query(models.UnloadingSequence).delete()
        db.query(models.UnloadingExecOperation).delete()
        db.query(models.UnloadingWindowAdjustment).delete()
        db.query(models.UnloadingExecStep).delete()
        db.query(models.UnloadingEffectivePort).delete()
        db.query(models.UnloadingFrozenPort).delete()
        db.query(models.UnloadingFrozenBox).delete()
        db.query(models.UnloadingExecution).delete()
        db.query(models.ConfirmedSlot).delete()
        db.query(models.PlanItem).delete()
        db.query(models.Plan).delete()
        db.query(models.ContainerRow).delete()
        db.query(models.ManifestRevision).delete()
        db.query(models.AdjacencyRow).delete()
        db.query(models.IncompatRow).delete()
        db.query(models.SlotRow).delete()
        db.query(models.SlotRevision).delete()
        db.query(models.RevisionCounter).delete()
        db.commit()
    finally:
        db.close()
    yield
