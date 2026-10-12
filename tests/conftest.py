import os, sys, tempfile
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from config import Config
from storage import Storage
from market_data import MarketDataService, MockProvider
from paper_execution import PaperExecutor
from position_manager import PositionManager
from risk_engine import RiskEngine
from intelligence import Intelligence


@pytest.fixture
def tmp_db(tmp_path):
    return str(tmp_path / "test.db")


@pytest.fixture
def cfg(tmp_db):
    return Config(db_path=tmp_db, initial_equity=20.0).validate()


@pytest.fixture
def storage(cfg):
    s = Storage(cfg.db_path)
    yield s
    s.close()


@pytest.fixture
def market():
    return MarketDataService([MockProvider()], cache_ttl=0.0, stale_after=60.0)


@pytest.fixture
def executor(cfg):
    return PaperExecutor(cfg)


@pytest.fixture
def pm(cfg, storage, executor):
    return PositionManager(cfg, storage, executor)


@pytest.fixture
def risk(cfg, storage):
    return RiskEngine(cfg, storage)


@pytest.fixture
def intelligence(cfg, storage, market, risk, pm):
    return Intelligence(cfg, storage, market, risk, pm)
