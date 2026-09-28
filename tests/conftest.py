"""Shared fixtures and test doubles for the tm1-dump test suite."""

from __future__ import annotations

import pytest
from TM1py.Objects import User

from tm1_dump.cli import build_parser


def load_args(zip_path, *extra: str):
    """Parse a ``load`` command line against the fixture zip."""
    return build_parser().parse_args(
        ["load", str(zip_path), "--address", "srv", "--port", "12354", "--user", "admin", "--password", "pw", *extra]
    )


MUTATING_ACTIONS = frozenset(
    {"upsert", "delete", "create_group", "create_user", "update_user", "add_user_to_groups", "write_value", "write_values"}
)


class FakeTM1:
    """Minimal TM1Service double: records every call, tiny in-memory model.

    ``calls`` holds ``(action, kind, name, detail)`` tuples where ``kind``
    is the zipio object type (``dimensions``, ..., ``security``, ``data``)
    and ``detail`` carries assertion-relevant extras (create/overwrite,
    view class, created-user state, written cell).
    """

    def __init__(self, existing: set[tuple[str, str]] | None = None, fail: dict[tuple[str, str], Exception] | None = None):
        self.existing = existing or set()
        self.fail = fail or {}
        self.calls: list[tuple[str, str, str, str]] = []
        self.connection_kwargs: dict | None = None
        self.cell_writes: list[tuple[str, list[str], dict]] = []
        self.memberships: dict[str, list[str]] = {}

        self.dimensions = _FakeCrud(self, "dimensions")
        self.cubes = _FakeCubes(self)
        self.views = _FakeViews(self)
        self.subsets = _FakeSubsets(self)
        self.processes = _FakeCrud(self, "processes")
        self.chores = _FakeCrud(self, "chores")
        self.security = _FakeSecurity(self)

    @property
    def mutating_calls(self) -> list[tuple[str, str, str, str]]:
        """Every recorded call that would change the target server."""
        return [call for call in self.calls if call[0] in MUTATING_ACTIONS]

    def _record(self, action: str, kind: str, name: str, detail: str = "") -> None:
        self.calls.append((action, kind, name, detail))

    def _fail_if_injected(self, kind: str, name: str) -> None:
        failure = self.fail.get((kind, name))
        if failure is not None:
            raise failure

    def __enter__(self) -> FakeTM1:
        self._record("connect", "", "")
        return self

    def __exit__(self, *exc_info) -> bool:
        return False


class _FakeCrud:
    """exists / update_or_create / delete for one object type."""

    def __init__(self, tm1: FakeTM1, kind: str):
        self._tm1 = tm1
        self._kind = kind

    def exists(self, name: str) -> bool:
        self._tm1._record("exists", self._kind, name)
        return (self._kind, name) in self._tm1.existing

    def update_or_create(self, tm1_object) -> None:
        name = tm1_object.name
        self._tm1._fail_if_injected(self._kind, name)
        detail = "overwrite" if (self._kind, name) in self._tm1.existing else "create"
        self._tm1._record("upsert", self._kind, name, detail)

    def delete(self, name: str) -> None:
        self._tm1._fail_if_injected(self._kind, name)
        self._tm1._record("delete", self._kind, name)


class _FakeCubes(_FakeCrud):
    """Cube service plus the rules and cells surfaces."""

    def __init__(self, tm1: FakeTM1):
        super().__init__(tm1, "cubes")

    def update_or_create_rules(self, cube_name: str, rules) -> None:
        self._tm1._record("upsert_rules", "cubes", cube_name, str(rules))

    @property
    def cells(self) -> _FakeCells:
        return _FakeCells(self._tm1)


class _FakeCells:
    """Cell write surface: value writes for rights, chunk writes for data."""

    def __init__(self, tm1: FakeTM1):
        self._tm1 = tm1

    def write_value(self, value, cube_name: str, element_tuple: tuple[str, ...], dimensions=None) -> None:
        self._tm1._fail_if_injected("data", cube_name)
        self._tm1._record("write_value", "data", cube_name, repr((value, element_tuple)))

    def write_values(self, cube_name: str, cellset_as_dict: dict, dimensions=None) -> str:
        self._tm1._fail_if_injected("data", cube_name)
        self._tm1.cell_writes.append((cube_name, list(dimensions or []), dict(cellset_as_dict)))
        self._tm1._record("write_values", "data", cube_name, f"{len(cellset_as_dict)} cells")


