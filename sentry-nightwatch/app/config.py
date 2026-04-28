"""Configuration loader: YAML + environment variables.

Single source of truth for runtime config. All other modules import from here.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parent.parent
CONFIGS = ROOT / "configs"
SNAPSHOTS = ROOT / "snapshots"
FIXTURES = ROOT / "tests" / "fixtures"


# ──────────────────────────────────────────────────────────────────────────
# Pydantic models
# ──────────────────────────────────────────────────────────────────────────


class ProjectSpec(BaseModel):
    slug: str
    kind: Literal["backend", "frontend"]
    git_path: str
    environment: str = "production"


class ProjectsConfig(BaseModel):
    organization: str
    projects: list[ProjectSpec]
    test_fixtures_whitelist: list[str] = Field(default_factory=list)


class ScoringWeights(BaseModel):
    level_fatal: int
    level_error: int
    level_warning: int
    is_new: int
    is_regression: int
    is_spike: int
    is_release_correlated: int
    is_user_impacting: int
    cross_project_member: int


class ScoringThresholds(BaseModel):
    user_impacting_min_users: int
    spike_baseline_multiplier: float
    cluster_time_window_minutes: int


class ScoringConfig(BaseModel):
    weights: ScoringWeights
    thresholds: ScoringThresholds


class RateLimits(BaseModel):
    sentry_max_req_per_min: int
    sentry_max_event_fetches_per_run: int


class RulesConfig(BaseModel):
    scoring: ScoringConfig
    rate_limits: RateLimits


class EnvConfig(BaseModel):
    sentry_auth_token: str = ""
    sentry_org_slug: str = ""
    sentry_base_url: str = "https://sentry.io"
    nightwatch_tz: str = "Asia/Tehran"
    nightwatch_twice_daily: bool = False
    nightwatch_retention_days: int = 30


class AppConfig(BaseModel):
    projects: ProjectsConfig
    rules: RulesConfig
    env: EnvConfig
    snapshots_dir: Path
    fixtures_dir: Path


# ──────────────────────────────────────────────────────────────────────────
# Loaders
# ──────────────────────────────────────────────────────────────────────────


def _load_yaml(path: Path) -> dict:
    with path.open(encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def load_projects(path: Path | None = None) -> ProjectsConfig:
    return ProjectsConfig(**_load_yaml(path or CONFIGS / "projects.yml"))


def load_rules(path: Path | None = None) -> RulesConfig:
    return RulesConfig(**_load_yaml(path or CONFIGS / "rules.yml"))


def load_env() -> EnvConfig:
    return EnvConfig(
        sentry_auth_token=os.environ.get("SENTRY_AUTH_TOKEN", ""),
        sentry_org_slug=os.environ.get("SENTRY_ORG_SLUG", ""),
        sentry_base_url=os.environ.get("SENTRY_BASE_URL", "https://sentry.io"),
        nightwatch_tz=os.environ.get("NIGHTWATCH_TZ", "Asia/Tehran"),
        nightwatch_twice_daily=os.environ.get("NIGHTWATCH_TWICE_DAILY", "false").lower()
        in ("1", "true", "yes"),
        nightwatch_retention_days=int(os.environ.get("NIGHTWATCH_RETENTION_DAYS", "30")),
    )


@lru_cache(maxsize=1)
def load_config() -> AppConfig:
    return AppConfig(
        projects=load_projects(),
        rules=load_rules(),
        env=load_env(),
        snapshots_dir=SNAPSHOTS,
        fixtures_dir=FIXTURES,
    )