class _FakeViews(_FakeCrud):
    """View service whose exists() matches the public-only contract."""

    def __init__(self, tm1: FakeTM1):
        super().__init__(tm1, "views")

    def exists(self, cube_name: str, view_name: str, private: bool = False) -> bool:
        self._tm1._record("exists", "views", view_name, cube_name)
        return ("views", view_name) in self._tm1.existing

    def update_or_create(self, view, private: bool = False) -> None:
        self._tm1._fail_if_injected("views", view.name)
        detail = type(view).__name__
        self._tm1._record("upsert", "views", view.name, detail)


class _FakeSubsets(_FakeCrud):
    """Subset service recording whether the upserted subset is dynamic."""

    def __init__(self, tm1: FakeTM1):
        super().__init__(tm1, "subsets")

    def exists(self, subset_name: str, dimension_name: str, hierarchy_name: str = None, private: bool = False) -> bool:
        self._tm1._record("exists", "subsets", subset_name, dimension_name)
        return ("subsets", subset_name) in self._tm1.existing

    def update_or_create(self, subset, private: bool = False) -> None:
        self._tm1._fail_if_injected("subsets", subset.name)
        detail = "dynamic" if getattr(subset, "expression", None) else "static"
        self._tm1._record("upsert", "subsets", subset.name, detail)


class _FakeSecurity:
    """Security surface: groups, users, memberships."""

    def __init__(self, tm1: FakeTM1):
        self._tm1 = tm1

    def group_exists(self, group_name: str) -> bool:
        self._tm1._record("group_exists", "security", group_name)
        return ("group", group_name) in self._tm1.existing

    def create_group(self, group_name: str) -> None:
        self._tm1._record("create_group", "security", group_name)

    def user_exists(self, user_name: str) -> bool:
        self._tm1._record("user_exists", "security", user_name)
        return ("user", user_name) in self._tm1.existing

    def create_user(self, user: User) -> None:
        detail = f"enabled={user.enabled};password_set={bool(user.password)}"
        self._tm1._record("create_user", "security", user.name, detail)

    def get_user(self, user_name: str) -> User:
        self._tm1._record("get_user", "security", user_name)
        return User(name=user_name, groups=self._tm1.memberships.get(user_name, []))

    def update_user(self, user: User) -> None:
        self._tm1._record("update_user", "security", user.name, f"friendly={user.friendly_name}")

    def add_user_to_groups(self, user_name: str, groups) -> None:
        self._tm1._record("add_user_to_groups", "security", user_name, ",".join(groups))
        current = self._tm1.memberships.setdefault(user_name, [])
        current.extend(group for group in groups if group not in current)


def install_fake_tm1(monkeypatch, existing: set[tuple[str, str]] | None = None, fail: dict | None = None) -> FakeTM1:
    """Patch ``tm1_dump.load.TM1Service`` so run_load uses one FakeTM1."""
    fake = FakeTM1(existing=existing, fail=fail)

    def factory(**kwargs):
        fake.connection_kwargs = kwargs
        return fake

    monkeypatch.setattr("tm1_dump.load.TM1Service", factory)
    return fake


@pytest.fixture(name="install_fake_tm1")
def fixture_install_fake_tm1(monkeypatch):
    """Fixture wrapper around :func:`install_fake_tm1`."""

    def _install(existing=None, fail=None) -> FakeTM1:
        return install_fake_tm1(monkeypatch, existing=existing, fail=fail)

    return _install


@pytest.fixture(name="fixture_zip_path")
def fixture_fixture_zip_path(tmp_path) -> str:
    """A fresh contract-valid fixture dump zip."""
    from fixture_dump import write_fixture_zip

    zip_path = tmp_path / "basic_dump.zip"
    write_fixture_zip(zip_path)
    return str(zip_path)
